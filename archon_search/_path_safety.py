"""Path safety validation for ingest and export/import endpoints.

Rejects inputs that could cause unintended filesystem traversal or OS-level
misinterpretation before the path reaches the pipeline.

PathUnsafeError reason codes:
  - "empty": raw path is an empty string
  - "whitespace_only": raw path is non-empty but contains only whitespace
  - "nul_byte": raw path contains a NUL byte
  - "not_absolute": path is not absolute after expanduser()
  - "contains_dotdot": path contains a ".." component
  - "outside_allowed_dirs": resolved path is not within any allowed base dir
  - "unsafe_tar_member": tar archive member has an unsafe name or path
  - "denied_location": an ingest path is, or lies under, a location that is never ingested (see IngestPathPolicy)
  - "outside_allowed_roots": an ingest path is not under any ``[ingest].allowed_roots`` entry
  - "allowed_roots_required": a non-admin caller supplied an ingest path while ``[ingest].allowed_roots`` is empty
  - "glob_empty" / "glob_too_long" / "glob_absolute" / "glob_too_recursive": an unsafe ``ingest_directory`` glob
"""
from __future__ import annotations

import fnmatch
import os
import stat
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DENIED_LOCATION: Final[str] = "denied_location"
# Reasons of the path validators that the export/import code (``jobs.export_archive``) also matches or raises.
NUL_BYTE: Final[str] = "nul_byte"
CONTAINS_DOTDOT: Final[str] = "contains_dotdot"
OUTSIDE_ALLOWED_DIRS: Final[str] = "outside_allowed_dirs"
UNSAFE_TAR_MEMBER: Final[str] = "unsafe_tar_member"
# The export archive: a gzip tar of exactly these two members, opened for reading in this mode.
MANIFEST_MEMBER: Final[str] = "manifest.json"
DOCUMENTS_MEMBER: Final[str] = "documents.jsonl"
ARCHIVE_READ_MODE: Final = "r:gz"
OUTSIDE_ALLOWED_ROOTS: Final[str] = "outside_allowed_roots"
ALLOWED_ROOTS_REQUIRED: Final[str] = "allowed_roots_required"
# The wire text for ``ALLOWED_ROOTS_REQUIRED``: fixed (it names no path) and pointing at the operator's remedy.
ALLOWED_ROOTS_REQUIRED_DETAIL: Final[str] = (
    "path is unsafe: path-based ingest by this API key needs the operator to set [ingest].allowed_roots; "
    "send the content as inline documents instead"
)
# The longest glob pattern the MCP ``ingest_directory`` tool accepts, and the most ``**`` segments in one: a glob is
# a walk of the tree, and every ``**`` multiplies it. Chosen bounds, far above any real pattern.
MAX_GLOB_PATTERN_LENGTH: Final[int] = 256
MAX_GLOB_RECURSIVE_SEGMENTS: Final[int] = 4


class PathUnsafeError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_ingest_path(raw: str) -> Path:
    """Validate and return the resolved absolute Path. Raises PathUnsafeError on rejection.

    Rejects:
      - empty or whitespace-only input
      - NUL byte in input
      - non-absolute path (checked after expanduser() so ~/foo is accepted)
      - any element of Path(raw).parts equal to ".."

    On accept returns Path(raw).expanduser().resolve(strict=False).
    Does NOT pre-check existence — non-existent paths pass through to downstream "not found".
    Validation operates on the **raw** Path.parts; the returned value is resolve()d and
    may point elsewhere via symlinks. Symlink-resolution is intentionally NOT validated
    (deferred to a future allowed_dirs feature). See brief Core Flow §4e. (C1-I-DA2-1)
    """
    if raw == "":
        raise PathUnsafeError("empty")

    # Two branches are deliberate: emit distinct reason codes ("empty" vs "whitespace_only") for caller diagnostics.
    if not raw.strip():
        raise PathUnsafeError("whitespace_only")

    if "\x00" in raw:
        raise PathUnsafeError(NUL_BYTE)

    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise PathUnsafeError("not_absolute")

    if ".." in Path(raw).parts:
        raise PathUnsafeError(CONTAINS_DOTDOT)

    return expanded.resolve(strict=False)


# ---------------------------------------------------------------------------
# Ingest scope: what a path may be read from (SecReview #3)
# ---------------------------------------------------------------------------

