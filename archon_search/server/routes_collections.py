"""GET/POST/DELETE /collections/* endpoints — collection management."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from archon_search._path_safety import (
    PathUnsafeError,
    unsafe_path_detail,
    validate_ingest_source,
)
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig, resolve_active_model, save_config
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.model import job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.model_validation import (
    ModelValidationError,
    validate_embedding_model,
)
from archon_search.server._collection_job import (
    SERVICE_NOT_READY_DETAIL,
    create_collection_job,
    refuse_active_job_or_fail_legacy,
)
from archon_search.server._ingest_admission import (
    INGEST_BUSY_503_DESCRIPTION,
    IngestSlot,
    ingest_busy_503,
    reserve_ingest_slot,
)
from archon_search.server._ingest_lock import (
    COLLECTION_WRITER_IN_PROGRESS_DETAIL,
    STORE_BUSY_503_DESCRIPTION,
    STORE_BUSY_DETAIL,
    acquire_collection_lock_or_503,
    collection_job_in_progress_detail,
    store_busy_503,
)
from archon_search.server._ingested_by import parse_ingested_by_header
from archon_search.server.authz import (
    ADMIN_ONLY_RESPONSES,
    caller_identity,
    require_admin_dependency,
)
from archon_search.server.routes_jobs import (
    _REINDEX_COLLECTION_LOG_CONTEXT,
    INTERNAL_ERROR_DETAIL,
    REINDEX_FAILED_DETAIL,
    IngestRequest,
    _ingest_task,
    _persist_job_status,
    _reindex_task,
    _track_background_task,
)
from archon_search.server.schemas import (
    CollectionDetail,
    CollectionSummary,
    DeleteResponse,
    DocumentInfoItem,
    DocumentListResponse,
    ErrorDetail,
    ExpiringChunkItem,
    ExpiringChunksResponse,
    JobResponse,
    MigrateInPlaceResponse,
    MigrateRequest,
    MigrationPendingResponse,
    MigrationSpecSchema,
    PatchCollectionBody,
)
from archon_search.store import (
    STORE_SCHEMA_VERSION,
    ReindexResult,
    SearchStore,
    StoreBusyError,
)
from archon_search.sync import path_to_collection_name
from archon_search.types import (
    IngestJob,
    JobStatus,
    MetadataReindexJob,
    MigrationJob,
    MigrationKind,
    MigrationSpec,
    ReindexJob,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collections")

#: Wire-facing body when persisting a collection add/remove to the config file fails
#: (read-only file, hand-corrupted TOML, ...); the exception text embeds filesystem paths, so it is logged only.
CONFIG_PERSIST_FAILED_DETAIL = "could not save the collection change to the config file; see the server log for details"
CONFIG_PERSIST_FAILED_CODE = "config_persist_failed"
#: Wire-facing body when DELETE's table drop or meta-row delete fails after the config was persisted
#: (a failed table drop also rolls the config change back); the exception text (store paths) is logged only.
COLLECTION_DROP_FAILED_DETAIL = "could not remove the collection from the store; see the server log for details"
COLLECTION_DROP_FAILED_CODE = "collection_drop_failed"

# Job ``error`` text for background-task failures: the caught exception's message
# may embed internals (LanceDB paths), so it goes to the server log only.
MIGRATION_FAILED_DETAIL = "migration failed; see the server log for details"
METADATA_REINDEX_FAILED_DETAIL = "metadata reindex failed; see the server log for details"


class NoPendingRewriteMigrationError(RuntimeError):
    """No pending REWRITE migration for a resumed MigrationJob (codebase-built, safe to surface)."""


# ---------------------------------------------------------------------------
# Request / response helpers
# ---------------------------------------------------------------------------


class AddCollectionRequest(BaseModel):
    path: str
    embedding_model: str | None = None


class ReindexMetadataRequest(BaseModel):
    dry_run: bool = False
    normalize_timestamps: bool = True


# ---------------------------------------------------------------------------
# Collection registry helpers
# ---------------------------------------------------------------------------


def _all_collection_paths(config: SearchConfig) -> dict[str, str]:
    """Return {name: resolved_path} for all known paths (collections + pinned)."""
    result: dict[str, str] = {}
    all_paths = list(dict.fromkeys(config.collections + config.pinned_collections))
    for p in all_paths:
        name = path_to_collection_name(p)
        resolved = str(Path(p).expanduser().resolve())
        result[name] = resolved
    return result


def _collection_status(config: SearchConfig, state_store, name: str) -> str:
    """Return the indexing status string for a collection."""
    try:
        state = state_store.read()
        if state and name in state.collections:
            return str(state.collections[name].status)
    except Exception:  # noqa: BLE001, S110 — status is best-effort display data; any state-store read failure yields "not_yet_indexed"
        pass
    return "not_yet_indexed"


def _maybe_save_config(config: SearchConfig, request: Request) -> None:
    """Persist config to disk if config_path is set on app.state (graceful degradation)."""
    config_path = getattr(request.app.state, "config_path", None)
    if config_path is not None:
        save_config(config, config_path)


def _persist_config_or_500(config: SearchConfig, request: Request, rollback: Callable[[], None]) -> JSONResponse | None:
    """Persist *config*; on any failure run *rollback* and return the sanitized 500 response, else ``None``."""
    try:
        _maybe_save_config(config, request)
    except Exception:
        rollback()
        logger.exception("Failed to persist collection change to the config file")
        return JSONResponse(
            {"detail": CONFIG_PERSIST_FAILED_DETAIL, "code": CONFIG_PERSIST_FAILED_CODE},
            status_code=500,
        )
    return None


def _drop_path_from_config(config: SearchConfig, resolved: str) -> Callable[[], None]:
    """Remove *resolved* from both config path lists; return a callback re-inserting the removed entries.

    The callback re-inserts only the removed entries at their original indexes, so running it after
    an ``await`` cannot discard a change another request made to the lists in between. Defensively, an
    entry is skipped rather than duplicated when its list already holds an entry resolving to the same
    path (matched like the removal, so ``~/x`` and its absolute spelling are one entry).
    """

    def _resolve(path: str) -> str:
        return str(Path(path).expanduser().resolve())

    def _split(paths: list[str]) -> tuple[list[str], list[tuple[int, str]]]:
        kept: list[str] = []
        removed: list[tuple[int, str]] = []
        for index, p in enumerate(paths):
            if _resolve(p) == resolved:
                removed.append((index, p))
            else:
                kept.append(p)
        return kept, removed

    config.collections, removed_collections = _split(config.collections)
    config.pinned_collections, removed_pinned = _split(config.pinned_collections)

    def _restore() -> None:
        for paths, removed in ((config.collections, removed_collections), (config.pinned_collections, removed_pinned)):
            present = {_resolve(p) for p in paths}
            for index, p in removed:
                if _resolve(p) not in present:
                    paths.insert(index, p)

    return _restore


def _save_config_after_rollback(config: SearchConfig, request: Request, reason: str) -> None:
    """Best-effort config save after an in-memory rollback; a failure is logged so the caller's status survives."""
    try:
        _maybe_save_config(config, request)
    except Exception:
        logger.exception("Failed to rollback config after %s", reason)


def _rollback_config_entry(config: SearchConfig, request: Request, resolved: str, reason: str) -> None:
    """Remove *resolved* from ``config.collections`` if still there (a concurrent DELETE may have removed it), then save best-effort."""
    if resolved in config.collections:
        config.collections.remove(resolved)
    _save_config_after_rollback(config, request, reason)


async def _rollback_add(
    config: SearchConfig,
    request: Request,
    *,
    search_store: SearchStore,
    resolved: str,
    collection_name: str,
    ns: str,
    reason: str,
) -> None:
    """Undo add_collection's config entry and stub meta row; failures are logged, never raised."""
    _rollback_config_entry(config, request, resolved, reason)
    try:
        await search_store.delete_collection_meta(collection_name, ns)
    except Exception:
        logger.exception("Failed to rollback meta after %s", reason)


