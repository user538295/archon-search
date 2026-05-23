"""Path safety validation for ingest endpoints (A5a).

Provides ``validate_ingest_path`` and ``PathUnsafeError``.
This module imports nothing project-local, so it is safe to import from
any layer (server, CLI, eval) without circular-import risk.
"""
from __future__ import annotations

from pathlib import Path


class PathUnsafeError(ValueError):
    """Raised by ``validate_ingest_path`` when a path fails safety checks.

    Subclasses ``ValueError`` so callers that catch ``ValueError`` (including
    Pydantic ``field_validator``) see it naturally.

    ``reason`` is a short code from the set:
    ``"empty"``, ``"whitespace_only"``, ``"nul_byte"``,
    ``"not_absolute"``, ``"contains_dotdot"``.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_ingest_path(raw: str) -> Path:
    """Validate *raw* and return the resolved absolute ``Path``.

    Raises ``PathUnsafeError`` on rejection.

    Checks in order (cheapest first):
    1. Empty string → ``reason="empty"``.
    2. Whitespace-only string → ``reason="whitespace_only"``.
    3. NUL byte in string → ``reason="nul_byte"``.
    4. ``Path(raw).expanduser()`` — so ``~/foo`` resolves to an absolute path.
    5. Any element of ``Path.parts`` equal to ``".."`` → ``reason="contains_dotdot"``.
       (Checked BEFORE absoluteness so ``../foo`` gives ``contains_dotdot``, not ``not_absolute``.)
    6. Non-absolute after expanduser → ``reason="not_absolute"``.

    On accept: returns ``Path(raw).expanduser().resolve(strict=False)``.

    Does NOT pre-check existence — non-existent paths pass through to downstream
    "not found" handling.

    Does NOT check symlinks — symlink-resolution is intentionally NOT validated
    (deferred to a future ``allowed_dirs`` feature).

    Validation operates on the raw ``Path.parts``; the returned value is
    ``resolve()``d and may point elsewhere via symlinks.
    """
    # Check 1: empty
    if not raw:
        raise PathUnsafeError("empty")

    # Check 2: whitespace-only
    if not raw.strip():
        raise PathUnsafeError("whitespace_only")

    # Check 3: NUL byte
    if "\x00" in raw:
        raise PathUnsafeError("nul_byte")

    # Check 4: expanduser (so ~/foo becomes an absolute path)
    expanded = Path(raw).expanduser()

    # Check 5: dotdot parts — check BEFORE absoluteness so that a bare ".."
    # returns "contains_dotdot" rather than "not_absolute".  Relative paths
    # that happen to contain ".." (e.g. "../foo") are also caught here first.
    if ".." in expanded.parts:
        raise PathUnsafeError("contains_dotdot")

    # Check 6: absoluteness (after expanduser and dotdot)
    if not expanded.is_absolute():
        raise PathUnsafeError("not_absolute")

    return expanded.resolve(strict=False)
