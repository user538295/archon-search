"""Path safety validation for ingest endpoints.

Rejects inputs that could cause unintended filesystem traversal or OS-level
misinterpretation before the path reaches the pipeline.
"""
from __future__ import annotations

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

    if not raw.strip():
        raise PathUnsafeError("whitespace_only")

    if "\x00" in raw:
        raise PathUnsafeError("nul_byte")

    # Absoluteness is checked after expanduser() so that ~/foo is accepted.
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise PathUnsafeError("not_absolute")

    if ".." in Path(raw).parts:
        raise PathUnsafeError("contains_dotdot")

    return expanded.resolve(strict=False)