def _collection_drop_failed_response() -> JSONResponse:
    return JSONResponse({"detail": COLLECTION_DROP_FAILED_DETAIL, "code": COLLECTION_DROP_FAILED_CODE}, status_code=500)


async def _visible_meta_or_404(
    search_store: SearchStore, name: str, ns: str, path_to_name: dict[str, str]
) -> CollectionMeta | None:
    """*name*'s meta row in namespace *ns*, or ``None`` for a config-only collection; 404 when *ns* cannot see *name*.

    Mirrors list_collections: a config-only path is visible only in the default namespace AND only when it has no
    meta row in ANY namespace.
    """
    meta = await search_store.get_collection_meta(name, namespace=ns)
    if meta is None:
        config_only_visible = False
        if ns == DEFAULT_NAMESPACE and name in path_to_name:
            all_meta = await search_store.get_all_collections_meta()
            config_only_visible = not any(m.name == name for m in all_meta)
        if not config_only_visible:
            raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")
    return meta


def _collection_job_in_progress_503(job: IngestJob, name: str, ns: str) -> JSONResponse:
    """Log and build the 503 ``store_busy`` refusing DELETE of *name* while *job* may write to it.

    The job id appears in the detail only when *job* belongs to the caller's namespace *ns*.
    """
    logger.warning(
        "DELETE /collections/%s refused: %s %s is %s (job namespace=%s, caller namespace=%s)",
        name,
        type(job).__name__,
        job.job_id,
        job.status.value,
        job.namespace,
        ns,
    )
    return store_busy_503(collection_job_in_progress_detail(job, ns))


def _collection_writer_in_progress_503(name: str) -> JSONResponse:
    """Log and build the 503 ``store_busy`` refusing DELETE of *name* while a write that creates no job may write it."""
    logger.warning(
        "DELETE /collections/%s refused: an MCP ingest or a collection sync is writing the collection", name
    )
    return store_busy_503(COLLECTION_WRITER_IN_PROGRESS_DETAIL)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/", response_model=list[CollectionSummary], responses={401: {"model": ErrorDetail}})
async def list_collections(request: Request) -> list[CollectionSummary]:
    """List all known collections with basic metadata."""
    config: SearchConfig = request.app.state.config
    state_store = request.app.state.state_store
    search_store = request.app.state.search_store
    ns: str = request.state.namespace

    all_meta = await search_store.get_all_collections_meta()
    ns_names = {m.name for m in all_meta if m.namespace == ns}
    meta_by_name = {m.name: m for m in all_meta}

    path_to_name = _all_collection_paths(config)
    # Include namespace-visible collections that exist only in the meta store
    # (e.g. created by a single-file `POST /ingest`, which never adds a config path).
    all_names = list(dict.fromkeys([*path_to_name, *ns_names]))
    result = []
    for name in all_names:
        resolved = path_to_name.get(name, "")
        if name in ns_names:
            namespace = meta_by_name[name].namespace
        elif name not in meta_by_name and ns == DEFAULT_NAMESPACE:
            namespace = DEFAULT_NAMESPACE
        else:
            continue
        status = _collection_status(config, state_store, name)
        col_meta = meta_by_name.get(name)
        # Live count, not the maintained meta.chunk_count: the cached value drifts
        # (delete subtracts a vector count, TTL-expiry pruning never decrements it).
        # A metaless entry is a configured-but-not-yet-indexed collection → 0; this
        # also avoids surfacing an orphaned table's rows to a caller that has no meta
        # row for it. count_chunks is table-wide (its namespace arg is ignored by the
        # store), but one collection name maps to exactly one namespace, so the count
        # is namespace-isolated in practice.
        chunk_count = 0
        doc_count = 0
        if col_meta is not None:
            try:
                chunk_count = await search_store.count_chunks(name, namespace=namespace)
            except Exception:  # noqa: BLE001 — count is best-effort listing data; any store failure falls back to 0
                chunk_count = 0
            try:
                doc_count = await search_store.count_documents(name)
            except Exception:  # noqa: BLE001 — count is best-effort listing data; any store failure falls back to 0
                doc_count = 0
        result.append(CollectionSummary(
            name=name,
            path=resolved,
            description=(col_meta.description or "") if col_meta is not None else "",
            doc_count=doc_count,
            chunk_count=chunk_count,
            namespace=namespace,
            status=status,
            active_embedding_model=resolve_active_model(col_meta, config),
            needs_reindex=col_meta.needs_reindex if col_meta is not None else False,
        ))

    return result


_ERROR_401 = {401: {"model": ErrorDetail}}
_ERROR_401_404 = {401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}}
_ERROR_401_409 = {401: {"model": ErrorDetail}, 409: {"model": ErrorDetail}}
_ERROR_400_401_409 = {
    400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"},
    401: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
}
_ERROR_400_401_409_503 = {
    400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"},
    401: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    503: {"model": ErrorDetail, "description": "Store busy — another write holds the collection"},
}
_CONFIG_PERSIST_500_DESCRIPTION = "Config file could not be saved (code config_persist_failed); change rolled back"
_ERROR_500_REMOVE_COLLECTION = {
    500: {
        "model": ErrorDetail,
        "description": (
            f"{_CONFIG_PERSIST_500_DESCRIPTION}; or the table drop or meta-row delete failed "
            f"(code {COLLECTION_DROP_FAILED_CODE}); a failed table drop rolls the config change back"
        ),
    },
}
_ERROR_500_ADD_COLLECTION = {
    500: {
        "model": ErrorDetail,
        "description": (
            f"{_CONFIG_PERSIST_500_DESCRIPTION}; or the collection meta write / job creation failed "
            f'(body {{"detail": "{INTERNAL_ERROR_DETAIL}"}})'
        ),
    },
}


@router.post(
    "/",
    status_code=202,
    response_model=JobResponse,
    responses={
        **_ERROR_400_401_409_503,
        503: {"model": ErrorDetail, "description": f"Store busy — another write holds the collection; or {INGEST_BUSY_503_DESCRIPTION}"},
        422: {"model": ErrorDetail},
        **_ERROR_500_ADD_COLLECTION,
        **ADMIN_ONLY_RESPONSES,
    },
    dependencies=[Depends(require_admin_dependency)],
)
async def add_collection(body: AddCollectionRequest, request: Request) -> JobResponse | JSONResponse:
    """Add a new collection: persist config + enqueue ingest. Returns 202 + IngestJob.

    Admin key required: it appends the path to the shared ``archon-search.toml`` collection list, which the
    startup sync and ``POST /sync`` process for every namespace.

    Refused ``503 ingest_busy`` while ``[ingest].max_concurrent_tasks`` ingest jobs are running, before any side effect.
    """
    slot = reserve_ingest_slot(request.app.state)
    if slot is None:
        return ingest_busy_503()
    try:
        return await _add_collection(body, request, slot)
    finally:
        slot.release_unless_attached()


