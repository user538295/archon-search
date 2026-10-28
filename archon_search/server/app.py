"""FastAPI app factory for archon-search REST control plane."""
from __future__ import annotations

import asyncio
import functools
import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.datastructures import State

    from archon_search.query_expansion_protocol import QueryExpansionProvider

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from archon_search._durable_io import ensure_private_dir
from archon_search._privacy import configure_query_fingerprint_salt
from archon_search.chunker import ASTChunker, DocumentChunker
from archon_search.config import (
    ConfigError,
    SearchConfig,
    get_default_config_path,
    resolve_active_model,
    warn_gc_cpu_priority,
    warn_if_legacy_config_ignored,
)
from archon_search.constants import BYTES_PER_MIB, DEFAULT_NAMESPACE
from archon_search.embedder import (
    Embedder,
    EmbedderBackend,
    ModelEmbedder,
    UnsupportedModelEmbedder,
)
from archon_search.embedder_cache import (
    EMBEDDER_NOT_READY_CODE,
    EMBEDDER_NOT_READY_DETAIL,
    EmbedderCache,
    EmbedderNotReadyError,
)
from archon_search.graph_store import check_and_warn_legacy_graph_tables
from archon_search.ingest_path_policy import (
    ingest_path_policy,
    log_if_non_admin_path_ingest_is_refused,
)
from archon_search.jobs.backup_loop import BackupLoop
from archon_search.jobs.maintenance_loop import MaintenanceLoop
from archon_search.jobs.scheduler import JobScheduler
from archon_search.jobs.store import JobStore
from archon_search.key_manager import (
    KeyRecord,
    KeyStore,
    KeyStoreUnreadableError,
    hash_token,
    load_or_generate_key,
)
from archon_search.logging_setup import build_uvicorn_log_config, configure_logging
from archon_search.memory_budget import warn_if_models_exceed_memory
from archon_search.model_specs import UnsupportedModelError
from archon_search.model_validation import validate_models_async
from archon_search.parser import DocumentParser
from archon_search.paths import get_data_dir, get_fasttext_models_dir
from archon_search.pipeline import SearchPipeline
from archon_search.progress import IndexingStateStore
from archon_search.reranker import (
    ModelReranker,
    Reranker,
    RerankerBackend,
    UnsupportedModelReranker,
)
from archon_search.server.graph_view_token import GRAPH_VIEW_SECRET_BYTES
from archon_search.server.middleware_auth import (
    _EXEMPT_PATHS,
    DOCS_PATHS,
    APIKeyMiddleware,
)
from archon_search.server.middleware_body_limit import install_body_size_limit
from archon_search.server.middleware_context import RequestContextMiddleware
from archon_search.server.schemas import StartupSyncResult, WarmupResult
from archon_search.server.warmup import run_model_warmup, run_warmup_recovery
from archon_search.store import SearchStore
from archon_search.sync_suppression import (
    get_sync_suppressed_file,
    write_sync_suppressed_sentinel,
)
from archon_search.types import JobStatus
from archon_search.uv_tool import extra_install_hint

try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version
    _VERSION = _pkg_version("archon-search")
except PackageNotFoundError:
    _VERSION = "dev"

from archon_search.server.routes_backup import router as backup_router
from archon_search.server.routes_collections import router as collections_router
from archon_search.server.routes_explain import router as explain_router
from archon_search.server.routes_export import router as export_router
from archon_search.server.routes_graph import router as graph_router
from archon_search.server.routes_health import router as health_router
from archon_search.server.routes_jobs import make_retry_dispatcher
from archon_search.server.routes_jobs import router as jobs_router
from archon_search.server.routes_keys import router as keys_router
from archon_search.server.routes_maintenance import router as maintenance_router
from archon_search.server.routes_ready import router as ready_router
from archon_search.server.routes_route import router as route_router
from archon_search.server.routes_search import router as search_router
from archon_search.server.routes_state import router as state_router
from archon_search.server.routes_status import router as status_router
from archon_search.server.routes_sync import router as sync_router
from archon_search.server.routes_telemetry import router as telemetry_router
from archon_search.telemetry.hasher import hash_doc_id, load_or_create_salt
from archon_search.telemetry.pruner import Pruner
from archon_search.telemetry.writer import TelemetryWriter

logger = logging.getLogger(__name__)

# Terminal ceiling for the startup collection sync. A full corpus (re)index is
# legitimately slower than a model load, so this is set well above the warm-up
# ceiling (``_WARMUP_TASK_TIMEOUT_SECONDS`` in server/warmup.py). Docker's
# HEALTHCHECK (Dockerfile) uses --start-period=600s --interval=15s --retries=3
# against /ready, i.e. ~645s of grace before the container is marked unhealthy;
# an unbounded sync exceeding that boot-loops under an orchestrator that
# restarts unhealthy containers (the sync restarts from the top every time,
# never converging on a large corpus). Bounding the sync here and marking
# ``sync: "fail"`` (which does NOT gate /ready — see _sync_check_status in
# routes_ready.py) breaks that loop: the container reports healthy and the
# operator still sees the failure on /ready and in the log.
_STARTUP_SYNC_TIMEOUT_SECONDS: float = 1800.0

# What a configured CORS origin (``[server].cors_origins``) may send: the methods the REST API uses and the headers
# a Bearer-authenticated JSON client needs. Nothing broader is ever allowed.
_CORS_ALLOWED_METHODS: list[str] = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
_CORS_ALLOWED_HEADERS: list[str] = ["Authorization", "Content-Type", "X-Request-ID", "X-Ingested-By"]

# Sanitized, wire-facing ``error`` values for the SyncJob that backs the startup
# sync — it is readable through GET /jobs, so no exception text goes in here.
_STARTUP_SYNC_ERROR_PARTIAL = "startup sync completed with per-collection errors"
_STARTUP_SYNC_ERROR_TIMEOUT = "startup sync exceeded its time limit"
_STARTUP_SYNC_ERROR_FAILED = "startup sync failed"


def _start_startup_sync_job(job_store: JobStore) -> str | None:
    """Create the RUNNING SyncJob that backs the startup sync; return its id.

    This is what makes the crash-loop guard self-arming. ``collection_sync.sync()``
    writes no job row of its own, so a process killed *during the startup sync*
    used to leave nothing behind and the next boot re-entered the same workload —
    the exact loop the guard exists to break. A RUNNING SyncJob on disk is
    rewritten to ``FAILED / "process_restart"`` by ``JobStore._load()`` on the
    next boot, which is the marker ``crashed_ingest_on_load`` keys on.

    Returns ``None`` when the job store could not be written: the sync then runs
    unmarked (exactly the previous behavior) rather than failing startup.

    The job is created RUNNING in one write, never QUEUED: ``QUEUED`` is
    non-terminal, so a QUEUED row whose claim failed would never be evicted
    (``JobStore._evict_old``) and every such boot would leak another row.

    Residual: ``atomic_write_json`` can raise after its rename, leaving the
    RUNNING row on disk although the create raised; ``create_job`` then rewrites
    the file without it. Only if that corrective rewrite fails too does the row
    stay, and the next boot reloads it as ``FAILED / "process_restart"`` — a
    false crash signal for the crash-loop guard although no sync was marked.
    """
    try:
        job = job_store.create_sync(namespace=DEFAULT_NAMESPACE)
    except Exception:
        logger.warning(
            "startup sync: could not record a job; running unmarked (a crash during "
            "this sync will not be detected on the next boot)",
            exc_info=True,
        )
        return None
    return job.job_id


