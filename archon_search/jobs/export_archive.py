"""Export/import archive utilities for collection backup and restore.

Provides:
  - EXPORT_SCHEMA_VERSION: int constant identifying the archive format version.
  - ExportArchiveWriter: two-phase writer (temp JSONL → finalized .tar.gz).
  - ImportArchiveReader: validated streaming reader for import archives.
  - ArchiveRejectedError: an archive the import refuses, with a caller-safe message.
  - INVALID_ARCHIVE_DETAIL / INVALID_MANIFEST_DETAIL / manifest_error_detail: the
    wire-safe texts the REST route and the MCP tool share for an unreadable archive.
"""
from __future__ import annotations

import contextlib
import importlib.metadata
import io
import json
import logging
import os
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Self, cast

from archon_search._durable_io import (
    PRIVATE_FILE_MODE,
    ensure_private_dir,
    restrict_fd_to_owner,
)
from archon_search._path_safety import (
    ARCHIVE_READ_MODE,
    DOCUMENTS_MEMBER,
    MANIFEST_MEMBER,
    OUTSIDE_ALLOWED_DIRS,
    PathUnsafeError,
    validate_archive_members,
    validate_export_path,
    validate_ingest_path,
)
from archon_search.constants import DEFAULT_NAMESPACE

logger = logging.getLogger(__name__)

EXPORT_SCHEMA_VERSION: int = 1


def _open_private(path: Path, extra_flags: int, mode: str) -> IO[bytes]:
    """Open *path* for writing as an owner-only file (also tightening one an older version left looser)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | extra_flags, PRIVATE_FILE_MODE)  # durable-write-exempt — streamed temp/archive, not an atomic publish
    try:
        restrict_fd_to_owner(fd)
        return os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)  # fdopen did not take ownership of it
        raise


class ArchiveRejectedError(ValueError):
    """A refusal the import makes: the archive's content is unusable, or the target collection already exists.

    The message is fixed text built by archon-search, quoting only line numbers, its own
    field names and request values, and archive values through :func:`quote_archive_value`
    — never a library's or the server's exception text — so it is safe for a wire-facing
    ``detail`` or a job's ``error``.
    """


# Wire-safe detail for an import archive that does not open as a tar.gz (tarfile/gzip
# error text can reflect the archive's own bytes); the cause is logged.
INVALID_ARCHIVE_DETAIL = "archive could not be opened: invalid or corrupt tar.gz"

# Wire-safe detail for a manifest that failed to read for any reason other than an
# ArchiveRejectedError (whose own message is safe to send); the cause is logged.
INVALID_MANIFEST_DETAIL = "manifest.json could not be read"

#: Longest archive-supplied value an ArchiveRejectedError message quotes: an archive is
#: untrusted input, so a crafted manifest value cannot inflate a ``detail`` or ``error``.
ARCHIVE_VALUE_MAX_CHARS = 80
_TRUNCATION_MARK = "…"


def quote_archive_value(value: object) -> str:
    """``repr(value)``, cut to :data:`ARCHIVE_VALUE_MAX_CHARS` with a trailing ``…``."""
    quoted = repr(value)
    if len(quoted) <= ARCHIVE_VALUE_MAX_CHARS:
        return quoted
    return quoted[: ARCHIVE_VALUE_MAX_CHARS - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK


_MIB = 1024 * 1024
#: Largest ``manifest.json`` an import reads: a manifest is a few hundred bytes (a collection description at most), so
#: anything near this is hostile, and reading it unbounded would let a small gzip bomb pin the process's memory.
IMPORT_MANIFEST_MAX_BYTES = _MIB
#: Longest ``documents.jsonl`` line (without its newline) an import reads. A line is one chunk: its text, its base64
#: vector and at most ``META_MAX_FIELDS`` capped metadata entries, a few tens of KiB; this leaves two orders of
#: magnitude of headroom while still bounding what one line can make the importer hold.
IMPORT_DOC_LINE_MAX_BYTES = 8 * _MIB
#: Subdirectory of the data directory that holds per-namespace export directories.
EXPORTS_DIRNAME = "exports"
#: Default subdirectory of the data directory for scheduled backups (``[backup].output_dir`` when unset).
BACKUPS_DIRNAME = "backups"
#: ``ArchiveRejectedError`` texts. Fixed, and they name no other namespace.
MANIFEST_TOO_LARGE_DETAIL = "manifest.json exceeds the size limit"
LINE_TOO_LARGE_REASON = "line exceeds the size limit"
ARCHIVE_NAMESPACE_MISMATCH_DETAIL = "archive was not exported from this namespace"


def exports_dir(data_dir: Path, namespace: str) -> Path:
    """The one directory an export of *namespace* is written to (``<data_dir>/exports/<namespace>``).

    A namespace is validated to ``[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}`` before it reaches here, so it is one path segment.
    """
    return data_dir / EXPORTS_DIRNAME / namespace


def backups_dir(backup_output_dir: str | Path, namespace: str) -> Path:
    """The directory the backup loop writes *namespace*'s scheduled backups to (``<[backup].output_dir>/<namespace>``)."""
    return Path(backup_output_dir) / namespace


