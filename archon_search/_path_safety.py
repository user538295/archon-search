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
"""
from __future__ import annotations

import tarfile
from pathlib import Path


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
        raise PathUnsafeError("nul_byte")

    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise PathUnsafeError("not_absolute")

    if ".." in Path(raw).parts:
        raise PathUnsafeError("contains_dotdot")

    return expanded.resolve(strict=False)


# ---------------------------------------------------------------------------
# Export / import path safety (Task 2.1)
# ---------------------------------------------------------------------------

_ALLOWED_ARCHIVE_MEMBERS = frozenset({"manifest.json", "documents.jsonl"})


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
    raise PathUnsafeError("outside_allowed_dirs")


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
            raise PathUnsafeError("unsafe_tar_member")
        # Reject any '..' component
        if ".." in name.split("/"):
            raise PathUnsafeError("unsafe_tar_member")
        # Reject names not in the allowed set
        if name not in _ALLOWED_ARCHIVE_MEMBERS:
            raise PathUnsafeError("unsafe_tar_member")
        member_names.add(name)
    # Require exactly the two expected members
    if member_names != _ALLOWED_ARCHIVE_MEMBERS:
        raise PathUnsafeError("unsafe_tar_member")