def _finish_startup_sync_job(job_store: JobStore, job_id: str | None, **fields: Any) -> None:
    """Drive the startup-sync job to a terminal status. Never raises into the task."""
    if job_id is None:
        return
    try:
        job_store.update(job_id, **fields)
    except Exception:  # noqa: BLE001 — "never raises into the task": a failed status write is logged, not propagated
        logger.warning(
            "startup sync: could not persist the terminal status for job %s", job_id
        )


async def _sync_watched_collection(app: FastAPI, col_name: str, source_path: str) -> None:
    """Run the watcher-triggered sync of collection *col_name* over *source_path* (the lifespan's watcher callback).

    Skipped while the startup sync is suppressed: the corpus may be stale for a reason an operator has not yet
    addressed, and re-entering ingest here would defeat the crash-loop guard through a side door. Keys on the SAME live
    state a manual POST /sync clears (``app.state.sync_result``), not ``job_store.crashed_ingest_on_load`` (boot-time
    only) — otherwise a successful manual sync would clear GET /status while the watcher stayed blocked forever.

    The sync creates no job, so it registers as a writer of the collection (``SearchStore.collection_writer``) for its
    whole run: ``DELETE /collections/{name}`` refuses meanwhile instead of dropping a collection the sync would
    re-create. The watcher's ``{collection: path}`` snapshot is taken at startup and a DELETE leaves it alone, so the
    path is checked against the live config after the registration (made under the collection lock the DELETE holds
    while it edits the config): a collection deleted meanwhile — or earlier — is ignored, not re-created from changes
    its stale indexing state still reports.

    Raises:
        StoreBusyError: if the collection lock is not acquired within ``INGEST_LOCK_TIMEOUT_S``; the change is
            not synced (the watcher logs it), and the next change triggers a sync again.
    """
    if app.state.sync_result == StartupSyncResult.SUPPRESSED:
        logger.warning(
            "watcher: ingest for collection %r SKIPPED — the "
            "startup sync is suppressed (the previous run died "
            "mid-ingest). Resume manually with POST /sync or "
            "`archon-search sync` once the cause is addressed.",
            col_name,
        )
        return
    async with app.state.search_store.collection_writer(col_name):
        config: SearchConfig = app.state.config
        configured_paths = {
            str(Path(path).expanduser().resolve()) for path in (*config.pinned_collections, *config.collections)
        }
        if source_path not in configured_paths:
            logger.info(
                "watcher: change in %r ignored — collection %r is no longer configured (deleted)", source_path, col_name
            )
            return
        await app.state.collection_sync.sync_collection(col_name, Path(source_path))


async def _run_startup_sync(
    job_store: JobStore,
    app_state: State,
    cols: list[str],
) -> None:
    """Run the lifespan's startup collection sync, job-backed and lock-held.

    Hoisted to module level (fix-brief-C item 5): this used to be a closure
    over ``job_store``/``app``/``all_cols`` inside ``create_app``'s lifespan,
    which directly caused the app.py boundary bug where a shutdown mid-sync
    was misclassified — a closure hides its captured state from anything
    outside the enclosing function, including tests and reviewers. Every
    dependency is now an explicit parameter instead of a capture.

    Job-backed so the crash-loop guard arms itself — see
    ``_start_startup_sync_job``. The job row is written here, not in the
    lifespan body: startup must not do file I/O before it yields, or the port
    stays closed while it happens.

    ``job_id`` starts ``None`` and is assigned inside the ``try:`` — belt-and-
    braces so a non-``Exception`` ``BaseException`` out of
    ``_start_startup_sync_job`` still leaves ``job_id`` defined for the
    ``except`` handlers below (they would otherwise hit ``UnboundLocalError``
    instead of failing loudly and coherently).
    """
    job_id: str | None = None
    try:
        job_id = _start_startup_sync_job(job_store)
        # A wedged/oversized sync must not leave /ready gated at 503 for the
        # process lifetime — see _STARTUP_SYNC_TIMEOUT_SECONDS.
        async with asyncio.timeout(_STARTUP_SYNC_TIMEOUT_SECONDS):
            # Held for the whole sync so a POST /sync arriving during startup
            # gets a 409 instead of starting a second, concurrent sync over
            # the same collections.
            async with app_state.sync_lock:
                result = await app_state.collection_sync.sync(cols)
            if result.errors:
                # sync() does not raise on a per-collection failure — it
                # accumulates messages in SyncResult.errors and returns
                # normally, so this is the common failure mode (missing path,
                # ingest error, chunk-size reindex failure).
                app_state._startup_sync_failed = True
                app_state.sync_result = StartupSyncResult.FAILED
                logger.warning(
                    "startup sync completed with %d error(s): %s",
                    len(result.errors),
                    result.errors,
                )
                _finish_startup_sync_job(
                    job_store,
                    job_id,
                    status=JobStatus.FAILED,
                    error=_STARTUP_SYNC_ERROR_PARTIAL,
                )
            else:
                logger.info("startup sync complete (%d collection(s))", len(cols))
                app_state.sync_result = StartupSyncResult.DONE
                _finish_startup_sync_job(
                    job_store,
                    job_id,
                    status=JobStatus.DONE,
                    result={
                        "added": result.added,
                        "removed": result.removed,
                        "unchanged": result.unchanged,
                        "errors": result.errors,
                        "skipped": result.skipped,
                        "updated": result.updated,
                    },
                )
    except asyncio.CancelledError:
        logger.info("startup sync cancelled during shutdown")
        app_state.sync_result = StartupSyncResult.FAILED
        # CANCELLED, not FAILED: an ordinary clean shutdown is not a failure, and
        # the job row is the ledger operators read during an incident. Mirrors
        # routes_sync._sync_task, which records the same event the same way.
        _finish_startup_sync_job(job_store, job_id, status=JobStatus.CANCELLED)
        raise
    except TimeoutError:
        logger.warning(
            "startup sync timed out after %.0fs; marking sync failed so /ready "
            "can recover instead of gating forever",
            _STARTUP_SYNC_TIMEOUT_SECONDS,
        )
        app_state._startup_sync_failed = True
        app_state.sync_result = StartupSyncResult.FAILED
        _finish_startup_sync_job(
            job_store,
            job_id,
            status=JobStatus.FAILED,
            error=_STARTUP_SYNC_ERROR_TIMEOUT,
        )
    except BaseException as exc:
        logger.warning("startup sync failed: %s", exc, exc_info=True)
        app_state._startup_sync_failed = True
        app_state.sync_result = StartupSyncResult.FAILED
        _finish_startup_sync_job(
            job_store,
            job_id,
            status=JobStatus.FAILED,
            error=_STARTUP_SYNC_ERROR_FAILED,
        )


def _multilingual_model_path() -> Path:
    """Return the lid.176.ftz model path, resolved lazily on every call.

    Derived from ``get_fasttext_models_dir()`` so ``ARCHON_SEARCH_DATA_DIR``
    redirects the model lookup at call time, not at import time. Imports
    ``FASTTEXT_MODEL_FILENAME`` lazily to avoid an eager ``language_detector``
    import at module load.
    """
    from archon_search.language_detector import FASTTEXT_MODEL_FILENAME

    return get_fasttext_models_dir() / FASTTEXT_MODEL_FILENAME


def _import_fasttext() -> Any:
    """Import fasttext; raises ImportError if the package is not installed."""
    import fasttext  # type: ignore[import-untyped]
    return fasttext


