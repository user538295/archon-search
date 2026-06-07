"""Module path resolver: derive a dotted module path from a file path."""
from __future__ import annotations

from pathlib import Path


def strip_extension(stem: str) -> str:
    """Strip all but the leading component from a dotted stem.

    For ``"lib.d"`` (as produced from ``lib.d.ts`` after one suffix strip)
    this returns ``"lib.d"``, preserving the dot-notation of compound
    extensions.  The function only strips a *trailing* double-extension if
    the caller already stripped one suffix via :meth:`Path.with_suffix("")`.

    Args:
        stem: A filename stem, possibly containing dots (e.g. ``"index.d"``).

    Returns:
        The stem unchanged — one suffix was already stripped by the caller.
    """
    # One suffix was stripped by the caller via Path.with_suffix("").
    # We intentionally do NOT strip further — this preserves "lib.d" for .d.ts.
    return stem


def derive_module_path(file_path: Path, collection_root: Path | None) -> str:
    """Compute the dotted module path for *file_path* relative to *collection_root*.

    Algorithm (applied in order):
    1. If *collection_root* is None: return ``file_path.stem``.
    2. Compute ``rel = file_path.relative_to(collection_root)``.
    3. Strip one extension: ``parts = rel.with_suffix("").parts``.
    4. Join parts with ``.``.
    5. If the last segment is ``__init__``, drop it (Python package root).
    6. For Python files (``.py``): replace hyphens with underscores in each segment.
    7. If the last segment is ``index`` and ext is ``.ts`` or ``.js``, drop it.

    Args:
        file_path: Absolute path to the source file.
        collection_root: Root directory of the collection, or None.

    Returns:
        Dotted module path string; may be empty for a root ``__init__.py``.
    """
    ext = file_path.suffix.lower()

    # Step 1: no root → stem-only fallback
    if collection_root is None:
        return file_path.stem

    # Step 2: compute relative path
    rel = file_path.relative_to(collection_root)

    # Step 3: strip one extension
    parts = list(rel.with_suffix("").parts)

    # Step 4: join with dots
    # Step 5: drop trailing __init__
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]

    # Step 6: hyphen → underscore for Python files
    if ext == ".py":
        parts = [p.replace("-", "_") for p in parts]

    # Step 7: drop trailing "index" for .ts / .js
    if parts and parts[-1] == "index" and ext in {".ts", ".js"}:
        parts = parts[:-1]

    return ".".join(parts)


class ModuleResolver:
    """Resolve and cache module paths for files within a collection root."""

    def __init__(self, collection_root: Path | None = None) -> None:
        self._root = collection_root
        self._cache: dict[Path, str] = {}

    def resolve_path(self, file_path: Path) -> str:
        """Return the dotted module path for *file_path*.

        Results are cached — repeated calls with the same path are O(1).

        Args:
            file_path: Absolute path to the source file.

        Returns:
            Dotted module path string.
        """
        if file_path not in self._cache:
            self._cache[file_path] = derive_module_path(file_path, self._root)
        return self._cache[file_path]

    def resolve_many(self, paths: list[Path]) -> dict[Path, str]:
        """Resolve module paths for a batch of file paths.

        Args:
            paths: List of absolute file paths.

        Returns:
            Mapping from each path to its dotted module path string.
        """
        return {p: self.resolve_path(p) for p in paths}

    def clear_cache(self) -> None:
        """Evict all cached resolutions."""
        self._cache.clear()