# Directories no ingest may read from, whoever asks: kernel/device views that expose process secrets
# (``/proc/self/environ``) or block forever (``/dev/zero``, FIFOs in ``/dev``), and the system's own secret
# stores (TLS private keys, root's home directory, sudo policy).
_SYSTEM_DENY_DIRS: Final[tuple[Path, ...]] = (
    Path("/proc"), Path("/sys"), Path("/dev"),
    Path("/root"), Path("/var/root"),
    Path("/etc/ssl/private"), Path("/etc/sudoers.d"),
)
# System files no ingest may read: the account databases and sudo policy.
_SYSTEM_DENY_FILES: Final[tuple[Path, ...]] = (
    Path("/etc/passwd"), Path("/etc/shadow"), Path("/etc/gshadow"), Path("/etc/master.passwd"),
    Path("/etc/sudoers"), Path("/etc/security/opasswd"),
)
# Directories that hold credentials, matched case-insensitively as a run of consecutive path components (so
# ``.config/gcloud`` matches ``~/.config/gcloud/credentials.db`` and not ``~/.config/other``).
_SECRET_DIR_SEQUENCES: Final[tuple[tuple[str, ...], ...]] = (
    (".ssh",), (".aws",), (".gnupg",), (".kube",), (".docker",), (".azure",), (".gcloud",), (".oci",),
    (".password-store",),
    (".config", "gcloud"), (".config", "gh"), (".config", "rclone"), (".config", "doctl"),
    # Browser profiles (saved logins, cookies, session tokens) and the macOS keychains.
    (".mozilla",), (".config", "google-chrome"), (".config", "chromium"), (".config", "microsoft-edge"),
    (".config", "bravesoftware"),
    ("library", "keychains"),
    ("library", "application support", "google", "chrome"),
    ("library", "application support", "firefox"),
    ("library", "application support", "microsoft edge"),
    ("library", "application support", "bravesoftware"),
    ("library", "safari"),
)
# File-name patterns of credentials and of this program's own secret files, matched case-insensitively against the
# final component only (a virtualenv directory called ``.env`` is not a secret; a file called ``.env`` is).
_SECRET_FILE_PATTERNS: Final[tuple[str, ...]] = (
    "id_*", "*_rsa*",
    ".env", ".env.*", "*.pem", "*.p12", "*.pfx", "*.keystore", "*.jks",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass", ".dockercfg", ".s3cfg", ".vault-token",
    ".search.env", ".secrets.env", "keys.json",
)
# ``*.key`` is a private key only when it is a PEM file: Keynote presentations use the same extension. It is
# denied when its first bytes (after whitespace) are a PEM armour line, or when it cannot be read to find out.
_KEY_FILE_SUFFIX: Final[str] = ".key"
_PEM_ARMOUR_PREFIX: Final[bytes] = b"-----BEGIN"
_PEM_PROBE_BYTES: Final[int] = 128


def _is_secret_name(name: str) -> bool:
    """Whether the file name *name* (any case) matches one of :data:`_SECRET_FILE_PATTERNS`."""
    folded = name.casefold()
    return any(fnmatch.fnmatchcase(folded, pattern) for pattern in _SECRET_FILE_PATTERNS)


_DirectoryCache = dict[Path, "frozenset[tuple[int, int]] | None"]


def _secret_identities(directory: Path) -> frozenset[tuple[int, int]] | None:
    """``(st_dev, st_ino)`` of every regular file in *directory* named like a credential or being a PEM ``*.key``.

    One listing, no recursion; only a credential-named or ``*.key`` entry is stat'ed or opened. ``None`` when the
    directory cannot be listed.
    """
    identities: set[tuple[int, int]] = set()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not (_is_secret_name(entry.name) or entry.name.casefold().endswith(_KEY_FILE_SUFFIX)):
                    continue
                try:
                    info = os.lstat(entry.path)  # not ``entry.stat()``: that has no inode or device on Windows
                except FileNotFoundError:
                    continue  # the entry vanished between the listing and the stat (an editor's temp file)
                if stat.S_ISREG(info.st_mode) and (_is_secret_name(entry.name) or _is_pem_key_file(directory / entry.name)):
                    identities.add((info.st_dev, info.st_ino))
    except OSError:
        return None
    return frozenset(identities)