def importable_dirs(data_dir: Path, backup_output_dir: str | Path, namespace: str) -> list[Path]:
    """The directories an import by *namespace* may read an archive from: its own exports and its own backups.

    An empty *backup_output_dir* means the default, ``<data_dir>/backups`` (what ``load_config`` resolves it to).
    Nothing else under the data directory or the backup directory is importable, whatever the caller's key: an
    admin key has no cross-namespace archive access either, because the manifest binds an archive to its exporter.
    """
    backup_root = backup_output_dir or data_dir / BACKUPS_DIRNAME
    return [exports_dir(data_dir, namespace), backups_dir(backup_root, namespace)]


def validate_legacy_flat_archive(raw: str, data_dir: Path) -> Path:
    """The resolved path of *raw* if it is a file directly in the flat ``<data_dir>/exports`` directory.

    Before archives were stored per namespace every export landed there. The directory is not recursive: a file in
    ``exports/<namespace>/`` is some namespace's own and never matches.

    Raises:
        PathUnsafeError: ``outside_allowed_dirs`` for any other path; the reasons of ``validate_ingest_path``.
    """
    resolved = validate_ingest_path(raw)
    if resolved.parent == (data_dir / EXPORTS_DIRNAME).resolve():
        return resolved
    raise PathUnsafeError(OUTSIDE_ALLOWED_DIRS)


def importable_archive_path(raw: str, data_dir: Path, backup_output_dir: str | Path, namespace: str) -> Path:
    """The resolved path of the archive *raw* if *namespace* may import it, else :class:`PathUnsafeError`.

    It must sit in the namespace's own export or backup directory (:func:`importable_dirs`). The default namespace
    may also import a pre-upgrade file in the flat ``<data_dir>/exports`` directory
    (:func:`validate_legacy_flat_archive`); no other namespace may. A path outside answers the same
    ``outside_allowed_dirs`` whether or not a file exists there.
    """
    try:
        return validate_export_path(raw, importable_dirs(data_dir, backup_output_dir, namespace))
    except PathUnsafeError as exc:
        if namespace == DEFAULT_NAMESPACE and exc.reason == OUTSIDE_ALLOWED_DIRS:
            return validate_legacy_flat_archive(raw, data_dir)
        raise


def manifest_namespace_ok(
    manifest: dict, namespace: str, archive_path: Path, data_dir: Path, backup_output_dir: str | Path
) -> bool:
    """Whether *namespace* may import the archive at *archive_path* whose manifest is *manifest*.

    A manifest that names a namespace must name *namespace*. One that names none (an archive made before archives
    were bound to their exporter, e.g. a pre-upgrade backup) is accepted only when the file sits in a directory
    *namespace* may import from: ownership then comes from where the file is.
    """
    if "namespace" in manifest:
        return manifest["namespace"] == namespace
    try:
        importable_archive_path(str(archive_path), data_dir, backup_output_dir, namespace)
    except PathUnsafeError:
        return False
    return True


def manifest_error_detail(exc: Exception, archive_path: Path) -> str:
    """The wire-safe text for a failed :meth:`ImportArchiveReader.read_manifest`.

    An :class:`ArchiveRejectedError` message or a ``PathUnsafeError`` reason code is
    text archon-search built itself; anything else is logged (with its traceback) and
    reported as :data:`INVALID_MANIFEST_DETAIL`.
    """
    if isinstance(exc, (ArchiveRejectedError, PathUnsafeError)):
        return str(exc)
    logger.warning("import archive %s: manifest could not be read: %s", archive_path, exc, exc_info=exc)
    return INVALID_MANIFEST_DETAIL