async def _add_collection(body: AddCollectionRequest, request: Request, slot: IngestSlot) -> JobResponse | JSONResponse:
    """The body of :func:`add_collection`; *slot* passes to the ingest task it starts."""
    config: SearchConfig = request.app.state.config
    store: JobStore = request.app.state.job_store
    search_store = request.app.state.search_store
    ns: str = request.state.namespace

    # Validate embedding_model early, before any side-effects
    if body.embedding_model is not None:
        try:
            await validate_embedding_model(body.embedding_model)
        except ModelValidationError as e:
            raise HTTPException(status_code=422, detail=str(e))

    try:
        resolved = str(
            await asyncio.to_thread(
                validate_ingest_source,
                body.path,
                request.app.state.ingest_path_policy,
                admin=caller_identity(request).admin,
            )
        )
    except PathUnsafeError as e:
        raise HTTPException(status_code=400, detail=unsafe_path_detail(e.reason))

    # Dedup check against resolved paths from both lists
    existing_resolved = {
        str(Path(p).expanduser().resolve())
        for p in config.collections + config.pinned_collections
    }
    if resolved in existing_resolved:
        return JSONResponse({"detail": "collection already registered"}, status_code=409)

    # Global name uniqueness check across all namespaces
    collection_name = path_to_collection_name(resolved)
    all_meta = await search_store.get_all_collections_meta()
    if any(m.name == collection_name for m in all_meta):
        return JSONResponse({"detail": "collection name already registered"}, status_code=409)

    config.collections.append(resolved)
    persist_error = _persist_config_or_500(config, request, lambda: config.collections.remove(resolved))
    if persist_error is not None:
        return persist_error

    # Write stub meta — update_collection_meta acquires the per-collection lock internally.
    # StoreBusyError → 503; ValueError → 409 TOCTOU race; other → 500.
    active_model = body.embedding_model if body.embedding_model is not None else config.embedding_model
    try:
        await search_store.update_collection_meta(
            CollectionMeta(
                name=collection_name,
                namespace=ns,
                active_embedding_model=active_model,
                pending_embedding_model=None,
                needs_reindex=False,
                reindex_job_id=None,
                community_rebuild_job_id=None,
                metadata_reindex_job_id=None,
                schema_version=STORE_SCHEMA_VERSION,
            )
        )
    except StoreBusyError as e:
        _rollback_config_entry(config, request, resolved, "stub meta write store-busy")
        retry_after = str(math.ceil(e.timeout_s))
        return JSONResponse(
            {"detail": f"store busy; retry in {retry_after} seconds"},
            status_code=503,
            headers={"Retry-After": retry_after},
        )
    except ValueError:
        # TOCTOU race: name claimed by another namespace between check and write
        _rollback_config_entry(config, request, resolved, "stub meta write name race")
        return JSONResponse({"detail": "collection name already registered"}, status_code=409)
    except Exception:
        logger.exception("Failed to write the stub meta row for collection %s", collection_name)
        _rollback_config_entry(config, request, resolved, "stub meta write failure")
        return JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)

    # Pre-acquire the per-collection lock for the ingest task.
    lock_result = await acquire_collection_lock_or_503(search_store, collection_name)
    if isinstance(lock_result, JSONResponse):
        # Lock busy for ingest task — roll back config and meta to leave clean state.
        await _rollback_add(
            config, request, search_store=search_store, resolved=resolved, collection_name=collection_name, ns=ns,
            reason="ingest-lock timeout",
        )
        retry_after_val = lock_result.headers.get("Retry-After", "")
        return JSONResponse(
            {"detail": f"store busy; retry in {retry_after_val} seconds"},
            status_code=503,
            headers={"Retry-After": retry_after_val},
        )

    ingested_by = parse_ingested_by_header(request.headers.get("X-Ingested-By"))
    try:
        job = store.create(namespace=ns, collection=collection_name)
    except Exception:
        logger.exception("Failed to create the ingest job for collection %s", collection_name)
        try:
            await _rollback_add(
                config, request, search_store=search_store, resolved=resolved, collection_name=collection_name, ns=ns,
                reason="job creation failure",
            )
        finally:
            if isinstance(lock_result, asyncio.Lock) and lock_result.locked():
                lock_result.release()
            # Only after the release (as in DELETE): discard_lock keeps an entry a queued waiter still needs.
            search_store.discard_lock(collection_name)
        return JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)
    ingest_body = IngestRequest(
        collection=collection_name, path=body.path, ingested_by=ingested_by
    ).with_resolved_path(resolved)
    _track_background_task(
        request.app.state, _ingest_task(request.app.state, job.job_id, ingest_body, ns, lock_result), slot=slot
    )

    return JobResponse(**job_to_dict(job))


@router.delete(
    "/{name}",
    response_model=DeleteResponse,
    responses={
        401: {"model": ErrorDetail},
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail},
        503: {
            "model": ErrorDetail,
            "description": (
                f"{STORE_BUSY_503_DESCRIPTION}, "
                "or a job may still be writing to the collection (an ingest, reindex, import, migration, "
                "community rebuild, or a metadata reindex that is not a dry run), "
                "or a write that creates no job is (an MCP ingest_file / ingest_directory, a collection sync: startup, "
                "POST /sync or filesystem watcher)"
            ),
        },
        **_ERROR_500_REMOVE_COLLECTION,
    },
)
async def remove_collection(name: str, request: Request) -> DeleteResponse | JSONResponse:
    """Remove a collection: delete config entry and drop LanceDB data.

    Once the table is dropped — or, for a config-only collection (no meta row), the config entry removed — the
    collection's base ingest jobs that are `FAILED` or `PENDING` — in every namespace, as the table is shared by
    name — become `CANCELLED`, so no maintenance retry (re-)creates the collection (best-effort: a failed write is
    logged and the delete still succeeds).
    """
    config: SearchConfig = request.app.state.config
    search_store = getattr(request.app.state, "search_store", None)
    ns: str = request.state.namespace

    path_to_name = _all_collection_paths(config)

    # A collection is visible (and removable) if it has a meta row in this
    # namespace — which a single-file `POST /ingest` writes even without adding
    # a config path. When the store is absent, fall back to config membership.
    meta = None
    if search_store is not None:
        meta = await _visible_meta_or_404(search_store, name, ns, path_to_name)
    elif name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    resolved = path_to_name.get(name, "")

    # Resolve config lists for comparison
    collections_resolved = [str(Path(p).expanduser().resolve()) for p in config.collections]
    pinned_resolved = [str(Path(p).expanduser().resolve()) for p in config.pinned_collections]

    in_collections = resolved in collections_resolved
    in_pinned = resolved in pinned_resolved

    # Pinned-only: path is in pinned but NOT in collections — reject
    if in_pinned and not in_collections:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Collection {name!r} is pinned-only; remove it from "
                "'pinned_collections' in config before deleting."
            ),
        )

    # Acquire the per-collection lock before mutating — 503 if a write is in progress (None without a search store:
    # nothing to lock).
    lock_result = await acquire_collection_lock_or_503(search_store, name)
    if isinstance(lock_result, JSONResponse):
        return lock_result

    collection_gone = False
    try:
        if meta is None and search_store is not None:
            # Read the row again under the lock: a first ingest that held the lock while this DELETE waited may have
            # created the collection's table and meta row (dropped below like any collection's), or another
            # namespace's meta row may now hide the config-only collection from this one (404).
            meta = await _visible_meta_or_404(search_store, name, ns, path_to_name)

        # Refuse if a job may still write this collection (JobStore.find_active_collection_job) — also for a
        # config-only collection, whose first ingest creates the table and meta row. Holding the lock alone is not
        # enough: no such job holds it for its whole run (_default_ingest_task_with_lock, for one, releases it early
        # to avoid an asyncio.Lock re-entrancy deadlock with store.ingest_chunks), so the job store is the
        # authoritative signal. The check must run under the lock, not before it: an ingest that held the lock while
        # this DELETE waited turns RUNNING and releases it, and only a check made after acquiring sees that (TOCTOU).
        # No job created through these routes can re-create the collection after the check: POST /ingest and
        # POST /collections/ pre-acquire this lock before creating their ingest job, a maintenance retry waits for it
        # before its claim, POST /collections/{name}/reindex, /reindex-metadata and /graph/{name}/rebuild-communities
        # create their job and meta pointer under it from a meta row re-read under it (create_collection_job), and a
        # rewrite migration created meanwhile opens the dropped table under this lock and fails cleanly. An import's
        # force_overwrite drop makes the same lock + job check (routes_export._ensure_collection_absent), and PATCH, the
        # in-place migrate and MCP update_collection re-read their meta row under this lock
        # (SearchStore.locked_collection_meta), so none writes it back after this drop. Writers that create no job — an
        # MCP ingest_file / ingest_directory and a collection sync (startup, POST /sync, filesystem watcher) — register
        # in SearchStore.collection_writer (under this lock) for their whole run — a sync for each collection's write
        # only — which this check reads (has_active_writer); a sync also skips a collection no longer in the live
        # config (SearchCollectionSync._write_collection, app._sync_watched_collection), so it cannot re-create one
        # deleted here.
        job_store = getattr(request.app.state, "job_store", None)
        active_job = job_store.find_active_collection_job(name, ns) if job_store is not None else None
        if active_job is not None:
            return _collection_job_in_progress_503(active_job, name, ns)
        if search_store is not None and search_store.has_active_writer(name):
            return _collection_writer_in_progress_503(name)

        # Mutate the config only now, under the lock. The returned rollback re-inserts just
        # the entries it removed, so it never discards config changes made by others meanwhile.
        restore_config_lists = _drop_path_from_config(config, resolved)
        persist_error = _persist_config_or_500(config, request, restore_config_lists)
        if persist_error is not None:
            return persist_error

        # Config-only collection (no meta row): nothing exists in the store to drop
        # (drop_collection/delete_collection_meta would fail).
        if meta is None:
            collection_gone = True
            if job_store is not None:
                # Auxiliary (best-effort, never fails the DELETE): a retry of a failed first ingest would create the
                # collection's table and meta row.
                job_store.cancel_ingest_retries(name)
            return DeleteResponse(name=name, deleted=True)

        if search_store is not None:
            try:
                # lock_for(name) is already held above (search_store is not None
                # ⇒ acquire_collection_lock_or_503 returned the acquired lock).
                # KeyError: the table doesn't exist — that's fine. RuntimeError (store
                # not connected, LanceDB failure) is a failed drop, handled below.
                with contextlib.suppress(KeyError):
                    await search_store.drop_collection(name, namespace=ns, _locked_by_caller=True)
            except Exception:
                logger.exception("Failed to drop collection %s; rolling back the config change", name)
                restore_config_lists()
                _save_config_after_rollback(config, request, "collection drop failure")
                return _collection_drop_failed_response()
            collection_gone = True
            if job_store is not None:
                # Auxiliary (best-effort, never fails the DELETE): no maintenance retry may re-create the table.
                job_store.cancel_ingest_retries(name)
            try:
                await search_store.delete_collection_meta(name, ns)
            except Exception:
                logger.exception("Failed to delete the meta row of dropped collection %s", name)
                return _collection_drop_failed_response()
    finally:
        if isinstance(lock_result, asyncio.Lock) and lock_result.locked():
            lock_result.release()
        if collection_gone and search_store is not None:
            # Only after the release: discard_lock keeps an entry a queued waiter still needs. Also for a config-only
            # collection: this DELETE created its entry (lock_for), which would otherwise outlive the collection.
            search_store.discard_lock(name)

    return DeleteResponse(name=name, deleted=True)