def _check_multilingual_deps(config: SearchConfig) -> None:
    """Check that multilingual dependencies are present when multilingual=True.

    Called synchronously in ``create_app()`` before ``SearchPipeline`` is
    constructed.  Raises ``RuntimeError`` with an actionable message when:

    - ``fasttext-wheel`` is not installed, or
    - ``lid.176.ftz`` model file is absent.

    No-ops when ``config.multilingual`` is ``False``.
    """
    if not config.multilingual:
        return

    # Check 1 — package availability
    try:
        _import_fasttext()
    except ImportError:
        raise RuntimeError(
            "multilingual=true but fasttext-wheel is not installed; "
            f"run: {extra_install_hint('multilingual')}"
        )

    # Check 2 — model file presence
    if not _multilingual_model_path().exists():
        raise RuntimeError(
            "multilingual=true but lid.176.ftz model is missing; "
            "run: archon-search install --multilingual"
        )


def _check_provider_deps(config: SearchConfig) -> None:
    """Check that provider package deps are present for HyDE / RAG Fusion.

    Firing is provider-specific: the ``ollama`` and ``openai`` guards fire
    unconditionally (any configured non-default provider is an explicit opt-in),
    but the ``anthropic`` guard fires ONLY when the feature is enabled —
    ``anthropic`` is the default provider (config.py), so an unconditional import
    guard would require ``archon-search[hyde]`` on every install and break the
    optional-extras model. ``claude_cli`` is never guarded (no pip package;
    availability is the ``claude`` binary on PATH with graceful runtime degradation).
    ``llama_cpp`` is never guarded either: httpx is a core dependency (no optional
    import), llama-server needs no API key, and reachability is a runtime concern
    handled by the non-blocking probe in ``model_validation.py`` — warn, never
    raise, at ``create_app()`` time.
    """
    # Per-provider firing rule: ollama/openai unconditional; anthropic enabled-gated;
    # claude_cli never (see docstring). `enabled` is consumed only by the anthropic branch.
    for label, enabled, provider, model in (
        ("hyde", config.hyde.enabled, config.hyde.provider, config.hyde.model),
        ("rag_fusion", config.rag_fusion.enabled, config.rag_fusion.provider, config.rag_fusion.model),
    ):
        if provider == "ollama":
            try:
                import ollama  # type: ignore[import-untyped]  # noqa: F401
            except ImportError:
                raise ConfigError(
                    f"[{label}] provider='ollama' but the 'ollama' package is not installed; "
                    f"run: {extra_install_hint('ollama')}"
                )
            # _apply_toml already rejects empty model via ConfigError for the TOML path;
            # this guard covers programmatic construction with model=''.
            if not model:
                raise ConfigError(
                    f"[{label}] provider='ollama' requires a non-empty model name; "
                    f"set [{label}].model in archon-search.toml"
                )
        elif provider == "openai":
            try:
                import openai  # type: ignore[import-untyped]  # noqa: F401
            except ImportError:
                raise ConfigError(
                    f"[{label}] provider='openai' but the 'openai' package is not installed; "
                    f"run: {extra_install_hint('openai-provider')}"
                )
            # _apply_toml already rejects empty model via ConfigError for the TOML path;
            # this guard covers programmatic construction with model=''.
            if not model:
                raise ConfigError(
                    f"[{label}] provider='openai' requires a non-empty model name; "
                    f"set [{label}].model in archon-search.toml"
                )
        elif provider == "anthropic" and enabled:
            # Enabled-gated, unlike ollama/openai above: 'anthropic' is the default
            # provider (config.py), so an unconditional import guard would require the
            # package on every server start and break the optional-extras philosophy.
            # Only enforce it when the feature is actually turned on.
            try:
                import anthropic  # noqa: F401
            except ImportError:
                raise ConfigError(
                    f"[{label}] enabled=true with provider='anthropic' but the 'anthropic' "
                    f"package is not installed; run: {extra_install_hint('hyde')}"
                )
        # provider='claude_cli' is intentionally not guarded: it has no pip package
        # and resolves availability via shutil.which('claude') with graceful runtime
        # degradation (claude_cli_provider.py).
        # provider='llama_cpp' is intentionally not guarded: httpx is a core
        # dependency (no optional import) and llama-server needs no API key.
        # Unreachability is warned-not-blocked by the async startup probe in
        # model_validation.py, not by this synchronous, network-free check.


def _build_query_expansion_provider(
    provider: str, model: str, ollama_base_url: str, llama_cpp_base_url: str
) -> QueryExpansionProvider:
    """Build and return the appropriate QueryExpansionProvider from config.

    Returns an OllamaQueryExpansionProvider for provider='ollama',
    a ClaudeCLIQueryExpansionProvider for provider='claude_cli',
    an OpenAIQueryExpansionProvider for provider='openai',
    a LlamaCppQueryExpansionProvider for provider='llama_cpp',
    or an AnthropicQueryExpansionProvider for provider='anthropic' (default).

    The returned object is injected as the ``provider`` argument to
    HyDEGenerator and RAGFusionGenerator so the generators do not
    construct a default provider.
    """
    if provider == "llama_cpp":
        from archon_search.providers.llama_cpp_provider import (
            LlamaCppQueryExpansionProvider,
        )
        return LlamaCppQueryExpansionProvider(model=model, base_url=llama_cpp_base_url)
    if provider == "ollama":
        from archon_search.providers.ollama_provider import (
            OllamaQueryExpansionProvider,
        )
        return OllamaQueryExpansionProvider(model=model, base_url=ollama_base_url)
    if provider == "claude_cli":
        from archon_search.providers.claude_cli_provider import (
            ClaudeCLIQueryExpansionProvider,
        )
        return ClaudeCLIQueryExpansionProvider(model=model)
    if provider == "openai":
        from archon_search.providers.openai_provider import (
            OpenAIQueryExpansionProvider,
        )
        return OpenAIQueryExpansionProvider(model=model)
    # Default: anthropic
    from archon_search.providers.anthropic_provider import (
        AnthropicQueryExpansionProvider,
    )
    return AnthropicQueryExpansionProvider(model=model)


def _check_graph_deps(config: SearchConfig) -> None:
    """Check that graph dependencies are present when graph.enabled=True.

    Called synchronously in ``create_app()`` before ``SearchPipeline`` is
    constructed.  Raises ``ConfigError`` with an actionable message when
    ``gliner`` is not installed.

    No-ops when ``config.graph.enabled`` is ``False``.
    """
    # 2026-08-19-030: delegates to the single implementation shared with
    # `pipeline.create_pipeline`, so the two construction-time guards cannot drift.
    from archon_search.graph_extractor import ensure_graph_engine_importable

    ensure_graph_engine_importable(config.graph)