def _has_secret_named_hard_link(path: Path, cache: _DirectoryCache | None = None) -> bool:
    """Whether *path* is a regular file with a hard link, in its own directory, whose name is a credential name.

    A hard link is only a second directory entry for one inode, so no name check on *path* can see that ``hard.md``
    is ``.env``. Costs one ``stat``, and, only for a regular file whose link count is above 1, one listing of
    *path*'s directory (no recursion), made once per directory when the caller passes a *cache* shared across the
    files of one walk: an entry with the same device and inode whose name matches :data:`_SECRET_FILE_PATTERNS`,
    or is a PEM ``*.key``, counts. A hard link in a *different* directory is not looked for. ``False`` for a path
    that is missing or not a regular file; ``True`` when the directory cannot be listed to find out.
    """
    try:
        info = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink <= 1:
        return False
    if cache is None:
        identities = _secret_identities(path.parent)
    else:
        if path.parent not in cache:
            cache[path.parent] = _secret_identities(path.parent)
        identities = cache[path.parent]
    return identities is None or (info.st_dev, info.st_ino) in identities


def _is_pem_key_file(path: Path) -> bool:
    """Whether *path* is a ``*.key`` file that is, or may be, a PEM private key.

    ``False`` for a missing path or a directory (a Keynote bundle): there is nothing to leak, and the files inside a
    bundle are judged by their own names. ``True`` for a file that starts with a PEM armour line and for one that
    exists but cannot be read (it is not worth the risk).
    """
    if path.suffix.casefold() != _KEY_FILE_SUFFIX:
        return False
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return False
    except OSError:
        return True
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            return False
        if not stat.S_ISREG(mode):
            return True
        head = os.read(fd, _PEM_PROBE_BYTES)
    except OSError:
        return True
    finally:
        os.close(fd)
    return head.lstrip().startswith(_PEM_ARMOUR_PREFIX)


def _has_secret_directory(parts: tuple[str, ...]) -> bool:
    """Whether *parts* (a path's components) contain any of the :data:`_SECRET_DIR_SEQUENCES` as a consecutive run."""
    folded = tuple(part.casefold() for part in parts)
    return any(
        folded[start : start + len(sequence)] == sequence
        for sequence in _SECRET_DIR_SEQUENCES
        for start in range(len(folded) - len(sequence) + 1)
    )


def _file_identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of *path* (symlinks followed), or ``None`` when it cannot be stat'ed."""
    try:
        stat_result = os.stat(path)
    except OSError:
        return None
    return stat_result.st_dev, stat_result.st_ino


def _identities(paths: Iterable[Path]) -> frozenset[tuple[int, int]]:
    return frozenset(identity for identity in map(_file_identity, paths) if identity is not None)