@router.get("/{name}", response_model=CollectionDetail, responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}})
async def get_collection_info(name: str, request: Request) -> CollectionDetail:
    """Return CollectionDetail for a single collection. 404 if not found."""
    config: SearchConfig = request.app.state.config
    state_store = request.app.state.state_store
    ns: str = request.state.namespace

    path_to_name = _all_collection_paths(config)

    # Namespace gate: 404 for cross-namespace access; also fetches meta for centroid/namespace.
    # A collection is visible if it has a meta row in this namespace (which a
    # single-file `POST /ingest` writes, even without a config path). When the
    # store is absent, fall back to config membership.
    search_store = getattr(request.app.state, "search_store", None)
    meta = None
    if search_store is not None:
        meta = await _visible_meta_or_404(search_store, name, ns, path_to_name)
    elif name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    resolved = path_to_name.get(name, "")
    status = _collection_status(config, state_store, name)

    # Pull extra detail from state if available
    last_indexed: str | None = None
    try:
        state = state_store.read()
        if state and name in state.collections:
            cp = state.collections[name]
            last_indexed = getattr(cp, "completed_at", None)
    except Exception:  # noqa: BLE001, S110 — last_indexed is best-effort detail; any state-store read failure leaves it None
        pass

    # Fetch real doc_count and ACL stats from search store; reuse already-fetched meta for centroid.
    doc_count = 0
    centroid_present = bool(meta is not None and meta.centroid)
    acl_protected = 0
    acl_open = 0
    chunk_count = 0
    if search_store is not None:
        try:
            doc_count = await search_store.count_documents(name)
        except Exception:  # noqa: BLE001 — stat is best-effort detail data; any store failure falls back to 0
            doc_count = 0
        try:
            chunk_count = await search_store.count_chunks(name, namespace=ns)
        except Exception:  # noqa: BLE001 — stat is best-effort detail data; any store failure falls back to 0
            chunk_count = 0
        try:
            acl_protected, acl_open = await search_store.get_acl_stats(name)
        except Exception:  # noqa: BLE001 — stat is best-effort detail data; any store failure falls back to 0
            acl_protected, acl_open = 0, 0

    data = {
        "name": name,
        "path": resolved,
        "description": (meta.description or "") if meta is not None else "",
        "doc_count": doc_count,
        "chunk_count": chunk_count,
        "status": status,
        "active_embedding_model": resolve_active_model(meta, config),
        "pending_embedding_model": meta.pending_embedding_model if meta is not None else None,
        "needs_reindex": meta.needs_reindex if meta is not None else False,
        "reindex_job_id": meta.reindex_job_id if meta is not None else None,
        "centroid_present": centroid_present,
        "last_indexed": last_indexed,
        "namespace": meta.namespace if meta is not None else DEFAULT_NAMESPACE,
        "acl_protected_count": acl_protected,
        "acl_open_count": acl_open,
        "default_ttl_seconds": meta.default_ttl_seconds if meta is not None else None,
        "schema_version": meta.schema_version if meta is not None else 0,
    }
    return CollectionDetail(**data)


