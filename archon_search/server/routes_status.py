"""GET /status endpoint — rich operator-facing service status."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import APIRouter, Request

from archon_search.config import SearchConfig
from archon_search.progress import compute_eta_seconds
from archon_search.server.readiness import collect_readiness
from archon_search.server.schemas import (
    BackupStatusDetail,
    CollectionBackupStatus,
    CollectionHealthEntry,
    ErrorDetail,
    HydeStatusDetail,
    MaintenanceStatusDetail,
    McpStatusDetail,
    ModelValidationStatus,
    RagFusionStatusDetail,
    StatusCollectionEntry,
    StatusResponse,
    TelemetryStatusDetail,
)
from archon_search.store import STORE_SCHEMA_VERSION

logger = logging.getLogger(__name__)
router = APIRouter()

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

        collection_entries.append(
            StatusCollectionEntry(
                name=name,
                path="",  # path not yet populated from store
                doc_count=0,
                chunk_count=0,
                status=progress["status"] if progress else "not_yet_indexed",
                watching=watching,
                eta_seconds=progress["eta_seconds"] if progress else None,
                processed_files=progress["processed_files"] if progress else 0,
                total_files=progress["total_files"] if progress else 0,
                error=progress.get("error") if progress else None,
                error_count=progress["error_count"] if progress else 0,
                needs_reindex=col_meta.needs_reindex if col_meta else False,
                warning=warning,
            )
        )

    # D3 BE-15 — count collections whose schema_version is behind STORE_SCHEMA_VERSION.
    # Uses already-fetched ns_meta to avoid N additional DB round-trips.
    collections_schema_behind = sum(
        1 for m in ns_meta if m.schema_version < STORE_SCHEMA_VERSION
    )

    readiness = await collect_readiness(request.app.state, state)
    backup_detail = _build_backup_status(request, config, ns, sorted(ns_names))
    maintenance_detail = _build_maintenance_status(request, config, ns)
    model_validation = _build_model_validation_status(request)
    telemetry_detail = _build_telemetry_status(request, config)
    mcp_detail = _build_mcp_status(request, config)
    hyde_detail = _build_hyde_status(request, config)
    rag_fusion_detail = _build_rag_fusion_status(request, config)
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
        hyde=hyde_detail,
        rag_fusion=rag_fusion_detail,
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
    return HydeStatusDetail(key_available=generator.is_key_available())


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
    return RagFusionStatusDetail(key_available=generator.is_key_available())


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


def _build_maintenance_status(
    request: Request, config: SearchConfig, ns: str
) -> MaintenanceStatusDetail | None:
    """Populate the ``maintenance`` sub-object for the caller's namespace.

    Returns ``None`` when no ``MaintenanceLoop`` is wired on ``app.state`` — this
    keeps the endpoint resilient to alternative app factories used in tests.

    Namespace scoping: ``collection_health`` entries are filtered to those whose
    ``{namespace}/{collection}`` key starts with ``{ns}/``, following the precedent
    in ``_build_backup_status`` which scopes backup status to the caller's namespace.
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
            )
        )

    return MaintenanceStatusDetail(
        enabled=enabled,
        interval_hours=interval_hours,
        last_run_at=last_run_at,
        next_run_at=next_run_at,
        collection_health=collection_health,
    )