@dataclass(frozen=True)
class IngestPathPolicy:
    """Which paths an ingest may read.

    * Always denied (:meth:`check_secrets`): the *deny_roots* (the data directory and the other directories the
      server keeps its own state in) and everything under them, the exact *deny_files*, the system's secret
      directories and files (``/proc``, ``/sys``, ``/dev``, ``/root``, ``/etc/shadow``, ``/etc/passwd``, ...),
      credential directories (``.ssh``, ``.kube``, ``.docker``, cloud CLIs, browser profiles) and credential-named
      files (``id_*``, ``.env``, ``*.pem``, ``.npmrc``, a PEM ``*.key``, ...).
    * Restricted to *allowed_roots* when that list is non-empty (:meth:`check_scope`), for a path a client
      supplies. Paths named in the operator's own configuration (collection paths) are operator-trusted and skip
      this check but never :meth:`check_secrets`.
    * A non-admin client may supply a path only when *allowed_roots* is non-empty (:meth:`check_client_path`): with
      no roots configured the whole filesystem is in scope, and only the operator may decide that.

    Locations are compared by file identity (device and inode), not by spelling: a symlink, a hard link, a
    different letter case on a case-insensitive filesystem or a bind mount of a denied location is still that
    location. Paths are resolved first, so a symlink cannot smuggle a denied target in under an allowed name.
    A credential-named file (``.env``, ``id_rsa``) is denied by name, not by identity, so a hard link to it is
    recognised only when the credential-named link sits in the same directory (see :meth:`check_secrets`).
    """

    deny_roots: tuple[Path, ...] = ()
    deny_files: tuple[Path, ...] = ()
    allowed_roots: tuple[Path, ...] = ()

    def check_secrets(self, path: Path, *, probe_content: bool = True, directory_cache: _DirectoryCache | None = None) -> None:
        """Raise ``PathUnsafeError("denied_location")`` when *path* is, or lies under, a location that is never ingested.

        A hard link to a file in *deny_files* (or a system file) is caught by device and inode wherever it lies. A
        hard link to a credential-named file (``.env``, ``id_rsa``, ...) is known only by name, so it is caught only
        when the other name is in the **same directory** as *path* (one listing, made only for a regular file with
        a link count above 1); a link to such a file from a different directory is not detected.

        *probe_content* opens a ``*.key`` file to tell a PEM key from a Keynote deck and looks for such a
        same-directory hard link; callers whose path is not yet confined to the allowed roots pass ``False`` so
        neither is an existence/content oracle. *directory_cache* lets a walk over many files share one listing
        per directory (pass the same dict for every file; it is not refreshed, so keep it per walk).
        """
        resolved = path.resolve(strict=False)
        if _has_secret_directory(resolved.parts):
            raise PathUnsafeError(DENIED_LOCATION)
        if _is_secret_name(resolved.name) or (probe_content and _is_pem_key_file(resolved)):
            raise PathUnsafeError(DENIED_LOCATION)
        if probe_content and _has_secret_named_hard_link(resolved, directory_cache):
            raise PathUnsafeError(DENIED_LOCATION)
        denied_dirs = _identities((*self.deny_roots, *_SYSTEM_DENY_DIRS))
        if any(identity in denied_dirs for identity in map(_file_identity, (resolved, *resolved.parents)) if identity):
            raise PathUnsafeError(DENIED_LOCATION)
        target = _file_identity(resolved)
        if target is not None and target in _identities((*self.deny_files, *_SYSTEM_DENY_FILES)):
            raise PathUnsafeError(DENIED_LOCATION)

    def check_scope(self, path: Path) -> None:
        """Raise ``PathUnsafeError("outside_allowed_roots")`` when *allowed_roots* is set and *path* is not under one."""
        if not self.allowed_roots:
            return
        resolved = path.resolve(strict=False)
        allowed = _identities(self.allowed_roots)
        if not any(identity in allowed for identity in map(_file_identity, (resolved, *resolved.parents)) if identity):
            raise PathUnsafeError(OUTSIDE_ALLOWED_ROOTS)

    def check_client_path(self, path: Path, *, admin: bool) -> None:
        """The full rule for a path a client supplies.

        Never a secret location; inside *allowed_roots* if set; and, for a non-admin caller, *allowed_roots* must be
        set (``allowed_roots_required``). An admin caller may read any non-secret path when no roots are set.
        """
        if not admin and not self.allowed_roots:
            self.check_secrets(path, probe_content=False)
            raise PathUnsafeError(ALLOWED_ROOTS_REQUIRED)
        # Scope first: the PEM probe opens the file, so it must only run on a path already inside the allowed roots.
        self.check_scope(path)
        self.check_secrets(path)


def unsafe_path_detail(reason: str) -> str:
    """The REST ``detail`` for a refused ingest path: ``path is unsafe: <reason>``, or the fixed allowed-roots text."""
    if reason == ALLOWED_ROOTS_REQUIRED:
        return ALLOWED_ROOTS_REQUIRED_DETAIL
    return f"path is unsafe: {reason}"

def validate_ingest_source(raw: str, policy: IngestPathPolicy, *, admin: bool) -> Path:
    """:func:`validate_ingest_path`, then *policy*'s :meth:`~IngestPathPolicy.check_client_path` on the resolved path.

    For a path the server will read from disk on a client's behalf (REST ``/ingest``, ``POST /collections/``, the MCP
    ingest tools). A client-supplied label that names no file on this host (an inline document's ``source_path``)
    needs only :func:`validate_ingest_path`. *admin* is whether the caller's key is an admin key.

    Raises:
        PathUnsafeError: ``denied_location``, ``outside_allowed_roots`` or ``allowed_roots_required``, or any reason
            of :func:`validate_ingest_path`.
    """
    resolved = validate_ingest_path(raw)
    policy.check_client_path(resolved, admin=admin)
    return resolved