@router.get("/{name}/documents", response_model=DocumentListResponse, responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}})
async def list_collection_documents(
    name: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> DocumentListResponse:
    """List documents in a collection with cursor-based pagination (E0c BE-6).

    - ``limit``: 1–200, default 50.
    - ``cursor``: opaque cursor from the previous response's ``next_cursor``.
      A cursor referencing a deleted document silently resumes from the next
      sort position — no 4xx is raised.
    - Returns 404 when the collection does not exist in the caller's namespace.
    - Returns 422 when ``limit`` is outside [1, 200].
    """
    ns: str = request.state.namespace
    search_store = request.app.state.search_store

    meta = await search_store.get_collection_meta(name, namespace=ns)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    pipeline = request.app.state.pipeline
    items, next_cursor, total = await pipeline.list_documents(
        name, limit, cursor=cursor, namespace=ns
    )
    return DocumentListResponse(
        items=[
            DocumentInfoItem(
                doc_id=doc.doc_id,
                source_path=doc.source_path,
                chunk_count=doc.chunk_count,
                indexed_at=doc.indexed_at,
                scopes=doc.scopes,
            )
            for doc in items
        ],
        next_cursor=next_cursor,
        total=total,
    )


_SECONDS_PER_HOUR: int = 3_600


@router.get(
    "/{name}/expiring",
    response_model=ExpiringChunksResponse,
    responses={**_ERROR_401_404, 422: {"model": ErrorDetail}},
)
async def get_expiring_chunks(
    name: str,
    request: Request,
    within_hours: int = Query(ge=1, le=8760),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> ExpiringChunksResponse:
    """List chunks expiring within *within_hours* hours (E2a BE-4).

    - ``within_hours``: 1–8760 (1 hour to 1 year); required.
    - ``limit``: 1–200, default 50.
    - ``cursor``: opaque cursor from a previous response's ``next_cursor``.
    - Returns 404 when the collection does not exist in the caller's namespace.
    - Already-expired chunks are excluded; chunks with no expiry are excluded.
    """
    ns: str = request.state.namespace
    search_store = request.app.state.search_store

    meta = await search_store.get_collection_meta(name, namespace=ns)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    within_seconds = within_hours * _SECONDS_PER_HOUR
    items_raw, next_cursor = await search_store.query_expiring_chunks(
        name, ns, within_seconds, limit, cursor
    )
    items = [ExpiringChunkItem(**row) for row in items_raw]
    return ExpiringChunksResponse(items=items, next_cursor=next_cursor, page_count=len(items))


_ERROR_401_404_409_422_503 = {
    401: {"model": ErrorDetail},
    404: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    422: {"model": ErrorDetail},
    503: {"model": ErrorDetail, "description": STORE_BUSY_503_DESCRIPTION},
}


@router.patch("/{name}", response_model=CollectionDetail, responses=_ERROR_401_404_409_422_503)
async def patch_collection(name: str, body: PatchCollectionBody, request: Request) -> CollectionDetail | JSONResponse:
    """Update a collection's embedding model and/or default TTL. Triggers reindex when the model changes.

    The row is changed under the collection lock, on a row read under it: 404 when a concurrent DELETE dropped the
    collection meanwhile (no row is re-inserted), 503 ``store_busy`` when the lock is not acquired in time.
    """
    config: SearchConfig = request.app.state.config
    search_store = request.app.state.search_store
    job_store: JobStore = request.app.state.job_store
    ns: str = request.state.namespace

    # 404 if collection not in config
    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    # 404 if meta not found for this namespace (read again under the lock below, where it is authoritative)
    if await search_store.get_collection_meta(name, namespace=ns) is None:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    # Determine which fields were explicitly provided in the request body.
    payload = body.model_dump(exclude_unset=True)

    # Validate embedding model (only when field is explicitly set AND non-null) — 422 on ModelValidationError.
    # Before the lock: loading a model can take long, and the lock blocks every write to the collection.
    changes_embedding_model = "embedding_model" in payload and body.embedding_model is not None
    if changes_embedding_model:
        try:
            await validate_embedding_model(body.embedding_model)
        except ModelValidationError as e:
            raise HTTPException(status_code=422, detail=str(e))

    # Read-modify-write under the collection lock, on a row read under it: update_collection_meta upserts, so a write
    # from a row read before a concurrent DELETE dropped the collection would re-insert the dropped collection's row.
    try:
        async with search_store.locked_collection_meta(name, ns) as meta:
            if meta is None:
                raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

            # --- Embedding model logic ---
            stale_cleared = False
            if changes_embedding_model:
                # 409 guard: check if reindex job is still active
                if meta.reindex_job_id is not None:
                    job = job_store.get(meta.reindex_job_id)
                    if job is not None and job.status in (JobStatus.RUNNING, JobStatus.PENDING):
                        return JSONResponse(
                            {"detail": "reindex in progress; wait for job to complete before changing embedding model"},
                            status_code=409,
                        )
                    # Stale (DONE/FAILED/CANCELLED) — clear it (must persist below)
                    meta.reindex_job_id = None
                    stale_cleared = True

                requested = body.embedding_model
                active = meta.active_embedding_model
                pending = meta.pending_embedding_model

                # State machine
                if active == requested and pending is None:
                    # (a) no-op — persist only if stale reindex_job_id was cleared
                    if stale_cleared:
                        await search_store.update_collection_meta(meta, _locked_by_caller=True)
                elif pending == requested:
                    # (a') no-op — persist only if stale reindex_job_id was cleared
                    if stale_cleared:
                        await search_store.update_collection_meta(meta, _locked_by_caller=True)
                elif pending is not None and active == requested:
                    # (c) revert: clear pending and any stale reindex_job_id
                    meta.pending_embedding_model = None
                    meta.needs_reindex = False
                    meta.reindex_job_id = None
                    await search_store.update_collection_meta(meta, _locked_by_caller=True)
                else:
                    # (b) or (d): new model requested
                    chunk_count = await search_store.count_chunks(name, namespace=ns)
                    if chunk_count > 0:
                        meta.pending_embedding_model = requested
                        meta.needs_reindex = True
                    else:
                        meta.active_embedding_model = requested
                        meta.pending_embedding_model = None
                        meta.needs_reindex = False
                        meta.reindex_job_id = None
                    await search_store.update_collection_meta(meta, _locked_by_caller=True)
            elif "embedding_model" not in payload:
                # No embedding_model field at all — check for stale reindex_job_id (for TTL-only path)
                if meta.reindex_job_id is not None:
                    job = job_store.get(meta.reindex_job_id)
                    if job is None or job.status not in (JobStatus.RUNNING, JobStatus.PENDING):
                        meta.reindex_job_id = None
                        stale_cleared = True

            # --- default_ttl_seconds (explicit null = clear; absent = no change) ---
            if "default_ttl_seconds" in payload:
                meta = dataclasses.replace(meta, default_ttl_seconds=body.default_ttl_seconds)
                await search_store.update_collection_meta(meta, _locked_by_caller=True)
            elif stale_cleared and "embedding_model" not in payload:
                # Persist stale-cleared reindex_job_id even when no other field changed
                await search_store.update_collection_meta(meta, _locked_by_caller=True)
    except StoreBusyError:
        return store_busy_503(STORE_BUSY_DETAIL)

    # Build CollectionDetail response
    state_store = request.app.state.state_store
    resolved = path_to_name[name]
    status = _collection_status(config, state_store, name)

    last_indexed: str | None = None
    try:
        state = state_store.read()
        if state and name in state.collections:
            cp = state.collections[name]
            last_indexed = getattr(cp, "completed_at", None)
    except Exception:  # noqa: BLE001, S110 — last_indexed is best-effort detail; any state-store read failure leaves it None
        pass

    doc_count = 0
    chunk_count = 0
    centroid_present = bool(meta.centroid)
    acl_protected = 0
    acl_open = 0
    try:
        doc_count = await search_store.count_documents(name)
    except Exception:  # noqa: BLE001 — stat is best-effort detail data; any store failure falls back to 0
        doc_count = 0
    try:
        chunk_count = await search_store.count_chunks(name, namespace=ns)
    except Exception:  # noqa: BLE001 — stat is best-effort detail data; any store failure falls back to 0
        chunk_count = 0
    try:
        acl_protected, acl_open = await search_store.get_acl_stats(name)
    except Exception:  # noqa: BLE001 — stat is best-effort detail data; any store failure falls back to 0
        acl_protected, acl_open = 0, 0

    return CollectionDetail(
        name=name,
        path=resolved,
        description=meta.description or "",
        doc_count=doc_count,
        chunk_count=chunk_count,
        status=status,
        active_embedding_model=resolve_active_model(meta, config),
        pending_embedding_model=meta.pending_embedding_model,
        needs_reindex=meta.needs_reindex,
        reindex_job_id=meta.reindex_job_id,
        centroid_present=centroid_present,
        last_indexed=last_indexed,
        namespace=meta.namespace,
        acl_protected_count=acl_protected,
        acl_open_count=acl_open,
        default_ttl_seconds=meta.default_ttl_seconds,
        schema_version=meta.schema_version,
    )


_ERROR_REINDEX = {
    **_ERROR_401_404,
    409: {"model": ErrorDetail, "description": "A reindex of this collection is already in progress"},
    500: {
        "model": ErrorDetail,
        "description": (
            "Creating the reindex job or recording it on the collection meta failed "
            f'(body {{"detail": "{INTERNAL_ERROR_DETAIL}"}})'
        ),
    },
    503: {
        "model": ErrorDetail,
        "description": (
            "Service not ready — the embedder cache, pipeline or search store is not initialised "
            f'(body {{"detail": "{SERVICE_NOT_READY_DETAIL}"}}). {STORE_BUSY_503_DESCRIPTION}'
        ),
    },
}


def _reindex_in_progress_409(job_store: JobStore, meta: CollectionMeta) -> JSONResponse | None:
    """The 409 refusing a reindex while ``meta.reindex_job_id`` names a PENDING/RUNNING job, else ``None`` (a stale
    pointer — job missing or terminal — is overwritten by the new job's id)."""
    if meta.reindex_job_id is None:
        return None
    existing_job = job_store.get(meta.reindex_job_id)
    if existing_job is not None and existing_job.status in {JobStatus.RUNNING, JobStatus.PENDING}:
        return JSONResponse({"detail": "reindex already in progress"}, status_code=409)
    return None


def _reindex_not_recorded_500(job_store: JobStore, job: ReindexJob, name: str, error: Exception) -> JSONResponse:
    """After the ``reindex_job_id`` write failed with *error*: log it, end *job* FAILED (best-effort, it never runs)
    and build the sanitized 500."""
    logger.error("Failed to record the reindex job %s on collection %s", job.job_id, name, exc_info=error)
    _persist_job_status(job_store, job.job_id, JobStatus.FAILED, context=_REINDEX_COLLECTION_LOG_CONTEXT, error=REINDEX_FAILED_DETAIL)
    return JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)


@router.post("/{name}/reindex", status_code=202, response_model=JobResponse, responses=_ERROR_REINDEX)
async def reindex_collection(name: str, request: Request) -> JobResponse | JSONResponse:
    """Start a reindex job for an existing collection. 404 if not found."""
    config: SearchConfig = request.app.state.config
    store: JobStore = request.app.state.job_store
    search_store = request.app.state.search_store
    ns: str = request.state.namespace

    # Readiness is checked first, before anything is read or written: no job, no meta pointer on a 503, and no 404
    # or 409 for a request that cannot run until the service is ready.
    embedder_cache = getattr(request.app.state, "embedder_cache", None)
    pipeline_obj = getattr(request.app.state, "pipeline", None)
    service_ready = embedder_cache is not None and pipeline_obj is not None and search_store is not None
    if not service_ready:
        return JSONResponse({"detail": SERVICE_NOT_READY_DETAIL}, status_code=503)

    # A collection is visible (and reindexable) if it has a meta row in this namespace — which a single-file
    # POST /ingest writes even without adding a config path (create_collection_job answers 404 without one).
    job = await create_collection_job(
        search_store,
        name,
        ns,
        job_store=store,
        log_context=_REINDEX_COLLECTION_LOG_CONTEXT,
        not_found_detail=f"Collection {name!r} not found",
        refuse_duplicate=lambda meta: _reindex_in_progress_409(store, meta),
        create_job=lambda meta: store.create_reindex(
            name, namespace=ns, target_embedding_model=meta.pending_embedding_model
        ),
        pointer_field="reindex_job_id",
        on_pointer_write_failure=lambda job, error: _reindex_not_recorded_500(store, job, name, error),
    )
    if isinstance(job, JSONResponse):
        return job

    resolved = _all_collection_paths(config).get(name, "")
    task = asyncio.create_task(
        _reindex_task(
            job_id=job.job_id,
            store=search_store,
            job_store=store,
            embedder_cache=embedder_cache,
            pipeline=pipeline_obj,
            collection=name,
            namespace=ns,
            # None for a meta-only collection (single-file POST /ingest) with no
            # configured source directory — the task must not re-scan the CWD.
            collection_path=Path(resolved) if resolved else None,
            config=config,
        )
    )
    request.app.state._background_tasks.add(task)
    task.add_done_callback(request.app.state._background_tasks.discard)

    return JobResponse(**job_to_dict(job))


async def _migration_task(
    job: MigrationJob,
    job_store: JobStore,
    search_store: object,
    spec: MigrationSpec | None = None,
) -> None:
    """Coroutine that drives a single REWRITE MigrationJob to completion.

    The caller is responsible for ensuring the job is already in RUNNING state
    before invoking this coroutine.  Both the route handler and the scheduler
    dispatch function satisfy this contract:

    - **Route path**: ``migrate_collection`` creates the job directly as
      RUNNING (``create_migration(status=RUNNING)``), then passes it to
      ``asyncio.create_task``.
    - **Scheduler path**: ``JobScheduler._tick()`` calls
      ``store.transition({QUEUED}, RUNNING)`` before calling ``dispatch_fn``,
      which creates this task.

    Lifecycle (caller sets RUNNING → this task drives to terminal):
      1. Resolve spec from ``pending_migrations()`` when ``spec`` is ``None``
         (scheduler resume path).
      2. call ``apply_rewrite_migration`` with ``progress_cb``
      3. Transition to DONE with ``result.migrated_chunks`` on success.
      4. Transition to FAILED on any exception: the ``NoPendingRewriteMigrationError``
         message, else ``MIGRATION_FAILED_DETAIL``.

    When ``spec`` is ``None`` (scheduler resume path), the pending migrations
    list is fetched from the store and the first REWRITE spec is used.
    """
    job_id = job.job_id

    def _progress_cb(processed: int, total: int, phase: str) -> None:
        try:
            job_store.update_progress(job_id, processed, total, phase)
        except (KeyError, OSError):
            logger.warning("_migration_task: could not update progress for job %s", job_id)

    def _mark_failed(error: str) -> None:
        # Best-effort: any persist failure is logged with its traceback, never raised.
        _persist_job_status(job_store, job_id, JobStatus.FAILED, context="_migration_task", error=error)

    try:
        if spec is None:
            pending = await search_store.pending_migrations(job.collection, job.namespace)  # type: ignore[attr-defined]
            rewrite_pending = [s for s in pending if s.kind == MigrationKind.REWRITE]
            if not rewrite_pending:
                raise NoPendingRewriteMigrationError(
                    f"MigrationJob {job_id}: no pending REWRITE migration found for "
                    f"collection {job.collection!r}"
                )
            spec = rewrite_pending[0]

        migrated = await search_store.apply_rewrite_migration(  # type: ignore[attr-defined]
            job.collection,
            job.namespace,
            spec,
            progress_cb=_progress_cb,
        )
        job_store.update(
            job_id,
            status=JobStatus.DONE,
            result={"migrated_chunks": migrated},
            migrations_applied=[spec.name],
        )
    except NoPendingRewriteMigrationError as exc:
        # Curated, expected outcome: no traceback.
        logger.warning("_migration_task: job %s failed: %s", job_id, exc)
        _mark_failed(str(exc))
    except Exception:
        logger.exception("_migration_task: job %s failed", job_id)
        _mark_failed(MIGRATION_FAILED_DETAIL)


@router.get("/{name}/migrations/pending", response_model=MigrationPendingResponse, responses=_ERROR_401_404)
async def get_migrations_pending(name: str, request: Request) -> MigrationPendingResponse:
    """Return the list of pending migrations for a collection.

    Returns 404 if the collection is not found in the caller's namespace.
    Returns an empty ``pending`` list when the collection schema is current.
    """
    config: SearchConfig = request.app.state.config
    search_store = request.app.state.search_store
    ns: str = request.state.namespace

    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    meta = await search_store.get_collection_meta(name, namespace=ns)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    pending = await search_store.pending_migrations(name, ns)
    return MigrationPendingResponse(
        collection=name,
        pending=[
            MigrationSpecSchema(
                name=spec.name,
                kind=spec.kind.value,
                description=spec.description,
                introduced_at=spec.introduced_at,
            )
            for spec in pending
        ],
        schema_version=meta.schema_version,
    )


_MIGRATE_ERROR_RESPONSES = {
    401: {"model": ErrorDetail},
    404: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    422: {"model": ErrorDetail},
    500: {"model": ErrorDetail},
    503: {"model": ErrorDetail, "description": STORE_BUSY_503_DESCRIPTION},
}


async def _apply_in_place_or_refusal(
    search_store: SearchStore, name: str, ns: str, specs: list[MigrationSpec]
) -> JSONResponse | None:
    """Apply the in-place *specs* to collection *name*; the response refusing the request, or ``None`` once applied.

    404 when a concurrent DELETE dropped the collection meanwhile (``apply_in_place_migrations`` finds no row to
    record ``schema_version`` on), 503 ``store_busy`` when the collection lock is not acquired in time, else the
    sanitized 500 (the failure is logged, never sent).
    """
    try:
        applied = await search_store.apply_in_place_migrations(name, ns, specs)
    except StoreBusyError:
        return store_busy_503(STORE_BUSY_DETAIL)
    except Exception:
        logger.exception("apply_in_place_migrations failed for collection %r", name)
        return JSONResponse({"detail": "migration failed: internal error"}, status_code=500)
    if not applied:
        return JSONResponse({"detail": f"Collection {name!r} not found"}, status_code=404)
    return None


@router.post(
    "/{name}/migrate",
    response_model=MigrateInPlaceResponse | MigrationPendingResponse | JobResponse,
    responses=_MIGRATE_ERROR_RESPONSES,
)
async def migrate_collection(
    name: str, body: MigrateRequest, request: Request
) -> MigrateInPlaceResponse | MigrationPendingResponse | JobResponse | JSONResponse:
    """Apply pending migrations or report pending list (dry-run).

    **dry_run=true:** returns the same ``MigrationPendingResponse`` body as
    ``GET /migrations/pending`` without applying anything.

    **In-place-only migrations:** applies synchronously, returns 200 with
    ``{migrations_applied: [...]}``.  No ``MigrationJob`` is created.

    **Rewrite migrations:** requires ``backup_confirmed: true``; creates a
    ``MigrationJob`` directly as RUNNING in one write, spawns its task, and
    returns 202 with ``{job_id, status: "RUNNING"}``.  The job is never QUEUED,
    so the scheduler never runs it.

    **export_rebuild migrations:** always returns 422 — D3 does not execute
    export_rebuild; operators must re-ingest manually.

    Returns 404 if the collection is not found in the caller's namespace, also when a concurrent ``DELETE`` dropped
    it while its in-place migrations ran (no ``schema_version`` is written then).
    Returns 409 if a ReindexJob is active for the same collection.
    Returns 422 if rewrite/export_rebuild migrations are pending without
    ``backup_confirmed: true``, or if export_rebuild migrations are present.
    Returns 500 (sanitized ``{"detail": "internal error"}``) if the rewrite's
    job could not be saved.
    Returns 503 ``store_busy`` if the in-place migrations' ``schema_version`` write could not take the collection
    lock in time.
    """
    config: SearchConfig = request.app.state.config
    search_store = request.app.state.search_store
    job_store: JobStore = request.app.state.job_store
    ns: str = request.state.namespace

    # Gate 1: config-path check (matches all sibling /{name} routes).
    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    # Gate 2: namespace-scoped meta check.
    meta = await search_store.get_collection_meta(name, namespace=ns)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    pending = await search_store.pending_migrations(name, ns)

    # dry_run: return pending list, no side effects.
    if body.dry_run:
        return MigrationPendingResponse(
            collection=name,
            pending=[
                MigrationSpecSchema(
                    name=spec.name,
                    kind=spec.kind.value,
                    description=spec.description,
                    introduced_at=spec.introduced_at,
                )
                for spec in pending
            ],
            schema_version=meta.schema_version,
        )

    # Classify pending specs by kind.
    in_place_specs = [s for s in pending if s.kind == MigrationKind.IN_PLACE]
    rewrite_specs = [s for s in pending if s.kind == MigrationKind.REWRITE]
    export_rebuild_specs = [s for s in pending if s.kind == MigrationKind.EXPORT_REBUILD]

    # Gate: export_rebuild migrations cannot be executed by D3.
    if export_rebuild_specs:
        names = ", ".join(s.name for s in export_rebuild_specs)
        raise HTTPException(
            status_code=422,
            detail=(
                f"export_rebuild migrations cannot be applied automatically: {names}. "
                "Operators must re-ingest the collection manually (D5)."
            ),
        )

    # Gate: rewrite migrations require backup_confirmed.
    if rewrite_specs and not body.backup_confirmed:
        raise HTTPException(
            status_code=422,
            detail=(
                "backup_confirmed: true is required before applying rewrite migrations. "
                "Confirm you have a backup of the collection before proceeding."
            ),
        )

    # Rewrite path: create a MigrationJob and dispatch asynchronously.
    if rewrite_specs:
        # Gate: reject if a ReindexJob is active for this collection.
        if meta.reindex_job_id is not None:
            existing_reindex = job_store.get(meta.reindex_job_id)
            if existing_reindex is not None and existing_reindex.status in {
                JobStatus.RUNNING, JobStatus.QUEUED, JobStatus.PENDING
            }:
                return JSONResponse(
                    {"detail": "reindex in progress; wait for reindex job to complete before migrating"},
                    status_code=409,
                )

        # Apply in-place migrations before queuing the rewrite job.
        if in_place_specs:
            refusal = await _apply_in_place_or_refusal(search_store, name, ns, in_place_specs)
            if refusal is not None:
                return refusal

        # Create the MigrationJob directly as RUNNING in one write. The route dispatches
        # the task itself (not via the scheduler), so the job must never be QUEUED —
        # in memory or on disk — or the scheduler would run a second _migration_task
        # for it (or, after a restart, run one whose caller was answered 500).
        try:
            running_job = job_store.create_migration(
                collection=name,
                kind=MigrationKind.REWRITE,
                backup_confirmed=body.backup_confirmed,
                namespace=ns,
                status=JobStatus.RUNNING,
            )
        except Exception:
            logger.exception("migrate_collection: failed to persist a rewrite MigrationJob for %r", name)
            return JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)

        # Use the first rewrite spec (D3 scope: one rewrite migration at a time).
        spec = rewrite_specs[0]

        task = asyncio.create_task(
            _migration_task(
                job=running_job,
                job_store=job_store,
                search_store=search_store,
                spec=spec,
            )
        )
        request.app.state._background_tasks.add(task)
        task.add_done_callback(request.app.state._background_tasks.discard)

        return JSONResponse(
            job_to_dict(running_job),
            status_code=202,
        )

    # In-place-only path: apply synchronously.
    refusal = await _apply_in_place_or_refusal(search_store, name, ns, in_place_specs)
    if refusal is not None:
        return refusal

    return MigrateInPlaceResponse(
        migrations_applied=[s.name for s in in_place_specs],
    )


