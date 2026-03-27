"""RAG collection synchronisation utilities."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon.rag.pipeline import RagPipeline

logger = logging.getLogger("archon")

_LEGACY_COLLECTION = "archon-history"
_DEFAULT_SESSIONS_NAME = "sessions"


def path_to_collection_name(path: str) -> str:
    """Derive a deterministic, sanitized LanceDB collection name from a filesystem path.

    Rules:
    - Expand ``~`` and resolve to absolute path.
    - Use the last path component (``Path.name``) as the raw name.
    - Sanitize: lowercase, replace non-alphanumeric runs with ``_``,
      strip leading/trailing ``_``.
    - Fall back to ``"collection"`` if the result is empty.

    This function is collision-unaware by design.  Collision resolution is
    applied in :class:`RagCollectionSync`.
    """
    resolved = Path(path).expanduser().resolve()
    name = resolved.name.lower()
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    return name or "collection"


def _sanitize(raw: str) -> str:
    """Sanitize a string into a valid collection name fragment."""
    name = raw.lower()
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    return name or "collection"


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def manifest_lookup_by_path(manifest_path: Path, resolved_path: str) -> str | None:
    """Return collection name for path, or None. Used by CLI commands."""
    if not manifest_path.exists():
        return None
    manifest: dict[str, str] = json.loads(manifest_path.read_text())
    for col_name, src_path in manifest.items():
        if str(Path(src_path).expanduser().resolve()) == resolved_path:
            return col_name
    return None


class RagCollectionSync:
    """Synchronises LanceDB collections with a declarative list of filesystem paths."""

    def __init__(self, pipeline: RagPipeline) -> None:
        self._pipeline = pipeline

    async def sync(self, collections: list[str]) -> SyncResult:
        """Synchronise LanceDB collections with the given filesystem paths.

        Steps:
        0. Run migration: archon-history → sessions if needed.
        1. Build desired: {collection_name: resolved_path}.
        2. Get existing collections from store.
        3. Load manifest → managed_names.
        4. Drop undesired managed collections.
        5. Record skipped (unmanaged, not in desired).
        6. Ingest new collections.
        7. Unchanged = existing ∩ desired.
        8. Update manifest atomically.
        """
        result = SyncResult()
        store = self._pipeline.store
        manifest_path = store._db_path / "sync_manifest.json"

        # Step 0: migration
        existing_info = await store.list_collections()
        existing: set[str] = {c.name for c in existing_info}
        await self._maybe_migrate(store, existing, manifest_path)
        # Refresh after potential migration
        existing_info = await store.list_collections()
        existing = {c.name for c in existing_info}

        # Step 1: build desired {name: resolved_path}
        desired = self._build_desired(collections)

        # Step 3: load manifest
        managed_names = self._load_manifest_names(manifest_path)

        # Step 4: drop (existing ∩ managed) - desired
        to_remove = (existing & managed_names) - desired.keys()
        for name in to_remove:
            try:
                await store.drop_collection(name)
                result.removed.append(name)
            except KeyError:
                logger.warning(
                    "Collection %r in manifest but not in LanceDB; skipping drop", name
                )
                result.errors.append(
                    f"Collection {name!r} in manifest but not in LanceDB; skipping drop"
                )

        # Step 5: skipped = existing - managed_names - desired
        skipped = existing - managed_names - desired.keys()
        result.skipped.extend(sorted(skipped))

        # Step 6: ingest new collections
        to_add = desired.keys() - existing
        successfully_added: set[str] = set()
        for name, path_str in desired.items():
            if name not in to_add:
                continue
            p = Path(path_str)
            if not p.exists():
                result.errors.append(f"path does not exist: {path_str}")
                continue
            try:
                await self._pipeline.ingest_directory(p, name)
                result.added.append(name)
                successfully_added.add(name)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(str(exc))

        # Step 7: unchanged = existing ∩ desired
        unchanged = existing & desired.keys()
        result.unchanged.extend(sorted(unchanged))

        # Step 8: update manifest atomically
        new_manifest: dict[str, str] = {}
        for name, path_str in desired.items():
            if name in successfully_added or name in unchanged:
                new_manifest[name] = path_str
        self._write_manifest(manifest_path, new_manifest)

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _maybe_migrate(
        self, store, existing: set[str], manifest_path: Path
    ) -> None:
        """Rename archon-history → sessions if only legacy table exists."""
        has_legacy = _LEGACY_COLLECTION in existing
        has_sessions = _DEFAULT_SESSIONS_NAME in existing

        if has_legacy and not has_sessions:
            logger.info(
                "Migrating legacy collection %r → %r",
                _LEGACY_COLLECTION,
                _DEFAULT_SESSIONS_NAME,
            )
            try:
                await store.rename_collection(_LEGACY_COLLECTION, _DEFAULT_SESSIONS_NAME)
                logger.info("Migrated LanceDB table %r → %r", _LEGACY_COLLECTION, _DEFAULT_SESSIONS_NAME)
                # Update manifest: replace archon-history key with sessions
                if manifest_path.exists():
                    try:
                        manifest: dict[str, str] = json.loads(manifest_path.read_text())
                        if _LEGACY_COLLECTION in manifest:
                            path = manifest.pop(_LEGACY_COLLECTION)
                            manifest[_DEFAULT_SESSIONS_NAME] = path
                            self._write_manifest(manifest_path, manifest)
                    except (json.JSONDecodeError, OSError):
                        pass  # manifest corruption handled elsewhere
            except NotImplementedError:
                logger.warning(
                    "rename_table not supported by this LanceDB version. "
                    "%r remains as an unmanaged collection. "
                    "Manually drop it once migration to %r is confirmed.",
                    _LEGACY_COLLECTION,
                    _DEFAULT_SESSIONS_NAME,
                )
        elif has_legacy and has_sessions:
            logger.warning(
                "Both %r and %r exist in LanceDB — skipping migration. "
                "Remove %r manually if it is no longer needed.",
                _LEGACY_COLLECTION,
                _DEFAULT_SESSIONS_NAME,
                _LEGACY_COLLECTION,
            )

    def _build_desired(self, collections: list[str]) -> dict[str, str]:
        """Return {collection_name: resolved_path} with collision resolution."""
        if not collections:
            return {}

        # Deduplicate while preserving order
        seen_paths: dict[str, None] = {}
        for p in collections:
            seen_paths[str(Path(p).expanduser().resolve())] = None
        resolved_paths = list(seen_paths.keys())

        # Start with depth=1 (basename only)
        depth = 1
        max_depth = max(len(Path(p).parts) for p in resolved_paths) if resolved_paths else 1

        names = [path_to_collection_name(p) for p in resolved_paths]

        while depth <= max_depth:
            if len(set(names)) == len(names):
                break  # no collisions
            # Find collision groups and increase depth for colliders
            seen: dict[str, int] = {}
            for i, name in enumerate(names):
                if name in seen:
                    # collision — increase depth for both
                    prev_i = seen[name]
                    names[i] = self._name_at_depth(resolved_paths[i], depth + 1)
                    names[prev_i] = self._name_at_depth(resolved_paths[prev_i], depth + 1)
                else:
                    seen[name] = i
            depth += 1
        else:
            # Hash fallback for remaining collisions after max depth.
            # Group ALL entries with the same name and hash every one of them.
            name_to_indices: dict[str, list[int]] = {}
            for i, name in enumerate(names):
                name_to_indices.setdefault(name, []).append(i)
            for group_indices in name_to_indices.values():
                if len(group_indices) > 1:
                    for i in group_indices:
                        base = path_to_collection_name(resolved_paths[i])
                        h = hashlib.sha1(resolved_paths[i].encode()).hexdigest()[:6]
                        names[i] = f"{base}_{h}"

        return dict(zip(names, resolved_paths))

    def _name_at_depth(self, resolved_path: str, depth: int) -> str:
        """Build a collection name using `depth` path components from the right."""
        parts = Path(resolved_path).parts
        components = parts[-depth:] if depth <= len(parts) else parts
        raw = "_".join(components)
        name = _sanitize(raw)
        return name or "collection"

    def _load_manifest_names(self, manifest_path: Path) -> set[str]:
        """Return set of collection names from the manifest file."""
        if not manifest_path.exists():
            return set()
        try:
            data: dict[str, str] = json.loads(manifest_path.read_text())
            return set(data.keys())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read sync manifest %s: %s", manifest_path, exc)
            return set()

    def _write_manifest(self, manifest_path: Path, manifest: dict[str, str]) -> None:
        """Write manifest atomically via a .tmp file."""
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = manifest_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(manifest, indent=2))
        os.replace(tmp_path, manifest_path)