def validate_glob_pattern(pattern: str) -> str:
    """Return *pattern* when it is a safe relative glob for :meth:`pathlib.Path.glob`; raise ``PathUnsafeError`` otherwise.

    Rejects an empty or over-long pattern, a NUL byte, an absolute pattern and any ``..`` component (which would
    walk outside the directory being ingested), and more than ``MAX_GLOB_RECURSIVE_SEGMENTS`` ``**`` segments
    (each multiplies the walk).
    """
    if not pattern.strip():
        raise PathUnsafeError("glob_empty")
    if len(pattern) > MAX_GLOB_PATTERN_LENGTH:
        raise PathUnsafeError("glob_too_long")
    if "\x00" in pattern:
        raise PathUnsafeError(NUL_BYTE)
    if pattern.startswith(("/", "\\")):
        raise PathUnsafeError("glob_absolute")
    parts = pattern.replace("\\", "/").split("/")
    if ".." in parts:
        raise PathUnsafeError(CONTAINS_DOTDOT)
    if parts.count("**") > MAX_GLOB_RECURSIVE_SEGMENTS:
        raise PathUnsafeError("glob_too_recursive")
    return pattern


# ---------------------------------------------------------------------------
# Export / import path safety (Task 2.1)
# ---------------------------------------------------------------------------

_ALLOWED_ARCHIVE_MEMBERS = frozenset({MANIFEST_MEMBER, DOCUMENTS_MEMBER})


def validate_export_path(raw: str, allowed_base_dirs: list[Path]) -> Path:
    """Validate and return the resolved absolute Path for an export/import archive.

    Extends validate_ingest_path() with an allowlist-based directory check.

    Args:
        raw: Raw path string supplied by the caller.
        allowed_base_dirs: List of base directories the resolved path must be
            relative to (i.e. contained within). Checked via Path.is_relative_to().

    Returns:
        The resolved Path on success.

    Raises:
        PathUnsafeError(reason="outside_allowed_dirs"): if the resolved path is
            not relative to any of the allowed_base_dirs.
        PathUnsafeError: propagates any reason from validate_ingest_path() for
            empty/whitespace/nul/non-absolute/dotdot violations.
    """
    resolved = validate_ingest_path(raw)
    for allowed in allowed_base_dirs:
        if resolved.is_relative_to(allowed.resolve()):
            return resolved
    raise PathUnsafeError(OUTSIDE_ALLOWED_DIRS)


def validate_archive_members(tf: tarfile.TarFile) -> None:
    """Validate that a tar archive contains exactly the expected members.

    A safe archive must contain exactly two members: 'manifest.json' and
    'documents.jsonl' — no more, no less. Any member with an absolute path,
    a '..' path component, or a name outside the allowed set is rejected.

    Args:
        tf: An open TarFile to inspect.

    Raises:
        PathUnsafeError(reason="unsafe_tar_member"): if any member fails the
            safety checks or if the set of member names is not exactly
            {'manifest.json', 'documents.jsonl'}.
    """
    member_names: set[str] = set()
    for member in tf.getmembers():
        name = member.name
        # Reject absolute paths
        if name.startswith("/"):
            raise PathUnsafeError(UNSAFE_TAR_MEMBER)
        # Reject any '..' component
        if ".." in name.split("/"):
            raise PathUnsafeError(UNSAFE_TAR_MEMBER)
        # Reject names not in the allowed set
        if name not in _ALLOWED_ARCHIVE_MEMBERS:
            raise PathUnsafeError(UNSAFE_TAR_MEMBER)
        member_names.add(name)
    # Require exactly the two expected members
    if member_names != _ALLOWED_ARCHIVE_MEMBERS:
        raise PathUnsafeError(UNSAFE_TAR_MEMBER)


def validate_archive_file(archive_path: Path) -> None:
    """Open *archive_path* as a gzip tar and run :func:`validate_archive_members` on it.

    Blocking (file I/O and gzip decompression of the member table): an async caller runs it in a thread.

    Raises:
        PathUnsafeError: if the members are not exactly the two expected, safely named ones.
        tarfile.TarError, OSError, EOFError: if the file is not a readable gzip tar. The text of these can reflect the
            archive's own bytes, so a caller logs it and sends a fixed detail.
    """
    with tarfile.open(archive_path, ARCHIVE_READ_MODE) as tf:
        validate_archive_members(tf)