# ---------------------------------------------------------------------------
# Metadata reindex endpoint (CSP120 BE-4)
# ---------------------------------------------------------------------------

# Active job statuses for the metadata_reindex_job_id 409 guard.
# Mirrors _REBUILD_ACTIVE_STATUSES from routes_graph.py: the route creates the
# job RUNNING and runs it itself; DELETE /jobs/{id} moves it to CANCELLING
# without stopping the task, so that still counts. QUEUED/PENDING are absent:
# nothing schedules a MetadataReindexJob, so such a row (written by an older
# server) never runs — it is stale: lazily cleared and marked FAILED, not a 409.
_REINDEX_METADATA_ACTIVE_STATUSES = {JobStatus.RUNNING, JobStatus.CANCELLING}

_ERROR_REINDEX_METADATA = {
    401: {"model": ErrorDetail},
    404: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    500: {
        "model": ErrorDetail,
        "description": f'Creating the metadata reindex job failed (body {{"detail": "{INTERNAL_ERROR_DETAIL}"}})',
    },
    503: {
        "model": ErrorDetail,
        "description": (
            f'Service not ready — the search store is not initialised (body {{"detail": "{SERVICE_NOT_READY_DETAIL}"}}). '
            f"{STORE_BUSY_503_DESCRIPTION}"
        ),
    },
}


