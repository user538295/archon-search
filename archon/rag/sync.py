"""RAG collection synchronisation utilities."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from archon.rag.pipeline import RagPipeline
    from archon.rag.progress import CollectionProgress, IndexingStateStore

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


def manifest_remove_entry(manifest_path: Path, col_name: str) -> None:
    """Remove col_name from manifest JSON. Best-effort — silently ignores all errors."""
    if not manifest_path.exists():
        return
    try:
        data = json.loads(manifest_path.read_text())
        data.pop(col_name, None)
        manifest_path.write_text(json.dumps(data, indent=2))
    except (json.JSONDecodeError, OSError):
        pass


class RagCollectionSync:
    """Synchronises LanceDB collections with a declarative list of filesystem paths."""

    def __init__(
        self,
        pipeline: RagPipeline,
        state_store: IndexingStateStore | None = None,
        pinned_collections: list[str] | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._state_store = state_store
        self._pinned_collections = pinned_collections or []
        self._collection_locks: dict[str, asyncio.Lock] = {}

    async def sync(
        self,
        collections: list[str],
        progress_cb: Callable[[int, int], None | Awaitable[None]] | None = None,
    ) -> SyncResult:
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
        from archon.rag.progress import CollectionProgress, IndexingStatus

        result = SyncResult()
        store = self._pipeline.store
        manifest_path = store._db_path / "sync_manifest.json"

        # Crash recovery: reset stale IN_PROGRESS → PENDING
        self._reset_stale_in_progress()

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
                # Clean removed collection from state
                self._safe_state_remove(name)
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
        sorted_to_add = self._sort_ingestion_order(to_add, desired)
        for name in sorted_to_add:
            path_str = desired[name]
            p = Path(path_str)
            if not p.exists():
                result.errors.append(f"path does not exist: {path_str}")
                continue
            async with self._get_lock(name):
                # Write PENDING state
                self._safe_state_update(name, CollectionProgress(
                    status=IndexingStatus.PENDING, total_files=0,
                ))
                # Transition to IN_PROGRESS
                started_at = datetime.now(UTC).isoformat()
                self._safe_state_update(name, CollectionProgress(
                    status=IndexingStatus.IN_PROGRESS,
                    total_files=0,
                    started_at=started_at,
                ))

                # Track total_files from callback
                cb_total_files = 0

                def _make_progress_wrapper(col_name: str):
                    """Build a progress wrapper that captures total from callbacks."""
                    nonlocal cb_total_files
                    total_captured = False

                    def wrapper(done_count: int, total: int):
                        nonlocal cb_total_files, total_captured
                        if not total_captured:
                            cb_total_files = total
                            total_captured = True
                            # Update total_files from first callback
                            self._safe_state_update(col_name, CollectionProgress(
                                status=IndexingStatus.IN_PROGRESS,
                                total_files=total,
                                processed_files=0,
                                started_at=started_at,
                            ))
                        if done_count % 50 == 0:
                            self._safe_state_update(col_name, CollectionProgress(
                                status=IndexingStatus.IN_PROGRESS,
                                total_files=total,
                                processed_files=done_count,
                                started_at=started_at,
                            ))
                        # Call caller's callback
                        if progress_cb is not None:
                            return progress_cb(done_count, total)
                        return None

                    return wrapper

                wrapped_cb = _make_progress_wrapper(name)

                try:
                    results = await self._pipeline.ingest_directory(
                        p, name, progress_cb=wrapped_cb,
                    )
                    ok_count = sum(1 for r in results if r.status == "ok")
                    error_count = sum(1 for r in results if r.status != "ok")
                    self._safe_state_update(name, CollectionProgress(
                        status=IndexingStatus.DONE,
                        total_files=len(results),
                        processed_files=ok_count,
                        error_count=error_count,
                        started_at=started_at,
                        completed_at=datetime.now(UTC).isoformat(),
                    ))
                    result.added.append(name)
                    successfully_added.add(name)
                except Exception as exc:  # noqa: BLE001
                    self._safe_state_update(name, CollectionProgress(
                        status=IndexingStatus.FAILED,
                        total_files=cb_total_files,
                        started_at=started_at,
                        completed_at=datetime.now(UTC).isoformat(),
                        error=str(exc),
                    ))
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
    # State store helpers (all writes are safe — never abort sync)
    # ------------------------------------------------------------------

    def _safe_state_update(self, name: str, progress: CollectionProgress) -> None:
        """Update collection progress in state store. Swallows all errors."""
        if self._state_store is None:
            return
        try:
            self._state_store.update_collection(name, progress)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to write indexing state for %r", name, exc_info=True)

    def _safe_state_remove(self, name: str) -> None:
        """Remove collection from state store. Swallows all errors."""
        if self._state_store is None:
            return
        try:
            self._state_store.remove_collection(name)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to remove indexing state for %r", name, exc_info=True)

    def _reset_stale_in_progress(self) -> None:
        """Reset any IN_PROGRESS entries to PENDING (crash recovery)."""
        if self._state_store is None:
            return
        try:
            from archon.rag.progress import CollectionProgress, IndexingStatus

            state = self._state_store.read()
            if state is None:
                return
            for name, cp in state.collections.items():
                if cp.status == IndexingStatus.IN_PROGRESS:
                    state.collections[name] = CollectionProgress(
                        status=IndexingStatus.PENDING,
                        total_files=cp.total_files,
                        processed_files=cp.processed_files,
                    )
            self._state_store.write(state)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to reset stale IN_PROGRESS states", exc_info=True)

    # ------------------------------------------------------------------
    # Lock helpers
    # ------------------------------------------------------------------

    def _get_lock(self, name: str) -> asyncio.Lock:
        """Return the per-collection lock, creating it on first access."""
        if name not in self._collection_locks:
            self._collection_locks[name] = asyncio.Lock()
        return self._collection_locks[name]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sort_ingestion_order(
        self, to_add: set[str], desired: dict[str, str],
    ) -> list[str]:
        """Sort collections for ingestion: pinned first (declaration order), then alphabetical."""
        if not self._pinned_collections:
            return sorted(to_add)

        # Reverse lookup: resolved_path → collection name
        path_to_name = {resolved: name for name, resolved in desired.items()}

        # Resolve pinned paths and map to collection names (preserving declaration order)
        pinned_names: list[str] = []
        for p in self._pinned_collections:
            resolved = str(Path(p).expanduser().resolve())
            name = path_to_name.get(resolved)
            if name is not None and name in to_add and name not in pinned_names:
                pinned_names.append(name)

        # Non-pinned: alphabetical
        non_pinned = sorted(to_add - set(pinned_names))
        return pinned_names + non_pinned

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