def _configure_openapi(app: FastAPI) -> None:
    """Override app.openapi with a closure that adds BearerAuth security scheme
    and per-path security annotations to all non-public endpoints."""

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title="archon-search",
            version=_VERSION,
            description="REST API for archon-search document search and collection management",
            routes=app.routes,
        )
        schema.setdefault("components", {})
        schema["components"].setdefault("securitySchemes", {})
        schema["components"]["securitySchemes"]["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
        }
        # _EXEMPT_PATHS: only /health is a real schema exemption (appears in paths);
        # /docs, /openapi.json, /redoc are defensive — FastAPI never includes them in the schema.
        for path, path_item in schema.get("paths", {}).items():
            if path in _EXEMPT_PATHS:
                continue
            for operation in path_item.values():
                if isinstance(operation, dict):
                    operation["security"] = [{"BearerAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def _embedder_backend(model_name: str, device: str) -> EmbedderBackend:
    """The configured embedder; a name outside the registry yields a backend that fails every encode.

    The failure surfaces through warm-up / model validation as ``checks.models == "fail"`` with the
    ``unsupported model ...; supported: ...`` text logged, so startup is not aborted (S750).
    """
    try:
        return ModelEmbedder(model_name, device=device)
    except UnsupportedModelError as exc:
        logger.warning("%s", exc)
        return UnsupportedModelEmbedder(exc)


def _reranker_backend(model_name: str, device: str) -> RerankerBackend:
    """The configured reranker; a name outside the registry yields a backend that fails every predict (S750)."""
    try:
        return ModelReranker(model_name, device=device)
    except UnsupportedModelError as exc:
        logger.warning("%s", exc)
        return UnsupportedModelReranker(exc)


def create_app(
    config: SearchConfig,
    job_store: JobStore,
    config_path: Path | str | None = None,
    scheduler: JobScheduler | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    _check_multilingual_deps(config)
    _check_graph_deps(config)
    _check_provider_deps(config)
    api_key, key_source = load_or_generate_key()
    if api_key in config.namespaces:
        # The managed-key path resolves a TOML token first, so it would shadow the default key and silently
        # demote it from admin. The message names no token.
        raise ConfigError(
            f"[namespaces] the token for namespace {config.namespaces[api_key]!r} is not usable: "
            "it equals the default API key; give every namespace its own token"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Startup: persist TOML synthetic KeyRecord objects into keys.json so that
        # GET /keys (BE-6) can surface them and active_keys() includes them.
        # This replaces synthetic records from previous runs with the current TOML state.
        # An unreadable keys.json is left untouched (S17): the server still starts, and TOML
        # tokens stay accepted through the middleware's namespaces fallback.
        try:
            await app.state.key_store.load_synthetic_records(_synthetic_records)
        except KeyStoreUnreadableError:
            logger.error("keys.json is unreadable — TOML namespace keys not persisted; keys.json left unchanged")
        await log_if_non_admin_path_ingest_is_refused(config, app.state.key_store)

        # Startup: connect search store
        await app.state.search_store.connect()
        await app.state.search_store._run_startup_migrations()

        # Startup: warn when gc_rebuild_cpu_priority is non-normal on non-Linux platforms
        # (CPU priority reduction via os.nice() is Linux-only).
        warn_gc_cpu_priority(config)

        # Startup: warn once if legacy graph tables (pre-E2d naming) exist in the DB.
        # This check runs unconditionally so operators are notified even when
        # graph.enabled = false.  Never raises — startup continues regardless.
        try:
            await check_and_warn_legacy_graph_tables(app.state.search_store._db)
        except Exception:
            logger.warning("Legacy graph table scan failed; skipping", exc_info=True)

        # Startup: connect graph store (if graph is enabled)
        if app.state.graph_store is not None:
            try:
                await app.state.graph_store.connect()
            except Exception:
                logger.warning(
                    "Graph store failed to connect; graph extraction will be disabled",
                    exc_info=True,
                )
                app.state.graph_store = None

        # Startup: create embedder cache and warm the models the first search needs. The default
        # model is seeded with the global embedder, so the cache and the pipeline share one copy of
        # its weights instead of each loading its own.
        embedder_cache = EmbedderCache(
            config.embedder_cache_size,
            device=config.device,
            preloaded={config.embedding_model: app.state.pipeline.global_embedder},
        )
        app.state.embedder_cache = embedder_cache
        if config.eager_load_embedders:
            metas = await app.state.search_store.get_all_collections_meta()
            # Seed with the global default: collections that pin no model resolve to it.
            distinct_models: set[str] = {config.embedding_model} | {
                resolve_active_model(m, config) for m in metas
            }
            # The default model is pinned in the cache (it is the pipeline's own embedder), so only the others compete.
            evictable_models = distinct_models - {config.embedding_model}
            if len(evictable_models) > config.embedder_cache_size:
                logger.warning(
                    "eager_load_embedders: %d distinct models exceed embedder_cache_size "
                    "(embedder_cache_size=%d non-default slots; the default model is pinned and counted in N) "
                    "— some preloaded models will be evicted before first use",
                    len(distinct_models),
                    config.embedder_cache_size,
                )
        else:
            # Warm-up runs for every deployment, not just eager_load_embedders ones
            # (S279): the cross-encoder and the default embedder are built lazily on
            # the first search, so a heavyweight profile (e.g. "max", where the cold
            # model build of BAAI/bge-reranker-base measured 91 s) blocks that request
            # past any client read timeout — the caller sees no response at all.
            # ``eager_load_embedders`` still decides the *scope*: every per-collection
            # model when on, only the global default when off.
            distinct_models = {config.embedding_model}

        warn_if_models_exceed_memory(config, distinct_models)

        # Warm-up must never be awaited here: uvicorn binds the listening socket
        # only after lifespan startup returns, so a cold model cache (3-5 min)
        # would keep the port closed for the whole window. ``warmup_result`` is
        # set to "pending" BEFORE the task starts so /ready never reports models
        # as ready during the gap between startup returning and the task running.
        app.state.warmup_result = WarmupResult.PENDING
        warmup_task = asyncio.create_task(
            run_model_warmup(
                set_result=functools.partial(setattr, app.state, "warmup_result"),
                pipeline=app.state.pipeline,
                embedder_cache=embedder_cache,
                config=config,
                model_names=sorted(distinct_models),
            )
        )
        app.state._warmup_task = warmup_task
        app.state._background_tasks.add(warmup_task)
        warmup_task.add_done_callback(app.state._background_tasks.discard)

        # All startup migrations complete before the lifespan context yields control to the request loop

        # Startup: spawn background model validation (D6 / BE-4). Never blocks
        # startup — the lazy-load contract is hard. The result is stored on
        # ``app.state.model_validation`` (None while pending) and surfaced via
        # ``GET /status`` and ``GET /ready``. The probe runs the pipeline's own embedder
        # and cross-encoder, after the warm-up has finished building them: a throwaway
        # copy of each would hold the weights twice at the startup peak. A model the
        # warm-up could not build is retried by the probe, which reports the failure.
        app.state.model_validation = None

        async def _validate_pipeline_models() -> None:
            pipeline = app.state.pipeline
            app.state.model_validation = await validate_models_async(
                config,
                timeout_seconds=config.validation_timeout_seconds,
                embedder=pipeline.global_embedder.backend,
                reranker=pipeline.reranker.backend if pipeline.reranker is not None else None,
            )

        async def _run_model_validation() -> None:
            try:
                # Inside the try: a raise here would otherwise leave model_validation
                # None — ``checks.models: pending`` and /ready 503 forever (S631).
                await asyncio.wait({warmup_task})
                await _validate_pipeline_models()
            except asyncio.CancelledError:
                logger.info("validation cancelled during shutdown")
                raise
            except BaseException as exc:
                logger.warning("model validation task failed unexpectedly: %s", exc, exc_info=True)
                from archon_search.model_validation import (
                    VALIDATION_TASK_FAILED_WARNING,
                    failed_result,
                )

                app.state.model_validation = await failed_result(
                    VALIDATION_TASK_FAILED_WARNING, config
                )

        validation_task = asyncio.create_task(_run_model_validation())
        app.state._background_tasks.add(validation_task)
        validation_task.add_done_callback(app.state._background_tasks.discard)

        # Startup: a failed warm-up heals itself (H2). Without a timer-driven retry only a
        # search or an ingest warmed the models again, so a replica an orchestrator kept
        # traffic away from stayed 503 for the process lifetime. Runs after the warm-up and
        # re-runs the model validation once the default models are warm: the stored result
        # describes the failure, which would otherwise outlive it.
        async def _recover_failed_warmup() -> None:
            await asyncio.wait({warmup_task})
            if app.state.warmup_result != WarmupResult.FAILED:
                return
            await run_warmup_recovery(
                set_result=functools.partial(setattr, app.state, "warmup_result"),
                pipeline=app.state.pipeline,
                embedder_cache=embedder_cache,
                config=config,
                model_names=sorted(distinct_models),
                on_recovered=_validate_pipeline_models,
            )

        recovery_task = asyncio.create_task(_recover_failed_warmup())
        app.state._background_tasks.add(recovery_task)
        recovery_task.add_done_callback(app.state._background_tasks.discard)

        # Startup: install the real export/import dispatch closure on the
        # scheduler and start it. The placeholder dispatch passed to
        # ``JobScheduler(...)`` in ``run_server()`` is replaced here, once
        # ``app.state.search_store``, ``pipeline``, and ``embedder_cache`` are
        # ready. Without this reassignment the scheduler would either fail
        # outright or mark every dispatched job FAILED.
        if scheduler is not None:
            from archon_search.server.routes_export import (
                _export_task,
                _import_task,
            )
            from archon_search.types import ExportJob, ImportJob, MigrationJob

            def _real_dispatch(job: ExportJob | ImportJob | MigrationJob) -> None:
                if isinstance(job, ExportJob):
                    if job.source == "backup":
                        # For backup-sourced jobs, call _drain_completed() immediately
                        # after the export task finishes so that rotation happens
                        # synchronously with the backup completion rather than waiting
                        # for the _completion_loop's 60-second poll (S476).
                        _bl = app.state.backup_loop
                        _j = job

                        async def _backup_export_with_rotation(
                            export_job: ExportJob = _j,
                            backup_loop: BackupLoop = _bl,
                        ) -> None:
                            await _export_task(
                                export_job,
                                app.state.job_store,
                                app.state.search_store,
                                config,
                            )
                            backup_loop._drain_completed()

                        task = asyncio.create_task(_backup_export_with_rotation())
                    else:
                        task = asyncio.create_task(
                            _export_task(
                                job,
                                app.state.job_store,
                                app.state.search_store,
                                config,
                            )
                        )
                elif isinstance(job, ImportJob):
                    task = asyncio.create_task(
                        _import_task(
                            job,
                            app.state.job_store,
                            app.state.search_store,
                            app.state.pipeline,
                            app.state.embedder_cache,
                            config,
                        )
                    )
                elif isinstance(job, MigrationJob):
                    from archon_search.server.routes_collections import _migration_task
                    task = asyncio.create_task(
                        _migration_task(
                            job=job,
                            job_store=app.state.job_store,
                            search_store=app.state.search_store,
                            # spec=None: _migration_task fetches the pending REWRITE spec itself
                        )
                    )
                else:
                    raise TypeError(
                        f"_real_dispatch: unsupported job type {type(job).__name__}"
                    )
                scheduler.register_task(task)

            scheduler.dispatch_fn = _real_dispatch
            scheduler_task = asyncio.create_task(scheduler.run())
            app.state._background_tasks.add(scheduler_task)
        app.state.scheduler = scheduler

        # Startup: instantiate BackupLoop and start it as a background task.
        # The trigger loop self-exits when ``backup.interval_hours == 0``; the
        # completion loop always runs to drain any in-flight jobs left from a
        # previous session. Always present on ``app.state`` so observability
        # endpoints (``GET /status``) can read its state unconditionally.
        backup_loop = BackupLoop(
            job_store=app.state.job_store,
            search_store=app.state.search_store,
            config=config.backup,
            data_dir=get_data_dir(),
        )
        app.state.backup_loop = backup_loop
        backup_task = asyncio.create_task(backup_loop.run())
        app.state._background_tasks.add(backup_task)
        backup_task.add_done_callback(app.state._background_tasks.discard)

        # Startup: instantiate MaintenanceLoop and start it as a background task.
        # Always present on app.state so trigger routes and status endpoints can
        # reach it unconditionally, even when interval_hours == 0 (disabled schedule).
        maintenance_loop = MaintenanceLoop(
            job_store=app.state.job_store,
            search_store=app.state.search_store,
            config=config.maintenance,
            data_dir=get_data_dir(),
            graph_store=_graph_store,
            graph_config=config.graph,
            enrichment_client=_enrichment_client,
            # Failed-ingest retries run through POST /ingest's ingest task (jobs/ never imports server/).
            dispatch_retry=make_retry_dispatcher(app.state),
        )
        app.state.maintenance_loop = maintenance_loop
        maintenance_task = asyncio.create_task(maintenance_loop.run())
        app.state._background_tasks.add(maintenance_task)
        maintenance_task.add_done_callback(app.state._background_tasks.discard)

        # E2f BE-5: wire post-ingest synonym enrichment callback.
        # Both SearchPipeline (created in create_app, sync) and MaintenanceLoop
        # (created above in lifespan, async) are siblings — the wiring happens here,
        # after both exist.  pipeline.py holds only a Callable — no MaintenanceLoop import.
        app.state.pipeline.on_synonym_edges_written = (
            maintenance_loop.schedule_synonym_enrichment
        )

        # E2g BE-7: wire post-ingest PageRank recompute callback. Same
        # sibling-construction-order rationale as the synonym-enrichment wiring
        # above — both SearchPipeline and MaintenanceLoop already exist here.
        app.state.pipeline.on_defref_edges_written = (
            maintenance_loop.schedule_pagerank_recompute
        )

        # Startup: initialise telemetry if enabled
        if config.telemetry.enabled:
            log_dir = Path(config.telemetry.log_dir).expanduser()
            ensure_private_dir(log_dir)
            pruner = Pruner(log_dir, config.telemetry.retention_days)
            await asyncio.to_thread(pruner.prune_once)
            writer = TelemetryWriter(log_dir)
            app.state._background_tasks.add(await writer.start())
            app.state._background_tasks.add(await pruner.start())
            app.state.telemetry_writer = writer
        else:
            app.state.telemetry_writer = None

        # Startup: load or create the HMAC salt for telemetry doc_id hashing (D8 / BE-2).
        # ``load_or_create_salt`` is synchronous (file I/O); run it in a thread so
        # the event loop is not blocked. The result is stored on app.state in two forms:
        # - ``app.state.salt_bytes``: raw bytes (or None), read by _build_telemetry_status (BE-5)
        # - ``app.state.doc_id_hasher``: a Callable[[str], str] closure (or None),
        #   injected into routes and the MCP sub-app at construction time (BE-4).
        # Both are unconditional (mirrors backup_loop / model_validation pattern).
        salt_path = get_data_dir() / ".telemetry-salt"
        app.state.salt_bytes = None
        app.state.doc_id_hasher = None
        app.state.salt_bytes = await asyncio.to_thread(
            load_or_create_salt,
            config.telemetry.hash_doc_ids,
            salt_path,
        )
        if app.state.salt_bytes is not None:
            app.state.doc_id_hasher = functools.partial(hash_doc_id, app.state.salt_bytes)
        # The same salt keys the log fingerprints of query text (``_privacy._query_fingerprint``); without
        # one, they stay keyed by a random per-process secret.
        configure_query_fingerprint_salt(app.state.salt_bytes)

        # Startup: mount the MCP HTTP app at /mcp on the existing FastAPI app
        # (D9 / BE-2). Done here — inside the lifespan, after all REST objects are
        # ready — so create_mcp_http_app() receives fully-constructed dependencies.
        # FastMCP's StreamableHTTPSessionManager task group only starts when the
        # sub-app's own lifespan is entered, so the mount is wrapped in an explicit
        # lifespan delegation (mcp_starlette.router.lifespan_context). The mount
        # itself happens AFTER that context has entered, so a failed startup never
        # leaves a zombie /mcp route (Starlette has no app.unmount()). See ADR 09.
        # Always present on app.state so _build_mcp_status can read it
        # unconditionally (mirrors backup_loop / maintenance_loop / model_validation).
        app.state.mcp_bound = False
        try:
            async with AsyncExitStack() as _mcp_stack:
                if config.mcp.enabled:
                    try:
                        from archon_search.server.mcp import create_mcp_http_app
                        mcp_starlette = create_mcp_http_app(
                            pipeline=app.state.pipeline,
                            default_collection=(
                                config.collections[0] if config.collections else "default"
                            ),
                            writer=app.state.telemetry_writer,
                            config=config,
                            embedder_cache=app.state.embedder_cache,
                            job_store=app.state.job_store,
                            hyde_generator=app.state.hyde_generator,
                            rag_fusion_generator=app.state.rag_fusion_generator,
                            key_store=app.state.key_store,
                            doc_id_hasher=app.state.doc_id_hasher,
                            graph_store=app.state.graph_store,
                            # One current default key for both transports: MCP auth and
                            # rotate_key read app.state.api_key; rotate_key publishes to it.
                            api_key_state=app.state,
                            ingest_policy=app.state.ingest_path_policy,
                        )
                        await _mcp_stack.enter_async_context(
                            mcp_starlette.router.lifespan_context(app)
                        )
                        app.mount("/mcp", mcp_starlette)
                        logger.info("MCP HTTP endpoint mounted at /mcp")
                        app.state.mcp_bound = True
                    except Exception:
                        logger.warning(
                            "MCP server failed to start; continuing without MCP", exc_info=True
                        )

                # Startup: create filesystem watcher when watch=True.
                # The WatcherManager fires a callback when files change in a
                # collection's source directory; the callback calls
                # collection_sync.sync_collection(), which tags the resulting
                # chunks ingested_by="watcher" unless it detects a genuine
                # reindex trigger, in which case it uses "reindex" (S276).
                if config.watch:
                    try:
                        from archon_search.watcher import WatcherManager
                        all_collections = (
                            list(config.pinned_collections) + list(config.collections)
                        )
                        desired = app.state.collection_sync.build_desired(all_collections)
                        if desired:
                            loop = asyncio.get_running_loop()
                            # Capture desired in the closure so the callback can resolve
                            # collection_name → source_path without a round-trip to config.
                            _desired_snapshot = dict(desired)

                            async def _watch_callback(col_name: str) -> None:
                                path_str = _desired_snapshot.get(col_name)
                                if path_str is None:
                                    logger.warning(
                                        "Watcher fired for unknown collection %r; skipping",
                                        col_name,
                                    )
                                    return
                                # Crash-loop guard, watcher half, and the collection-writer registration: see
                                # _sync_watched_collection.
                                await _sync_watched_collection(app, col_name, path_str)

                            wm = WatcherManager(
                                on_change=_watch_callback,
                                loop=loop,
                            )
                            for col_name, path_str in _desired_snapshot.items():
                                wm.add(col_name, Path(path_str))
                            app.state.watcher_manager = wm
                            logger.info(
                                "Filesystem watcher started for %d collection(s): %s",
                                len(_desired_snapshot),
                                sorted(_desired_snapshot),
                            )
                    except Exception:
                        logger.warning(
                            "Filesystem watcher failed to start; continuing without watching",
                            exc_info=True,
                        )

                # Startup: sync collections (detects chunk_size changes, auto-reindex).
                # Never awaited here: uvicorn binds the listening socket only after
                # lifespan startup returns, so a full corpus sync would keep the port
                # closed — clients would get ConnectError instead of a 503.
                all_cols = list(config.pinned_collections) + list(config.collections)
                # Sticky suppression (fix-brief-C item 1): suppress when EITHER
                # loading the job store just rewrote an ingest-family job to
                # FAILED/"process_restart" (a fresh crash detected THIS boot) OR
                # a previous boot already suppressed and the operator has not
                # yet run a clean POST /sync (the sentinel file from that boot
                # still exists). Without the second half, suppression lasted
                # exactly one boot: by the boot after the crash the job row is
                # already terminal (FAILED, not RUNNING), so
                # crashed_ingest_on_load alone would silently re-arm the loop
                # under any supervisor that restarts more than once. Read only
                # — .exists() is not slow work, so this stays safe before yield.
                sync_suppressed = all_cols and (
                    job_store.crashed_ingest_on_load
                    or get_sync_suppressed_file().exists()
                )
                if sync_suppressed:
                    # No task is created, so /ready stays 200 — the state is
                    # degraded-but-ready, surfaced on GET /status.
                    logger.warning(
                        "startup sync SUPPRESSED for %d collection(s): the previous run "
                        "died mid-ingest (job marked 'process_restart'). The index may be "
                        "stale — resume manually with POST /sync or `archon-search sync` "
                        "once the cause is addressed.",
                        len(all_cols),
                    )
                    # Fail-open: a write failure must not fail startup — see
                    # write_sync_suppressed_sentinel's own docstring.
                    write_sync_suppressed_sentinel()
                    app.state.sync_result = StartupSyncResult.SUPPRESSED
                elif all_cols:
                    app.state.sync_result = StartupSyncResult.PENDING
                    sync_task = asyncio.create_task(
                        _run_startup_sync(job_store, app.state, all_cols)
                    )
                    app.state._startup_sync_task = sync_task
                    app.state._background_tasks.add(sync_task)
                    sync_task.add_done_callback(app.state._background_tasks.discard)

                yield
        finally:
            # Shutdown: stop filesystem watcher before disconnecting the store.
            if app.state.watcher_manager is not None:
                try:
                    await app.state.watcher_manager.stop_all()
                except Exception:
                    logger.warning("Filesystem watcher stop_all() raised", exc_info=True)

            # Shutdown ordering is load-bearing in two directions at once:
            # - the telemetry writer must drain BEFORE its own background task is
            #   cancelled — drain_and_stop() cancels writer._task internally, so
            #   cancelling app.state._background_tasks first would leave
            #   queue.join() unsatisfiable and every buffered entry would be lost.
            # - background tasks (startup sync, model warm-up, model validation)
            #   must be cancelled and drained BEFORE the stores disconnect, or an
            #   in-flight task keeps calling into a closed store.
            # Draining telemetry first, then cancelling the rest, then
            # disconnecting the stores last satisfies both.
            if app.state.telemetry_writer is not None:
                await app.state.telemetry_writer.drain_and_stop()

            tasks = list(app.state._background_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            # Shutdown: disconnect search store
            await app.state.search_store.disconnect()

            # Shutdown: disconnect graph store (if enabled)
            if app.state.graph_store is not None:
                await app.state.graph_store.disconnect()

    # Instantiate the key store pointing to keys.json under the data directory.
    # The mounted MCP app receives this same instance (app.state.key_store), so REST and
    # MCP rotations share its rotation_lock; cross-process visibility is achieved because
    # active_keys() re-reads from disk on every call.
    key_store = KeyStore(get_data_dir() / "keys.json")

    # Build synthetic KeyRecord objects from the TOML [namespaces] map once at
    # construction time. The actual write to keys.json happens inside the lifespan
    # (async context). Synthetic records have id=None and no expires_at.
    _synthetic_records = [
        KeyRecord(
            id=None,
            token_hash=hash_token(raw_token),
            namespace=ns,
            label=ns,  # label mirrors the TOML namespace name for operator identification
            created_at=datetime.now(UTC),
            expires_at=None,
            status="active",
            admin=False,  # TOML [namespaces] tokens are never admin keys
        )
        for raw_token, ns in config.namespaces.items()
    ]

    # ``/docs`` and ``/redoc`` exist only when ``[server].docs_enabled``; ``/openapi.json`` always exists but
    # needs a bearer token unless docs are enabled (the Swagger UI page fetches it without credentials).
    app = FastAPI(
        title="archon-search",
        lifespan=lifespan,
        docs_url="/docs" if config.docs_enabled else None,
        redoc_url="/redoc" if config.docs_enabled else None,
    )
    # ``namespaces=config.namespaces`` is kept for backward compatibility. TOML
    # tokens are also registered as synthetic KeyRecord objects (path 1, above),
    # so the TOML namespace dict (path 2) is a defense-in-depth fallback that
    # activates only if the synthetic write failed. The managed-key path always
    # wins on early-exit, making path 2 redundant in normal operation, but
    # removing it would silently break any direct construction of APIKeyMiddleware
    # without a key_store — the plan requires backward-compat to be unchanged.
    app.add_middleware(
        APIKeyMiddleware,
        api_key=api_key,
        namespaces=config.namespaces,
        key_store=key_store,
        exempt_paths=_EXEMPT_PATHS | DOCS_PATHS if config.docs_enabled else _EXEMPT_PATHS,
    )
    # OpenAI401Middleware must be added AFTER APIKeyMiddleware (Starlette LIFO —
    # last-added is outermost) so it wraps the 401 response before it leaves the
    # server.  Guard: only registered when openai_shim.enabled = true.
    if config.openai_shim.enabled:
        from archon_search.server.routes_openai_shim import OpenAI401Middleware
        app.add_middleware(OpenAI401Middleware)
    # Outside authentication and routing (an oversized body is refused before either sees it), inside CORS (so the
    # 413 carries the CORS headers and a browser client of a configured origin can read it) and inside
    # RequestContextMiddleware (so it carries the request id).
    install_body_size_limit(app, config.max_request_body_mb * BYTES_PER_MIB)
    # Cross-origin browser access is opt-in: the API authenticates with a Bearer header (never cookies), so with no
    # ``[server].cors_origins`` no CORS header is sent and a page on another origin cannot use it. Never ``*``,
    # never credentials.
    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_methods=_CORS_ALLOWED_METHODS,
            allow_headers=_CORS_ALLOWED_HEADERS,
            allow_credentials=False,
        )
    app.add_middleware(
        RequestContextMiddleware,
        header_name=config.observability.request_id_header,
    )
    logger.info("API key authentication enabled (source: %s)", key_source)
    app.state.key_store = key_store
    # The server's current default key: POST /keys/rotate and the mounted MCP rotate_key
    # read the key to retire from it and publish the new key to it after a successful
    # rotation; both auth middlewares accept it on the legacy path.
    app.state.api_key = api_key
    # Keys the graph-viewer view tokens (see ``graph_view_token``); random per process, so tokens die on restart.
    app.state.graph_view_secret = secrets.token_bytes(GRAPH_VIEW_SECRET_BYTES)
    app.state.config = config
    app.state.job_store = job_store
    app.state.config_path = Path(config_path) if config_path is not None else None
    # What a client-supplied ingest path may read: never the server's own state or credential files, and inside
    # ``[ingest].allowed_roots`` when that is set (SecReview #3).
    app.state.ingest_path_policy = ingest_path_policy(config, app.state.config_path)
    app.state._background_tasks: set = set()
    # Set to a Task by the lifespan's model warm-up; the None default keeps the
    # attribute readable before the lifespan has run.
    app.state._warmup_task = None
    # Set to a Task by the lifespan only when at least one collection is configured;
    # the None default keeps the attribute readable on every path.
    app.state._startup_sync_task = None
    # Set to True by _run_startup_sync when the sync crashed (except branch) or
    # completed with per-collection errors (result.errors non-empty); the False
    # default keeps the attribute readable on every path.
    app.state._startup_sync_failed = False
    # Warm-up progress for /ready and /status: set to "pending"/"done"/"failed" by
    # the lifespan on every startup; None means the lifespan has not run yet.
    app.state.warmup_result = None
    # Startup-sync progress for GET /status: set to "pending"/"done"/"failed" by the
    # lifespan when at least one collection is configured; None means no startup
    # sync was ever run (mirrors warmup_result above).
    app.state.sync_result = None
    app.state.state_store = IndexingStateStore(config.db_path)
    app.state.search_store = SearchStore(config.db_path, config=config)
    app.state.watcher_manager = None
    app.state.embedder = Embedder(_embedder_backend(config.embedding_model, config.device))

    # C2: instantiate LanguageDetector for production path when multilingual=True.
    # _check_multilingual_deps() has already passed at this point, so imports are safe.
    if config.multilingual:
        from archon_search.language_detector import LanguageDetector
        _lang_detector = LanguageDetector(_multilingual_model_path())
    else:
        _lang_detector = None

    # LLCP BE-7: build the enrichment client once, regardless of graph.enabled --
    # construction is cheap (no I/O; the httpx-based v1 clients only make network
    # calls when their methods are invoked) and CommunityBuilder is also reachable
    # via the rebuild-communities route independently of the extractor below.
    # Returns None when [graph].provider is None (air-gap default) or unset.
    from archon_search.enrichment.factory import EnrichmentClientFactory
    _enrichment_client = EnrichmentClientFactory.build(config.graph)
    app.state.enrichment_client = _enrichment_client

    if config.graph.enabled:
        from archon_search.defref_extractor import DefRefExtractor as _DefRefExtractor
        from archon_search.graph_expander import GraphExpander as _GraphExpander
        from archon_search.graph_extractor import GraphExtractor as _GraphExtractor
        from archon_search.graph_store import GraphStore as _GraphStore
        from archon_search.ppr_walker import PPRWalker as _PPRWalker
        _graph_store = _GraphStore(config.db_path)
        _graph_extractor = _GraphExtractor(config.graph)
        _graph_expander = _GraphExpander(_graph_store, naive_max_expansion_terms=config.graph.naive_max_expansion_terms)
        _defref_extractor = _DefRefExtractor(_graph_store)
        _ppr_walker = _PPRWalker(_graph_store)
    else:
        _graph_store = None
        _graph_extractor = None
        _graph_expander = None
        _defref_extractor = None
        _ppr_walker = None
    app.state.graph_store = _graph_store

    app.state.pipeline = SearchPipeline(
        store=app.state.search_store,
        embedder=app.state.embedder,
        reranker=(
            Reranker(_reranker_backend(config.reranker_model, config.device))
            if config.reranker_model
            else None
        ),
        chunker=DocumentChunker(config.chunk_size),
        ast_chunker=ASTChunker(config.chunk_size),
        parser=DocumentParser.from_config(config.ingest),
        top_k_retrieve=config.top_k_retrieve,
        top_k_return=config.top_k_return,
        max_fanout=config.max_fanout,
        fanout_leg_trim=config.fanout_leg_trim,
        fanout_timeout_seconds=config.fanout_timeout_seconds,
        language_detector=_lang_detector,
        language_detection_confidence_threshold=config.language_detection_confidence_threshold,
        max_file_mb=config.ingest.max_file_mb,
        max_discovered_files=config.ingest.max_discovered_files,
        path_policy=app.state.ingest_path_policy,
        context_window_max=config.context_window_max,
        graph_extractor=_graph_extractor,
        graph_store=_graph_store,
        graph_config=config.graph,
        graph_expander=_graph_expander,
        defref_extractor=_defref_extractor,
        ppr_walker=_ppr_walker,
    )
    from archon_search.sync import SearchCollectionSync

    # asyncio.Lock() is created here (in create_app's synchronous body, matching
    # the established app.state.* pattern). On Python 3.12+ the lock binds to
    # the running loop lazily on first use, so construction here is correct.
    app.state.sync_lock = asyncio.Lock()
    app.state.collection_sync = SearchCollectionSync(
        pipeline=app.state.pipeline,
        state_store=app.state.state_store,
        pinned_collections=config.pinned_collections,
        chunk_size=config.chunk_size,
        auto_reindex_on_chunk_size_change=config.auto_reindex_on_chunk_size_change,
        device=config.device,
        job_store=job_store,
        configured_paths=lambda: (*config.pinned_collections, *config.collections),
    )

    from archon_search.hyde import HyDEGenerator
    from archon_search.rag_fusion import RAGFusionGenerator

    _hyde_provider = _build_query_expansion_provider(
        config.hyde.provider, config.hyde.model, config.hyde.ollama_base_url, config.hyde.llama_cpp_base_url
    )
    app.state.hyde_generator = HyDEGenerator(config=config.hyde, provider=_hyde_provider)
    if config.hyde.enabled:
        logger.info(
            "HyDE is enabled — search query text will be sent to %s (model: %s)",
            config.hyde.provider,
            config.hyde.model,
        )

    _rag_fusion_provider = _build_query_expansion_provider(
        config.rag_fusion.provider, config.rag_fusion.model, config.rag_fusion.ollama_base_url, config.rag_fusion.llama_cpp_base_url
    )
    app.state.rag_fusion_generator = RAGFusionGenerator(config=config.rag_fusion, provider=_rag_fusion_provider)
    if config.rag_fusion.enabled:
        logger.info(
            "RAG Fusion is enabled — search query text will be sent to %s (model: %s)",
            config.rag_fusion.provider,
            config.rag_fusion.model,
        )
    @app.exception_handler(EmbedderNotReadyError)
    async def _embedder_not_ready_handler(request: Request, exc: EmbedderNotReadyError) -> JSONResponse:
        """Map a wedged/slow model load to a uniform 503 across every HTTP route.

        ``routes_search.search`` keeps its own local ``except EmbedderNotReadyError``
        handler (it also emits the ExitStack-scoped stage-timings recorder before
        returning — this generic handler has no access to that recorder); every
        other route that resolves an embedder via ``EmbedderCache.get_or_load``
        without its own handling (``/explain``, ``/v1/chat/completions``) falls
        through to this one instead of surfacing as a 500.

        ``mcp.py`` is NOT covered here, for two independent reasons: (1) it is a
        mounted Starlette sub-app (``app.mount("/mcp", mcp_starlette)``), which
        does not share this app's ``exception_handlers`` registry — a FastMCP/
        Starlette app builds its own middleware stack; and (2) every MCP tool
        already wraps its body in its own ``try/except Exception`` that returns an
        ``McpErrorResponse`` dict rather than raising, so nothing ever reaches an
        ASGI-level exception handler regardless. Each tool therefore catches
        ``EmbedderNotReadyError`` explicitly, before its generic ``except
        Exception``, and returns the dict directly (see ``mcp.py``).
        """
        logger.warning("embedder not ready: %s", exc)
        # Retry-After matches every other retryable 503 in the server
        # (_ingest_lock.py, routes_collections.py) — same condition, same hint,
        # regardless of which surface (REST, OpenAI shim) maps it.
        if request.url.path.startswith("/v1/"):
            from archon_search.server.routes_openai_shim import _openai_error
            response = _openai_error(503, "Service temporarily unavailable.", "server_error")
            response.headers["Retry-After"] = "30"
            return response
        return JSONResponse(
            {"detail": EMBEDDER_NOT_READY_DETAIL, "code": EMBEDDER_NOT_READY_CODE},
            status_code=503,
            headers={"Retry-After": "30"},
        )

    app.include_router(collections_router)
    app.include_router(export_router, prefix="/collections")
    app.include_router(graph_router)
    app.include_router(health_router)
    app.include_router(ready_router)
    app.include_router(jobs_router)
    app.include_router(status_router)
    app.include_router(state_router)
    app.include_router(route_router)
    app.include_router(search_router)
    app.include_router(explain_router)
    app.include_router(telemetry_router)
    app.include_router(backup_router)
    app.include_router(maintenance_router)
    app.include_router(sync_router)
    app.include_router(keys_router)
    # OpenAI-compatible shim routes — guarded separately from the middleware guard
    # above (they live in different regions of create_app).  When disabled, no
    # /v1/* routes are registered and the app behaves as if the feature does not
    # exist.
    if config.openai_shim.enabled:
        from fastapi.exception_handlers import request_validation_exception_handler
        from fastapi.exceptions import RequestValidationError

        from archon_search.server.routes_openai_shim import _openai_error
        from archon_search.server.routes_openai_shim import router as openai_shim_router

        @app.exception_handler(RequestValidationError)
        async def _openai_validation_error_handler(request: Request, exc: RequestValidationError):
            if not request.url.path.startswith("/v1/"):
                return await request_validation_exception_handler(request, exc)
            # RequestValidationError.errors() always returns at least one entry.
            # Filter "body" from the loc tuple — body-validated fields carry it as a
            # prefix; query/path/header params do not, so filtering by value (not position)
            # is safe for all current /v1/* routes (body-only validation today).
            errors = exc.errors()
            loc = [str(x) for x in errors[0]["loc"] if x != "body"]
            field = ".".join(loc) if loc else "unknown"
            return _openai_error(422, f"Invalid value for field '{field}'.", "invalid_request_error")

        app.include_router(openai_shim_router, prefix="/v1")
    _configure_openapi(app)
    return app


def run_server(config: SearchConfig, config_path: Path | str | None = None) -> None:
    """Create JobStore, build the app, and start the uvicorn server.

    ``config_path`` defaults to ``get_default_config_path()`` and is forwarded to
    ``create_app`` as ``app.state.config_path``. This is what makes runtime config
    persistence effective — ``_maybe_save_config`` (``collection add``/``remove``)
    no-ops when it is ``None`` (the S07 / S252 root cause).

    When no ``config_path`` is given, the ignored-legacy-config WARNING
    (``warn_if_legacy_config_ignored``) is emitted right after
    ``configure_logging`` so it reaches the configured ``log_file`` / format.
    """
    effective_config_path = config_path or get_default_config_path()
    configure_logging(config)
    warn_if_legacy_config_ignored(config_path)
    job_store = JobStore()

    # Defensive placeholder dispatch: the lifespan handler in ``create_app()``
    # reassigns ``scheduler.dispatch_fn`` to the real export/import closure as
    # soon as app state is ready. This placeholder should never run in practice
    # — if it does, something dispatched before startup completed.
    from archon_search.types import ExportJob, ImportJob, MigrationJob

    def _placeholder_dispatch(job: ExportJob | ImportJob | MigrationJob) -> None:
        logger.warning(
            "JobScheduler placeholder dispatch invoked before lifespan startup "
            "completed for job %s; this should not happen",
            job.job_id,
        )

    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=config.jobs.max_concurrent_bulk,
        dispatch_fn=_placeholder_dispatch,
    )
    app = create_app(config, job_store, scheduler=scheduler, config_path=effective_config_path)
    uvicorn_kwargs: dict = {
        "host": config.host,
        "port": config.port,
        "timeout_graceful_shutdown": config.graceful_shutdown_seconds,
    }
    log_config = build_uvicorn_log_config(config)
    if log_config is not None:
        uvicorn_kwargs["log_config"] = log_config
    uvicorn.run(app, **uvicorn_kwargs)