async def _reindex_metadata_task(
    job: MetadataReindexJob,
    job_store: JobStore,
    search_store: object,
    dry_run: bool,
    normalize_timestamps: bool,
) -> None:
    """Coroutine that drives a MetadataReindexJob to completion.

    The caller is responsible for ensuring the job is already in RUNNING state
    before invoking this coroutine (the route creates the job RUNNING in one
    write).

    Lifecycle (caller sets RUNNING -> this task drives to terminal):
      1. Call ``search_store.reindex_metadata(collection, dry_run=..., normalize_timestamps=...)``.
      2. In finally (also on cancellation): clear ``CollectionMeta.metadata_reindex_job_id``.
      3. Transition to DONE with all 5 ReindexResult fields on success.
      4. Transition to FAILED with ``METADATA_REINDEX_FAILED_DETAIL`` on any exception (the DONE write's included).

    The clear runs BEFORE the terminal write: DELETE /collections/{name} refuses only while this job is
    RUNNING/CANCELLING, and the clear (a meta read outside the collection lock, then an upsert) would re-create
    the meta row of a collection dropped after the terminal write.

    Note: does NOT pre-acquire the per-collection lock — ``reindex_metadata``
    already acquires ``lock_for(collection)`` internally (non-reentrant).
    """
    job_id = job.job_id

    result: ReindexResult | None = None  # set only when the reindex succeeded
    try:
        result = await search_store.reindex_metadata(  # type: ignore[attr-defined]
            job.collection,
            dry_run=dry_run,
            normalize_timestamps=normalize_timestamps,
        )
    except Exception:
        logger.exception("_reindex_metadata_task: job %s failed", job_id)
    finally:
        try:
            meta = await search_store.get_collection_meta(  # type: ignore[attr-defined]
                job.collection, namespace=job.namespace
            )
            if meta is not None and meta.metadata_reindex_job_id == job_id:
                meta.metadata_reindex_job_id = None
                await search_store.update_collection_meta(meta)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — best-effort pointer cleanup in a background task; failure is logged, never raised
            logger.warning(
                "_reindex_metadata_task: failed to clear metadata_reindex_job_id for job %s",
                job_id,
            )

    if result is not None and _record_metadata_reindex_done(job_store, job_id, result):
        return
    _persist_job_status(
        job_store, job_id, JobStatus.FAILED, context="_reindex_metadata_task", error=METADATA_REINDEX_FAILED_DETAIL
    )


