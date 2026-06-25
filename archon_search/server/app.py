"""FastAPI app factory for archon-search REST control plane."""
from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncGenerator

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from archon_search.chunker import DocumentChunker
from archon_search.config import SearchConfig
from archon_search.language_detector import FASTTEXT_MODEL_FILENAME, get_fasttext_models_dir
from archon_search.embedder import Embedder, ModelEmbedder
from archon_search.embedder_cache import EmbedderCache
from archon_search.jobs.backup_loop import BackupLoop
from archon_search.jobs.maintenance_loop import MaintenanceLoop
from archon_search.jobs.scheduler import JobScheduler
from archon_search.jobs.store import JobStore
from archon_search.key_manager import KeyRecord, KeyStore, load_or_generate_key
from archon_search.paths import get_data_dir
from archon_search.logging_setup import configure_logging
from archon_search.model_validation import ModelValidationResult, validate_models_async
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline
from archon_search.progress import IndexingStateStore
from archon_search.reranker import ModelReranker, Reranker
from archon_search.server.middleware_auth import APIKeyMiddleware, _EXEMPT_PATHS
from archon_search.server.middleware_context import RequestContextMiddleware
from archon_search.store import SearchStore

try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
    _VERSION = _pkg_version("archon-search")
except PackageNotFoundError:
    _VERSION = "dev"

from archon_search.server.routes_backup import router as backup_router
from archon_search.server.routes_keys import router as keys_router
from archon_search.server.routes_maintenance import router as maintenance_router
from archon_search.server.routes_collections import router as collections_router
from archon_search.server.routes_explain import router as explain_router
from archon_search.server.routes_export import router as export_router
from archon_search.server.routes_health import router as health_router
from archon_search.server.routes_jobs import router as jobs_router
from archon_search.server.routes_ready import router as ready_router
from archon_search.server.routes_route import router as route_router
from archon_search.server.routes_search import router as search_router
from archon_search.server.routes_state import router as state_router
from archon_search.server.routes_status import router as status_router
from archon_search.server.routes_telemetry import router as telemetry_router
from archon_search.telemetry.hasher import hash_doc_id, load_or_create_salt
from archon_search.telemetry.pruner import Pruner
from archon_search.telemetry.writer import TelemetryWriter

logger = logging.getLogger(__name__)


def _multilingual_model_path() -> Path:
    """Return the lid.176.ftz model path, resolved lazily on every call.

    Derived from ``get_fasttext_models_dir()`` so ``ARCHON_SEARCH_DATA_DIR``
    redirects the model lookup at call time, not at import time.
    """
    return get_fasttext_models_dir() / FASTTEXT_MODEL_FILENAME


