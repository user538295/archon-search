"""GET /status endpoint — rich operator-facing service status."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import APIRouter, Request

from archon_search import code_enricher
from archon_search.config import SearchConfig
from archon_search.jobs.model import IngestJob, JobStatus
from archon_search.progress import compute_eta_seconds
from archon_search.server.readiness import collect_readiness
from archon_search.server.routes_collections import _all_collection_paths
from archon_search.server.schemas import (
    BackupStatusDetail,
    CodeParsersStatusDetail,
    CollectionBackupStatus,
    CollectionHealthEntry,
    ErrorDetail,
    GraphCollectionStats,
    GraphStatusDetail,
    HydeStatusDetail,
    MaintenanceStatusDetail,
    McpStatusDetail,
    ModelValidationStatus,
    RagFusionStatusDetail,
    SearchStatusDetail,
    StatusCollectionEntry,
    StatusResponse,
    TelemetryStatusDetail,
)
from archon_search.store import STORE_SCHEMA_VERSION, SearchStore

logger = logging.getLogger(__name__)
router = APIRouter()

# Collection names already logged at DEBUG for a missing config path. /status is a
# polled endpoint, so the fallback is logged once per name — not on every call.
_missing_path_logged: set[str] = set()


def _resolve_collection_path(paths: dict[str, str], name: str) -> str:
    """Return the collection's absolute path, or ``""`` (logged once at DEBUG) when absent.

    An absent name is an expected steady state — ad-hoc-ingested, collision-resolved
    (``docs_2``), or config-removed collections have no entry in
    ``_all_collection_paths(config)``. Resolved paths from that helper are never the empty
    string (``Path(p).resolve()`` yields at least the CWD), so ``is not None`` reliably
    distinguishes "present" from "absent". Caveat (plan Q1): the map is keyed by a
    collision-unaware basename, so two configured paths sharing a basename collapse
    last-write-wins — the surviving name may resolve to the *wrong* absolute path, not
    ``""``. That is an accepted limitation, not a guarantee.
    """
    path = paths.get(name)
    if path is not None:
        return path
    if name not in _missing_path_logged:
        _missing_path_logged.add(name)
        logger.debug("Status: no configured path for collection %r; reporting empty path", name)
    return ""

try:
    _VERSION = version("archon-search")
except PackageNotFoundError:
    _VERSION = "dev"


@router.get("/status", response_model=StatusResponse, responses={401: {"model": ErrorDetail}})
async def status(request: Request) -> StatusResponse:
    """Return rich operator-facing status including service info and per-collection progress."""
    config: SearchConfig = request.app.state.config
    ns: str = request.state.namespace

    # Service / process fields
    pid = os.getpid()

    # Resolve which collection names belong to the caller's namespace
    search_store = request.app.state.search_store
    all_meta = await search_store.get_all_collections_meta()
    ns_meta = [m for m in all_meta if m.namespace == ns]
    ns_names: set[str] = {m.name for m in ns_meta}
    meta_by_name = {m.name: m for m in ns_meta}

    # Load indexing state for collection progress (state_store created once in create_app)
    state_store = request.app.state.state_store
    state = state_store.read()

    collections_progress: dict = {}
    if state:
        for cname, cp in state.collections.items():
            eta = compute_eta_seconds(cp)
            collections_progress[cname] = {
                "status": str(cp.status),
                "processed_files": cp.processed_files,
                "total_files": cp.total_files,
                "error": cp.error,
                "error_count": cp.error_count,
                "eta_seconds": eta,
            }

    # ns_names (from the store) is the authoritative, namespace-scoped set of collections.
    # Config/pinned paths without a store meta row are not yet indexed and won't appear here.
    all_names: set[str] = ns_names

    # E1b BE-8 — fetch graph_store once; used per-collection inside the loop below.
    graph_store = getattr(request.app.state, "graph_store", None)

    # SPD — resolve config-derived absolute paths once; looked up by name in the loop.
    collection_paths = _all_collection_paths(config)

    collection_entries: list[StatusCollectionEntry] = []
    for name in sorted(all_names):
        progress = collections_progress.get(name)
        watching = config.watch
        col_meta = meta_by_name.get(name)

        # C2: warn when multilingual mode is on but untagged legacy chunks exist
        warning: str | None = None
        if config.multilingual:
            untagged = await search_store.count_untagged_language_chunks(name)
            if untagged > 0:
                warning = "multilingual=true but collection contains untagged chunks; re-ingest required"

        # E1b BE-8 — community stats per collection (C2, S4, S14)
        community_count = 0
        last_built_at: str | None = None
        if config.graph.enabled and graph_store is not None:
            try:
                count, built_at_dt = await graph_store.get_community_stats(name, ns=ns)
                community_count = count
                if built_at_dt is not None:
                    last_built_at = built_at_dt.isoformat()
            except Exception:
                logger.warning(
                    "community stats unavailable for collection %r; using defaults",
                    name,
                    exc_info=True,
                )

        # Live chunk count; a failure here must never 500 the status endpoint.
        chunk_count = 0
        try:
            chunk_count = await search_store.count_chunks(name, namespace=ns)
        except Exception:  # noqa: BLE001
            logger.warning("chunk count unavailable for collection %r; reporting 0", name, exc_info=True)

        collection_entries.append(
            StatusCollectionEntry(
                name=name,
                path=_resolve_collection_path(collection_paths, name),
                doc_count=col_meta.doc_count if col_meta else 0,
                chunk_count=chunk_count,
                status=progress["status"] if progress else "not_yet_indexed",
                watching=watching,
                eta_seconds=progress["eta_seconds"] if progress else None,
                processed_files=progress["processed_files"] if progress else 0,
                total_files=progress["total_files"] if progress else 0,
                error=progress.get("error") if progress else None,
                error_count=progress["error_count"] if progress else 0,
                needs_reindex=col_meta.needs_reindex if col_meta else False,
                warning=warning,
                community_count=community_count,
                last_built_at=last_built_at,
            )
        )

    # D3 BE-15 — count collections whose schema_version is behind STORE_SCHEMA_VERSION.
    # Uses already-fetched ns_meta to avoid N additional DB round-trips.
    collections_schema_behind = sum(
        1 for m in ns_meta if m.schema_version < STORE_SCHEMA_VERSION
    )

    readiness = await collect_readiness(request.app.state, state)
    backup_detail = _build_backup_status(request, config, ns, sorted(ns_names))
    maintenance_detail = await _build_maintenance_status(
        request, config, ns, request.app.state.search_store, sorted(ns_names)
    )
    model_validation = _build_model_validation_status(request)
    telemetry_detail = _build_telemetry_status(request, config)
    mcp_detail = _build_mcp_status(request, config)
    hyde_detail = _build_hyde_status(request, config)
    rag_fusion_detail = _build_rag_fusion_status(request, config)
    failed_expired_count = _count_failed_expired_ingest_jobs(request, ns)
    search_detail = _build_search_status(config)
    graph_detail = await _build_graph_status(request, config, sorted(ns_names))
    code_parsers_detail = _build_code_parsers_status(config)
    return StatusResponse(
        running=True,
        pid=pid,
        version=_VERSION,
        collections=collection_entries,
        readiness=readiness,
        backup=backup_detail,
        maintenance=maintenance_detail,
        store_schema_version=STORE_SCHEMA_VERSION,
        collections_schema_behind=collections_schema_behind,
        model_validation=model_validation,
        telemetry=telemetry_detail,
        mcp=mcp_detail,
        search=search_detail,
        hyde=hyde_detail,
        rag_fusion=rag_fusion_detail,
        failed_expired_ingest_count=failed_expired_count,
        graph=graph_detail,
        code_parsers=code_parsers_detail,
    )


def _count_failed_expired_ingest_jobs(request: Request, ns: str) -> int:
    """Count exact base-class IngestJob instances with status FAILED_EXPIRED in the caller's namespace.

    Only counts exact ``IngestJob`` instances (``type(j) is IngestJob``) — the same
    predicate used by ``MaintenanceLoop`` when it transitions FAILED jobs to
    FAILED_EXPIRED.  Subclasses (``ExportJob``, ``ImportJob``, etc.) are excluded.

    Reads the job store synchronously; the store's ``list()`` method is safe to call
    without an ``await`` — asyncio's single-thread scheduling guarantee prevents
    concurrent coroutine mutation during iteration.
    """
    job_store = getattr(request.app.state, "job_store", None)
    if job_store is None:
        return 0
    return sum(
        1
        for j in job_store.list()
        if type(j) is IngestJob
        and j.status == JobStatus.FAILED_EXPIRED
        and j.namespace == ns
    )


def _build_search_status(config: SearchConfig) -> SearchStatusDetail:
    """Return the search config status sub-object (E0c BE-4 / C4).

    Always returns a non-null ``SearchStatusDetail`` populated from the live
    ``SearchConfig`` values — either the TOML-configured values or defaults.
    """
    return SearchStatusDetail(
        max_fanout=config.max_fanout,
        top_k_max=config.top_k_max,
    )


def _build_telemetry_status(request: Request, config: SearchConfig) -> TelemetryStatusDetail | None:
    """Return the telemetry status sub-object when ``telemetry.enabled = true``, or ``None`` otherwise (D8 BE-5 / C3).

    ``hash_doc_ids_enabled`` is ``True`` only when the config flag is on **and**
    ``app.state.salt_bytes`` is non-null — guards the salt-unreadable fallback (S5).
    """
    if not config.telemetry.enabled:
        return None
    salt_bytes = getattr(request.app.state, "salt_bytes", None)
    hash_doc_ids_enabled = config.telemetry.hash_doc_ids and salt_bytes is not None
    return TelemetryStatusDetail(enabled=True, hash_doc_ids_enabled=hash_doc_ids_enabled)


def _build_mcp_status(request: Request, config: SearchConfig) -> McpStatusDetail | None:
    """Return the MCP status sub-object when ``mcp.enabled = true``, or ``None`` otherwise (D9 C3)."""
    if not config.mcp.enabled:
        return None
    bound = getattr(request.app.state, "mcp_bound", False)
    bind_address = f"{config.host}:{config.port}/mcp" if bound else None
    return McpStatusDetail(enabled=True, bind_address=bind_address)


def _build_hyde_status(request: Request, config: SearchConfig) -> HydeStatusDetail | None:
    """Return the HyDE status sub-object when ``hyde.enabled = true``, or ``None`` otherwise (E0b BE-8 / C2).

    ``key_available`` delegates to ``HyDEGenerator.is_key_available()`` so the
    env-var check stays in the Use Cases layer.  The generator is unconditionally
    instantiated in ``create_app``; the ``getattr`` guard keeps the endpoint
    resilient to alternative app factories used in tests.
    """
    if not config.hyde.enabled:
        return None
    generator = getattr(request.app.state, "hyde_generator", None)
    if generator is None:
        return None
    return HydeStatusDetail(
        key_available=generator.is_key_available(),
        provider=config.hyde.provider,
    )


def _build_rag_fusion_status(request: Request, config: SearchConfig) -> RagFusionStatusDetail | None:
    """Return the RAG Fusion status sub-object when ``rag_fusion.enabled = true``, or ``None`` otherwise (E0b BE-8 / C2).

    ``key_available`` delegates to ``RAGFusionGenerator.is_key_available()`` so the
    env-var check stays in the Use Cases layer.  The generator is unconditionally
    instantiated in ``create_app``; the ``getattr`` guard keeps the endpoint
    resilient to alternative app factories used in tests.
    """
    if not config.rag_fusion.enabled:
        return None
    generator = getattr(request.app.state, "rag_fusion_generator", None)
    if generator is None:
        return None
    return RagFusionStatusDetail(
        key_available=generator.is_key_available(),
        provider=config.rag_fusion.provider,
    )


def _build_model_validation_status(request: Request) -> ModelValidationStatus | None:
    """Mirror the background validation result into the ``model_validation`` sub-object.

    Returns ``None`` while the background task has not yet completed (D6 S2) — the
    ``getattr`` guard also keeps the endpoint resilient to app factories that never
    set ``app.state.model_validation`` (mirrors ``_build_maintenance_status``).
    """
    result = getattr(request.app.state, "model_validation", None)
    if result is None:
        return None
    return ModelValidationStatus(
        embedder_ok=result.embedder_ok,
        reranker_ok=result.reranker_ok,
        provider_warnings=list(result.provider_warnings),
        validated_at=result.validated_at,
    )


def _build_backup_status(
    request: Request, config: SearchConfig, ns: str, ns_collection_names: list[str]
) -> BackupStatusDetail | None:
    """Populate the ``backup`` sub-object for the caller's namespace.

    Returns ``None`` when no ``BackupLoop`` is wired on ``app.state`` — this
    keeps the endpoint resilient to alternative app factories used in tests.
    """
    backup_loop = getattr(request.app.state, "backup_loop", None)
    if backup_loop is None:
        return None

    interval_hours = config.backup.interval_hours
    enabled = interval_hours > 0
    last_tick_at = backup_loop._last_tick_at
    next_run_at: str | None = None
    if last_tick_at and interval_hours > 0:
        try:
            next_run_at = (
                datetime.fromisoformat(last_tick_at) + timedelta(hours=interval_hours)
            ).isoformat()
        except ValueError:
            # Defensive: a corrupt last_tick_at should not 500 the status endpoint.
            logger.warning("Status: unparseable backup last_tick_at=%r", last_tick_at)
            next_run_at = None

    state_map = backup_loop._load_state()
    ns_dir = Path(config.backup.output_dir) / ns

    collection_status: list[CollectionBackupStatus] = []
    for col in ns_collection_names:
        last_backup_at = state_map.get(f"{ns}/{col}")
        if ns_dir.exists():
            archive_count = len(list(ns_dir.glob(f"{col}.backup.*.tar.gz")))
        else:
            archive_count = 0
        collection_status.append(
            CollectionBackupStatus(
                collection=col,
                namespace=ns,
                last_backup_at=last_backup_at,
                archive_count=archive_count,
            )
        )

    return BackupStatusDetail(
        enabled=enabled,
        interval_hours=interval_hours,
        last_tick_at=last_tick_at,
        next_run_at=next_run_at,
        collections_excluded=list(config.backup.exclude),
        collection_status=collection_status,
    )


async def _build_graph_status(
    request: Request, config: SearchConfig, ns_collection_names: list[str]
) -> GraphStatusDetail | None:
    """Return the graph status sub-object when ``graph.enabled = true``, or ``None`` otherwise (E1a FE-1 / C2).

    Reads live node/edge counts from ``GraphStore`` per collection per call;
    no server-side caching in E1a (see Q7 in the team plan).  Returns ``None``
    when graph is disabled **or** when no ``GraphStore`` is wired on
    ``app.state`` — the latter keeps the endpoint resilient to alternative app
    factories used in tests.

    BE-8: reads ``communities_invalidated`` per collection and ``stale_mention_count``
    from the maintenance state file via the shared ``_load_state`` helper on the
    ``MaintenanceLoop`` instance.  Falls back to defaults (``False`` / ``0``) when no
    ``MaintenanceLoop`` is wired on ``app.state``.
    """
    if not config.graph.enabled:
        return None
    graph_store = getattr(request.app.state, "graph_store", None)
    if graph_store is None:
        return None

    ns: str = request.state.namespace

    # BE-8 — read maintenance state once; shared with _build_maintenance_status
    maintenance_loop = getattr(request.app.state, "maintenance_loop", None)
    if maintenance_loop is not None:
        state = maintenance_loop._load_state()
    else:
        state = {}
    collection_health: dict = state.get("collection_health", {}) if state else {}
    stale_mention_count: int = state.get("stale_mention_count", 0) if state else 0

    collection_stats: list[GraphCollectionStats] = []
    for col in ns_collection_names:
        node_count = await graph_store.node_count(col, ns=ns)
        edge_count = await graph_store.edge_count(col, ns=ns)
        col_key = f"{ns}/{col}"
        col_health_entry: dict = collection_health.get(col_key, {})
        communities_invalidated: bool = bool(col_health_entry.get("communities_invalidated", False))

        # BE-7 health metrics — swallow errors per post-persist contract
        synonym_edge_count = 0
        singleton_node_pct = 0.0
        try:
            synonym_edge_count = await graph_store.count_synonym_edges(col, ns=ns)
        except Exception:
            logger.warning(
                "synonym_edge_count unavailable for collection %r; using 0", col, exc_info=True
            )
        try:
            singleton_node_pct = await graph_store.compute_singleton_pct(col, ns=ns)
        except Exception:
            logger.warning(
                "singleton_node_pct unavailable for collection %r; using 0.0", col, exc_info=True
            )

        # synonym_link_rate: fraction of edges that are synonym edges (0.0–1.0)
        if edge_count > 0:
            synonym_link_rate = synonym_edge_count / edge_count
        else:
            synonym_link_rate = 0.0

        collection_stats.append(
            GraphCollectionStats(
                collection=col,
                node_count=node_count,
                edge_count=edge_count,
                communities_invalidated=communities_invalidated,
                synonym_edge_count=synonym_edge_count,
                singleton_node_pct=singleton_node_pct,
                synonym_link_rate=synonym_link_rate,
            )
        )

    return GraphStatusDetail(
        enabled=True,
        backend_threshold_edges=config.graph.backend_threshold_edges,
        collections=collection_stats,
        stale_mention_count=stale_mention_count,
    )


def _build_code_parsers_status(config: SearchConfig) -> CodeParsersStatusDetail | None:
    """Return the code-parsers status sub-object when ``graph.enabled = true`` (E2g BE-11 / S9).

    Reuses ``code_enricher.has_missing_code_parsers()`` — a lazy, process-global
    per-extension cache populated the first time ``_get_grammar(ext)`` is called
    for a code file and finds no available tree-sitter grammar. Returns ``None``
    when graph is disabled (this soft-degrade check is only meaningful alongside
    code graphing).
    """
    if not config.graph.enabled:
        return None
    missing_extensions = code_enricher.missing_code_parser_extensions()
    degraded = code_enricher.has_missing_code_parsers()
    return CodeParsersStatusDetail(
        degraded=degraded,
        missing_extensions=missing_extensions,
        message=(
            "tree-sitter grammar missing for one or more code extensions; "
            "install archon-search[code] to enable code graphing"
            if degraded
            else ""
        ),
    )


async def _build_maintenance_status(
    request: Request,
    config: SearchConfig,
    ns: str,
    store: SearchStore,
    ns_collection_names: list[str],
) -> MaintenanceStatusDetail | None:
    """Populate the ``maintenance`` sub-object for the caller's namespace.

    Returns ``None`` when no ``MaintenanceLoop`` is wired on ``app.state`` — this
    keeps the endpoint resilient to alternative app factories used in tests.

    Namespace scoping: ``collection_health`` entries are filtered to those whose
    ``{namespace}/{collection}`` key starts with ``{ns}/``, following the precedent
    in ``_build_backup_status`` which scopes backup status to the caller's namespace.

    E2a BE-8: ``expired_chunk_count`` is the live sum of ``store.count_expired_chunks()``
    across all collection tables belonging to the caller's namespace.  Note: counts are
    table-wide — not namespace-scoped within a shared table.  For single-namespace
    deployments this is exact; in multi-tenant deployments where two namespaces share
    a collection name, the count includes all namespaces' expired chunks for that table.
    ``last_expired_pruned_at`` is read from the maintenance state file (written by
    ``MaintenanceLoop`` after each prune run; null until the first prune).
    """
    maintenance_loop = getattr(request.app.state, "maintenance_loop", None)
    if maintenance_loop is None:
        return None

    interval_hours = config.maintenance.interval_hours
    enabled = interval_hours > 0
    centroid_threshold = config.centroid_recompute_threshold

    state = maintenance_loop._load_state()
    last_run_at: str | None = state.get("last_run_at")
    next_run_at: str | None = state.get("next_run_at")
    last_expired_pruned_at: str | None = state.get("last_expired_pruned_at")
    last_graph_gc_at: str | None = state.get("last_graph_gc_at")
    all_health: dict = state.get("collection_health", {})
    if not isinstance(all_health, dict):
        all_health = {}

    # Namespace-scope: only include entries whose key starts with "{ns}/"
    ns_prefix = f"{ns}/"
    collection_health: list[CollectionHealthEntry] = []
    for key, entry in all_health.items():
        if not key.startswith(ns_prefix):
            continue
        # Extract collection name from the "{ns}/{col}" key
        col_name = key[len(ns_prefix):]
        collection_health.append(
            CollectionHealthEntry(
                collection=col_name,
                fts_optimized_at=entry.get("fts_optimized_at"),
                orphans_removed_last_run=entry.get("orphans_removed_last_run", 0),
                last_retry_at=entry.get("last_retry_at"),
                last_error=entry.get("last_error"),
                mutations_since_recompute=entry.get("mutations_since_recompute", 0),
                centroid_recompute_threshold=centroid_threshold,
                meta_chunk_count=entry.get("meta_chunk_count", 0),
                expired_chunks_removed_last_run=entry.get(
                    "expired_chunks_removed_last_run", 0
                ),
                communities_invalidated=bool(
                    entry.get("communities_invalidated", False)
                ),
            )
        )

    # E2a BE-8 — live count of expired chunks across all collections in this namespace
    expired_chunk_count = 0
    for col in ns_collection_names:
        try:
            expired_chunk_count += await store.count_expired_chunks(col, ns)
        except Exception:
            logger.warning(
                "expired_chunk_count unavailable for collection %r: store.count_expired_chunks failed",
                col,
                exc_info=True,
            )

    return MaintenanceStatusDetail(
        enabled=enabled,
        interval_hours=interval_hours,
        last_run_at=last_run_at,
        next_run_at=next_run_at,
        collection_health=collection_health,
        expired_chunk_count=expired_chunk_count,
        last_expired_pruned_at=last_expired_pruned_at,
        last_graph_gc_at=last_graph_gc_at,
    )