def _record_metadata_reindex_done(job_store: JobStore, job_id: str, result: ReindexResult) -> bool:
    """Mark metadata reindex job *job_id* DONE with all 5 *result* fields; False (logged) when that write fails."""
    try:
        job_store.update(
            job_id,
            status=JobStatus.DONE,
            result={
                "processed": result.processed,
                "updated": result.updated,
                "skipped": result.skipped,
                "ts_normalized": result.ts_normalized,
                "warnings": result.warnings,
            },
        )
    except Exception:
        logger.exception("_reindex_metadata_task: could not record DONE for job %s", job_id)
        return False
    return True


@router.post(
    "/{name}/reindex-metadata",
    name="reindex_metadata",
    status_code=202,
    response_model=JobResponse,
    responses=_ERROR_REINDEX_METADATA,
)
async def reindex_metadata(
    name: str, body: ReindexMetadataRequest, request: Request
) -> JSONResponse:
    """Enqueue an async metadata reindex job for a collection — CSP120 BE-4.

    Follows the rebuild_communities pattern (``routes_graph.py``):
    validate -> create the job RUNNING in one write -> spawn task
    -> return 202 with the full JobResponse body, so the response reports RUNNING.

    Returns:
    - 202: JobResponse-shaped body, status RUNNING.
    - 404: Collection not found in config or meta record absent (also when a DELETE dropped it meanwhile).
    - 409: A metadata reindex is already in progress for this collection.
    - 500: The job could not be saved (sanitized ``{"detail": "internal error"}``).
    - 503: ``service not ready`` — no search store (checked after the config 404); or ``store_busy`` — the
      collection lock was not acquired within ``INGEST_LOCK_TIMEOUT_S``.
    """
    config: SearchConfig = request.app.state.config
    search_store = request.app.state.search_store
    job_store: JobStore = request.app.state.job_store
    ns: str = request.state.namespace

    # Guard 1: collection must be in the configured path list.
    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    # Readiness, before the first store access: without a search store no meta row, lock or job can be used. After
    # Guard 1, whose 404 holds whatever the readiness (a name absent from the config never becomes reindexable).
    if search_store is None:
        return JSONResponse({"detail": SERVICE_NOT_READY_DETAIL}, status_code=503)

    # Guard 2 (404: no namespace-scoped meta record) and Guard 3 (409: metadata_reindex_job_id names an active job)
    # run on the meta row read before the collection lock, then again under it; the job and its meta pointer are
    # created under the same lock (create_collection_job). A pointer naming a missing, terminal or legacy
    # QUEUED/PENDING job is stale and is overwritten by the new job's id (a dry-run leaves it).
    # A dry-run is a read-only preview and must write NOTHING to the meta table:
    # the non-atomic delete-then-add in update_collection_meta would otherwise open a
    # window during which a lock-free get_collection_meta sees no rows and the
    # collection becomes transiently unresolvable (S52).
    # Consequence: a dry-run never registers metadata_reindex_job_id, so it never makes another
    # submission's Guard 3 answer 409. A dry-run is still refused by Guard 3 (an instant 409) while a non-dry-run
    # metadata reindex is active. Concurrent dry-runs are harmless (read-only) but serialise: each waits for the
    # collection lock here (create_collection_job) — held for the whole run of an earlier dry-run's
    # SearchStore.reindex_metadata — and answers 503 store_busy if that wait times out.
    running_job = await create_collection_job(
        search_store,
        name,
        ns,
        job_store=job_store,
        log_context="reindex_metadata",
        not_found_detail=f"Collection {name!r} not found",
        refuse_duplicate=lambda meta: refuse_active_job_or_fail_legacy(
            job_store,
            meta.metadata_reindex_job_id,
            active_statuses=_REINDEX_METADATA_ACTIVE_STATUSES,
            conflict_detail="metadata reindex already in progress for this collection",
            log_context="reindex_metadata",
        ),
        # Created RUNNING in one write: the route runs the task itself, so the response never
        # reports QUEUED (mirrors rebuild_communities).
        create_job=lambda _meta: job_store.create_metadata_reindex(collection=name, namespace=ns, dry_run=body.dry_run),
        pointer_field=None if body.dry_run else "metadata_reindex_job_id",
    )
    if isinstance(running_job, JSONResponse):
        return running_job

    task = asyncio.create_task(
        _reindex_metadata_task(
            job=running_job,
            job_store=job_store,
            search_store=search_store,
            dry_run=body.dry_run,
            normalize_timestamps=body.normalize_timestamps,
        )
    )
    request.app.state._background_tasks.add(task)
    task.add_done_callback(request.app.state._background_tasks.discard)

    return JSONResponse(job_to_dict(running_job), status_code=202)