def _import_fasttext() -> Any:
    """Import fasttext; raises ImportError if the package is not installed."""
    import fasttext  # type: ignore[import-untyped]  # noqa: PLC0415
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
            "run: pip install archon-search[multilingual]"
        )

    # Check 2 — model file presence
    if not _multilingual_model_path().exists():
        raise RuntimeError(
            "multilingual=true but lid.176.ftz model is missing; "
            "run: archon-search install --multilingual"
        )


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
            for _method, operation in path_item.items():
                if isinstance(operation, dict):
                    operation["security"] = [{"BearerAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def create_app(
    config: SearchConfig,
    job_store: JobStore,
    config_path: Path | str | None = None,
    scheduler: JobScheduler | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    _check_multilingual_deps(config)
    api_key, key_source = load_or_generate_key()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Startup: persist TOML synthetic KeyRecord objects into keys.json so that
        # GET /keys (BE-6) can surface them and active_keys() includes them.
        # This replaces synthetic records from previous runs with the current TOML state.
        await app.state.key_store.load_synthetic_records(_synthetic_records)

        # Startup: connect search store
        await app.state.search_store.connect()
        await app.state.search_store._run_startup_migrations()

        # Startup: create embedder cache and optionally preload models
        embedder_cache = EmbedderCache(config.embedder_cache_size)
        app.state.embedder_cache = embedder_cache
        if config.eager_load_embedders:
            metas = await app.state.search_store.get_all_collections_meta()
            distinct_models = {m.active_embedding_model for m in metas if m.active_embedding_model}
            await embedder_cache.preload(list(distinct_models))

        # All startup migrations complete before the lifespan context yields control to the request loop

        # Startup: spawn background model validation (D6 / BE-4). Never blocks
        # startup — the lazy-load contract is hard. The result is stored on
        # ``app.state.model_validation`` (None while pending) and surfaced via
        # ``GET /status`` and ``GET /ready``. ``embedder_is_warm`` is read from
        # the global default embedder: only True when it has been exercised
        # directly (eager_load_embedders warms per-collection caches, not this
        # global instance, so it is typically False at startup).
        app.state.model_validation = None

        async def _run_model_validation() -> None:
            embedder_is_warm = app.state.embedder.is_warm
            try:
                app.state.model_validation = await validate_models_async(
                    config,
                    timeout_seconds=config.validation_timeout_seconds,
                    embedder_is_warm=embedder_is_warm,
                )
            except asyncio.CancelledError:
                logger.info("validation cancelled during shutdown")
                raise
            except BaseException as exc:  # noqa: BLE001 — never let the task escape
                logger.warning("model validation task failed unexpectedly: %s", exc)
                app.state.model_validation = ModelValidationResult(
                    embedder_ok=False,
                    reranker_ok=False,
                    provider_warnings=["validation task failed unexpectedly"],
                    validated_at=datetime.now(UTC),
                )

        validation_task = asyncio.create_task(_run_model_validation())
        app.state._background_tasks.add(validation_task)
        validation_task.add_done_callback(app.state._background_tasks.discard)

        # Startup: warn if the multi-collection fan-out validation cap is out of
        # sync with the configured max_fanout.
        from archon_search.server.routes_search import _FANOUT_VALIDATION_LIMIT
        if config.max_fanout != _FANOUT_VALIDATION_LIMIT:
            logger.warning(
                "max_fanout config (%d) differs from _FANOUT_VALIDATION_LIMIT constant (%d) in routes_search.py; "
                "update the constant or requests with >%d collections will be rejected",
                config.max_fanout, _FANOUT_VALIDATION_LIMIT, _FANOUT_VALIDATION_LIMIT,
            )

        # Startup: install the real export/import dispatch closure on the
        # scheduler and start it. The placeholder dispatch passed to
        # ``JobScheduler(...)`` in ``run_server()`` is replaced here, once
        # ``app.state.search_store``, ``pipeline``, and ``embedder_cache`` are
        # ready. Without this reassignment the scheduler would either fail
        # outright or mark every dispatched job FAILED.
        if scheduler is not None:
            from archon_search.server.routes_export import (  # noqa: PLC0415
                _export_task,
                _import_task,
            )
            from archon_search.types import ExportJob, ImportJob, MigrationJob  # noqa: PLC0415

            def _real_dispatch(job: ExportJob | ImportJob | MigrationJob) -> None:
                if isinstance(job, ExportJob):
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
                    from archon_search.server.routes_collections import _migration_task  # noqa: PLC0415
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
        )
        app.state.maintenance_loop = maintenance_loop
        maintenance_task = asyncio.create_task(maintenance_loop.run())
        app.state._background_tasks.add(maintenance_task)
        maintenance_task.add_done_callback(app.state._background_tasks.discard)

        # Startup: initialise telemetry if enabled
        if config.telemetry.enabled:
            log_dir = Path(config.telemetry.log_dir).expanduser()
            log_dir.mkdir(parents=True, exist_ok=True)
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
                        from archon_search.server.mcp import create_mcp_http_app  # noqa: PLC0415
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
                        )
                        await _mcp_stack.enter_async_context(
                            mcp_starlette.router.lifespan_context(app)
                        )
                        app.mount("/mcp", mcp_starlette)
                        logger.info("MCP HTTP endpoint mounted at /mcp")
                        app.state.mcp_bound = True
                    except Exception:  # noqa: BLE001 — MCP must never block REST startup
                        logger.warning(
                            "MCP server failed to start; continuing without MCP", exc_info=True
                        )

                yield
        finally:
            # Shutdown: disconnect search store
            await app.state.search_store.disconnect()

            # Shutdown: drain writer before cancelling background tasks
            if app.state.telemetry_writer is not None:
                await app.state.telemetry_writer.drain_and_stop()
            tasks = list(app.state._background_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # Instantiate the key store pointing to keys.json under the data directory.
    # Each app (HTTP and MCP) creates its own KeyStore instance; cross-process
    # visibility is achieved because active_keys() re-reads from disk on every call.
    key_store = KeyStore(get_data_dir() / "keys.json")

    # Build synthetic KeyRecord objects from the TOML [namespaces] map once at
    # construction time. The actual write to keys.json happens inside the lifespan
    # (async context). Synthetic records have id=None and no expires_at.
    _synthetic_records = [
        KeyRecord(
            id=None,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            namespace=ns,
            label=ns,  # label mirrors the TOML namespace name for operator identification
            created_at=datetime.now(UTC),
            expires_at=None,
            status="active",
        )
        for raw_token, ns in config.namespaces.items()
    ]

    app = FastAPI(title="archon-search", lifespan=lifespan)
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
    )
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(
        RequestContextMiddleware,
        header_name=config.observability.request_id_header,
    )
    logger.info("API key authentication enabled (source: %s)", key_source)
    app.state.key_store = key_store
    # Store the raw api_key token so POST /keys/rotate can read the current
    # default key without re-reading .search.env.  The route handler updates
    # this in-process value after a successful rotation.
    app.state.api_key = api_key
    app.state.config = config
    app.state.job_store = job_store
    app.state.config_path = Path(config_path) if config_path is not None else None
    app.state._background_tasks: set = set()
    app.state.state_store = IndexingStateStore(config.db_path)
    app.state.search_store = SearchStore(config.db_path)
    app.state.watcher_manager = None
    app.state.embedder = Embedder(ModelEmbedder(config.embedding_model, providers=config.providers or None))

    # C2: instantiate LanguageDetector for production path when multilingual=True.
    # _check_multilingual_deps() has already passed at this point, so imports are safe.
    if config.multilingual:
        from archon_search.language_detector import LanguageDetector  # noqa: PLC0415
        _lang_detector = LanguageDetector(_multilingual_model_path())
    else:
        _lang_detector = None

    app.state.pipeline = SearchPipeline(
        store=app.state.search_store,
        embedder=app.state.embedder,
        reranker=(
            Reranker(ModelReranker(config.reranker_model, providers=config.providers or None))
            if config.reranker_model
            else None
        ),
        chunker=DocumentChunker(config.chunk_size),
        parser=DocumentParser(),
        top_k_retrieve=config.top_k_retrieve,
        top_k_return=config.top_k_return,
        language_detector=_lang_detector,
        language_detection_confidence_threshold=config.language_detection_confidence_threshold,
    )
    from archon_search.hyde import HyDEGenerator  # noqa: PLC0415
    app.state.hyde_generator = HyDEGenerator(embedder=app.state.embedder, config=config.hyde)
    if config.hyde.enabled:
        logger.info(
            "HyDE is enabled — search query text will be sent to Anthropic's API (model: %s)",
            config.hyde.model,
        )
    from archon_search.rag_fusion import RAGFusionGenerator  # noqa: PLC0415
    app.state.rag_fusion_generator = RAGFusionGenerator(config=config.rag_fusion)
    if config.rag_fusion.enabled:
        logger.info(
            "RAG Fusion is enabled — search query text will be sent to Anthropic's API (model: %s)",
            config.rag_fusion.model,
        )
    app.include_router(collections_router)
    app.include_router(export_router, prefix="/collections")
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
    app.include_router(keys_router)
    _configure_openapi(app)
    return app


def run_server(config: SearchConfig) -> None:
    """Create JobStore, build the app, and start the uvicorn server."""
    configure_logging(config)
    job_store = JobStore()

    # Defensive placeholder dispatch: the lifespan handler in ``create_app()``
    # reassigns ``scheduler.dispatch_fn`` to the real export/import closure as
    # soon as app state is ready. This placeholder should never run in practice
    # — if it does, something dispatched before startup completed.
    from archon_search.types import ExportJob, ImportJob, MigrationJob  # noqa: PLC0415

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
    app = create_app(config, job_store, scheduler=scheduler)
    uvicorn.run(app, host=config.host, port=config.port)