def get_lancedb_version() -> str | None:
    """Resolve the installed lancedb package version for export manifests.

    Returns the version string on success, or ``None`` if the package is not
    installed (in which case a WARNING is logged). Added in D2-1.4 so the
    export manifest records the LanceDB version it was produced against for
    forensic / migration purposes.
    """
    try:
        return importlib.metadata.version("lancedb")
    except importlib.metadata.PackageNotFoundError:
        logger.warning("Could not determine lancedb version")
        return None

# Required keys in the manifest dict. ``namespace`` is deliberately not one: an archive made before archives were bound
# to their exporter has none. A manifest that names a namespace binds the archive to it (an import by another
# namespace is refused, so an archive copied between namespaces' directories is inert); one that names none is
# accepted only from the importing namespace's own directories (:func:`manifest_namespace_ok`).
_REQUIRED_MANIFEST_KEYS = frozenset({
    "schema_version",
    "collection",
    "exported_at",
    "doc_count",
    "active_embedding_model",
})


class ExportArchiveWriter:
    """Two-phase archive writer: stream docs to a temp JSONL file, then finalize to .tar.gz.

    Usage::

        writer = ExportArchiveWriter(tmp_path)
        for doc in docs:
            writer.write_doc(doc)
        writer.finalize(manifest, archive_path)

    Or as a context manager (cleanup is called automatically on exception)::

        with writer:
            writer.write_doc(doc)
            writer.finalize(manifest, archive_path)
    """

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self._lines_written: int = 0
        self._file: IO[bytes] | None = None
        ensure_private_dir(tmp_path.parent)
        self._file = _open_private(tmp_path, os.O_APPEND, "ab")

    def write_doc(self, doc: dict) -> None:
        """Serialize *doc* as a compact JSON line and append to the temp file."""
        if self._file is None:
            raise RuntimeError("ExportArchiveWriter is closed")
        line = json.dumps(doc, ensure_ascii=False) + "\n"
        self._file.write(line.encode())
        self._lines_written += 1

    @property
    def lines_written(self) -> int:
        """Number of documents written so far."""
        return self._lines_written

    def finalize(self, manifest: dict, archive_path: Path) -> None:
        """Close the temp file and build the final .tar.gz archive.

        The archive contains exactly two members:
          - ``manifest.json``: JSON-encoded *manifest* dict.
          - ``documents.jsonl``: the accumulated temp file contents.

        Calls :meth:`cleanup` at the end to remove the temp file.
        """
        if self._file is not None:
            self._file.close()
            self._file = None

        manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode()
        with _open_private(archive_path, os.O_TRUNC, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tf:
            # Add manifest.json
            info = tarfile.TarInfo(name=MANIFEST_MEMBER)
            info.size = len(manifest_bytes)
            tf.addfile(info, io.BytesIO(manifest_bytes))
            # Add documents.jsonl from the temp file
            tf.add(str(self._tmp_path), arcname=DOCUMENTS_MEMBER)

        self.cleanup()

    def cleanup(self) -> None:
        """Close the temp file (if open) and delete it (if it exists)."""
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None
        self._tmp_path.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if exc_type is not None:
            self.cleanup()


def _bounded_lines(f: IO[bytes]) -> Iterator[bytes | None]:
    """Yield *f*'s lines (newline kept), or ``None`` for one longer than :data:`IMPORT_DOC_LINE_MAX_BYTES`.

    Reads at most one byte more than the limit at a time, so a hostile member without a newline cannot make this
    hold more than that; the rest of an over-long line is read in the same bounded steps and dropped.
    """
    limit = IMPORT_DOC_LINE_MAX_BYTES
    while chunk := f.readline(limit + 1):
        if len(chunk) <= limit or chunk.endswith(b"\n"):
            yield chunk
            continue
        while not chunk.endswith(b"\n") and (chunk := f.readline(limit + 1)):
            pass
        yield None


class ImportArchiveReader:
    """Streaming reader for an archon-search collection export archive.

    Usage::

        reader = ImportArchiveReader(archive_path)
        manifest = reader.read_manifest()
        for doc in reader.iter_docs(skip=0):
            ...
    """

    def __init__(self, archive_path: Path) -> None:
        self._archive_path = archive_path
        self.skipped_lines: int = 0
        #: 1-based ``documents.jsonl`` line of the document :meth:`iter_docs` last yielded.
        self.line_number: int = 0

    def read_manifest(self) -> dict:
        """Open the archive, validate its members, and return the parsed manifest.

        Raises:
            PathUnsafeError: if any tar member is unsafe (via :func:`validate_archive_members`).
            ArchiveRejectedError: if the manifest is not a regular file, larger than
                :data:`IMPORT_MANIFEST_MAX_BYTES`, not UTF-8 JSON (including an oversized int or
                nesting past the parser's recursion limit), not a JSON object, or is missing required keys.
        """
        with tarfile.open(self._archive_path, ARCHIVE_READ_MODE) as tf:
            validate_archive_members(tf)
            member = tf.getmember(MANIFEST_MEMBER)
            f = tf.extractfile(member)
            if f is None:
                raise ArchiveRejectedError("manifest.json member is not a regular file")
            raw_manifest = f.read(IMPORT_MANIFEST_MAX_BYTES + 1)
            if len(raw_manifest) > IMPORT_MANIFEST_MAX_BYTES:
                raise ArchiveRejectedError(MANIFEST_TOO_LARGE_DETAIL)
            try:
                manifest = json.loads(raw_manifest.decode("utf-8"))
            # ValueError covers UnicodeDecodeError, JSONDecodeError and an oversized int;
            # RecursionError covers nesting deeper than the parser's recursion limit.
            except (ValueError, RecursionError) as exc:
                logger.warning("manifest.json of %s is not valid UTF-8 JSON: %s", self._archive_path, exc)
                raise ArchiveRejectedError("manifest.json is not valid UTF-8 JSON") from exc

        if not isinstance(manifest, dict):
            raise ArchiveRejectedError(
                f"manifest.json must be a JSON object, got {type(manifest).__name__}"
            )

        missing = _REQUIRED_MANIFEST_KEYS - manifest.keys()
        if missing:
            raise ArchiveRejectedError(
                f"manifest.json is missing required keys: {', '.join(sorted(missing))}"
            )

        return manifest

    def iter_docs(self, skip: int = 0, on_error: str = "fail") -> Iterator[dict]:
        """Stream documents from ``documents.jsonl``, skipping the first *skip* lines.

        Each yielded value is a parsed JSON dict representing one document.

        When *on_error* is ``"skip"``, corrupt lines (not UTF-8 JSON — including an oversized int or nesting past the
        parser's recursion limit — not a JSON object, or longer than :data:`IMPORT_DOC_LINE_MAX_BYTES`) are logged,
        counted in :attr:`skipped_lines`, and
        skipped.  When ``"fail"`` (default), a corrupt line raises immediately, unlogged: the caller
        logs the refusal.  :attr:`line_number` holds the line of the document last yielded. An over-long line is
        discarded in bounded reads, never held whole.

        Raises:
            ArchiveRejectedError: if *on_error* is ``"fail"`` and a line is corrupt
                (the message names its 1-based line number), or ``documents.jsonl``
                is not a regular file.
        """
        self.skipped_lines = 0
        with tarfile.open(self._archive_path, ARCHIVE_READ_MODE) as tf:
            member = tf.getmember(DOCUMENTS_MEMBER)
            f = tf.extractfile(member)
            if f is None:
                raise ArchiveRejectedError("documents.jsonl member is not a regular file")
            lines_skipped = 0
            lineno = 0
            for raw_line in _bounded_lines(f):
                lineno += 1
                # ``None``: the line exceeded the size limit (its bytes were discarded unread).
                line = None if raw_line is None else raw_line.rstrip(b"\n")
                if line is not None and not line:
                    continue
                if lines_skipped < skip:
                    lines_skipped += 1
                    continue
                cause: Exception | None = None
                if line is None:
                    parsed, reason = None, LINE_TOO_LARGE_REASON
                else:
                    try:
                        parsed = json.loads(line.decode("utf-8"))
                    # Same cases as read_manifest: decode/syntax errors, oversized ints, deep nesting.
                    except (ValueError, RecursionError) as exc:
                        parsed, cause, reason = None, exc, "not valid UTF-8 JSON"
                    else:
                        reason = "" if isinstance(parsed, dict) else "not a JSON object"
                if reason:
                    if on_error != "skip":
                        # The caller logs the refusal, with its job context and this cause.
                        raise ArchiveRejectedError(f"Corrupt line {lineno}: {reason}") from cause
                    self.skipped_lines += 1
                    logger.warning("Corrupt line %d: %s", lineno, cause or reason)
                    continue
                self.line_number = lineno
                yield cast(dict, parsed)  # ``reason`` is empty only for a parsed JSON object
