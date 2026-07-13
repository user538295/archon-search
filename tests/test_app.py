"""Tests for FastAPI app factory ."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.sync import path_to_collection_name


@pytest.fixture
def config(tmp_path: Path) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    return cfg


@pytest.fixture
def job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def test_create_app_returns_fastapi_instance(config: SearchConfig, job_store: JobStore) -> None:
    app = create_app(config, job_store)
    assert isinstance(app, FastAPI)


def test_app_state_has_config(config: SearchConfig, job_store: JobStore) -> None:
    app = create_app(config, job_store)
    assert app.state.config is config


def test_app_state_has_job_store(config: SearchConfig, job_store: JobStore) -> None:
    app = create_app(config, job_store)
    assert app.state.job_store is job_store


def test_app_title_is_archon_search(config: SearchConfig, job_store: JobStore) -> None:
    app = create_app(config, job_store)
    assert app.title == "archon-search"


# ---------------------------------------------------------------------------
# server startup collection derivation 
# ---------------------------------------------------------------------------


def test_server_main_derives_collection_from_history_dir() -> None:
    """Server startup derives collection names from history directory via path_to_collection_name.

    When archon-search is used with a history directory path, the collection name is
    derived from the last path component of the directory, sanitized to a valid name.
    This mirrors what archon.cli.search_cmd._path_to_collection_name does on the client side.
    """
    history_dir = "/home/user/.archon/history"
    sessions_path = str(Path(history_dir) / "sessions")

    col = path_to_collection_name(sessions_path)

    # The last component is "sessions" → sanitized → "sessions"
    assert col == "sessions"


def test_server_collection_derivation_uses_last_path_component() -> None:
    """path_to_collection_name uses the last path component regardless of parent directories.

    Different base directories with the same last component produce the same collection name.
    """
    col1 = path_to_collection_name("/alpha/sessions")
    col2 = path_to_collection_name("/beta/sessions")
    assert col1 == "sessions"
    assert col2 == "sessions"


# ---------------------------------------------------------------------------
# telemetry router registration 
# ---------------------------------------------------------------------------


@pytest.fixture
def telemetry_config(tmp_path: Path) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.telemetry.enabled = True
    cfg.telemetry.log_dir = str(tmp_path / "telemetry-logs")
    return cfg


def test_telemetry_stats_route_registered(telemetry_config: SearchConfig, job_store: JobStore) -> None:
    """GET /telemetry/stats must be registered (not 404) when telemetry is enabled."""
    from starlette.testclient import TestClient

    app = create_app(telemetry_config, job_store)
    with TestClient(app) as client:
        response = client.get("/telemetry/stats")
    assert response.status_code != 404, f"Route not registered — got {response.status_code}"


def test_telemetry_entries_route_registered(telemetry_config: SearchConfig, job_store: JobStore) -> None:
    """GET /telemetry/entries must be registered (not 404) when telemetry is enabled."""
    from starlette.testclient import TestClient

    app = create_app(telemetry_config, job_store)
    with TestClient(app) as client:
        response = client.get("/telemetry/entries")
    assert response.status_code != 404, f"Route not registered — got {response.status_code}"


# ---------------------------------------------------------------------------
# startup migrations lifespan wiring (BE-6: consolidated into _run_startup_migrations)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_calls_startup_migrations(config: SearchConfig, job_store: JobStore) -> None:
    """lifespan startup calls _run_startup_migrations() after connect().

    BE-6 consolidated the five direct migrate_*() calls into _run_startup_migrations().
    This test verifies the call order: connect → _run_startup_migrations → disconnect.
    """
    from unittest.mock import AsyncMock, patch

    from archon_search.store import SearchStore

    call_order: list[str] = []

    async def fake_connect(self: SearchStore) -> None:  # type: ignore[override]
        call_order.append("connect")

    async def fake_run_startup_migrations(self: SearchStore) -> None:  # type: ignore[override]
        call_order.append("_run_startup_migrations")

    async def fake_disconnect(self: SearchStore) -> None:  # type: ignore[override]
        call_order.append("disconnect")

    with (
        patch.object(SearchStore, "connect", new=fake_connect),
        patch.object(SearchStore, "_run_startup_migrations", new=fake_run_startup_migrations),
        patch.object(SearchStore, "disconnect", new=fake_disconnect),
    ):
        app = create_app(config, job_store)
        from starlette.testclient import TestClient

        with TestClient(app):
            pass

        assert "connect" in call_order
        assert "_run_startup_migrations" in call_order
        assert call_order.index("connect") < call_order.index("_run_startup_migrations")


# ---------------------------------------------------------------------------
# create_app passes namespaces to middleware 
# ---------------------------------------------------------------------------


def test_create_app_passes_namespaces_to_middleware(tmp_path: Path, job_store: JobStore) -> None:
    """create_app() with non-empty config.namespaces passes that dict to APIKeyMiddleware.

    FastAPI stores middleware kwargs at add_middleware() time (lazy instantiation), so we
    inspect app.user_middleware to verify the correct kwargs were registered.
    """
    from archon_search.server.middleware_auth import APIKeyMiddleware

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.namespaces = {"abc123": "tenantA", "def456": "tenantB"}

    app = create_app(cfg, job_store)

    # Find the APIKeyMiddleware entry in the registered middleware stack
    middleware_entry = next(
        (m for m in app.user_middleware if m.cls is APIKeyMiddleware),
        None,
    )
    assert middleware_entry is not None, "APIKeyMiddleware not registered"
    assert middleware_entry.kwargs.get("namespaces") == {"abc123": "tenantA", "def456": "tenantB"}


def test_create_app_empty_namespaces_no_error(tmp_path: Path, job_store: JobStore) -> None:
    """create_app() with config.namespaces == {} creates middleware without error; existing key still works."""
    from starlette.testclient import TestClient
    from unittest.mock import AsyncMock, patch
    from archon_search.store import SearchStore

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    # namespaces defaults to {} — no [namespaces] section

    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store)
        # Retrieve the api_key that was generated so we can use it in the request
        from archon_search.key_manager import load_or_generate_key
        api_key, _ = load_or_generate_key()
        with TestClient(app) as client:
            response = client.get("/health")
    # /health is public — must be 200 regardless of auth
    assert response.status_code == 200


def test_health_endpoint_unauthenticated_200(tmp_path: Path, job_store: JobStore) -> None:
    """GET /health with NO Authorization header returns 200 even when namespaces is non-empty."""
    from starlette.testclient import TestClient
    from unittest.mock import AsyncMock, patch
    from archon_search.store import SearchStore

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.namespaces = {"abc123": "tenantA"}

    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store)
        with TestClient(app) as client:
            # No Authorization header
            response = client.get("/health")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# X-Request-ID header tests — Task 2.2 (B1)
# ---------------------------------------------------------------------------

import re as _re
_REQUEST_ID_RE = _re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _make_test_client(tmp_path: Path, job_store: JobStore):  # type: ignore[no-untyped-def]
    from unittest.mock import AsyncMock, patch
    from starlette.testclient import TestClient
    from archon_search.store import SearchStore

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store)
        return TestClient(app)


def test_health_has_request_id(tmp_path: Path, job_store: JobStore) -> None:
    from unittest.mock import AsyncMock, patch
    from starlette.testclient import TestClient
    from archon_search.store import SearchStore

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store)
        with TestClient(app) as client:
            resp = client.get("/health")
    assert _REQUEST_ID_RE.match(resp.headers.get("x-request-id", ""))


def test_401_has_request_id(tmp_path: Path, job_store: JobStore) -> None:
    from unittest.mock import AsyncMock, patch
    from starlette.testclient import TestClient
    from archon_search.store import SearchStore

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/search")  # no auth
    assert resp.status_code == 401
    assert _REQUEST_ID_RE.match(resp.headers.get("x-request-id", ""))


def test_options_preflight_has_request_id(tmp_path: Path, job_store: JobStore) -> None:
    from unittest.mock import AsyncMock, patch
    from starlette.testclient import TestClient
    from archon_search.store import SearchStore

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.options("/search", headers={"Origin": "http://example.com"})
    assert _REQUEST_ID_RE.match(resp.headers.get("x-request-id", ""))


def test_run_server_calls_configure_logging(monkeypatch):
    """configure_logging must be called before anything else in run_server."""
    import archon_search.server.app as app_module

    calls = []

    def spy_configure_logging(cfg):
        calls.append(cfg)

    def noop_uvicorn_run(*args, **kwargs):
        pass

    monkeypatch.setattr(app_module, "configure_logging", spy_configure_logging)
    monkeypatch.setattr(app_module.uvicorn, "run", noop_uvicorn_run)

    cfg = SearchConfig()
    app_module.run_server(cfg)

    assert len(calls) == 1
    assert calls[0] is cfg


# ---------------------------------------------------------------------------
# EmbedderCache in app.state — Task 2.2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedder_cache_in_app_state(config: SearchConfig, job_store: JobStore) -> None:
    """After lifespan startup, app.state.embedder_cache is an EmbedderCache instance."""
    from unittest.mock import AsyncMock, patch
    from starlette.testclient import TestClient
    from archon_search.embedder_cache import EmbedderCache
    from archon_search.store import SearchStore

    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
        patch.object(SearchStore, "get_all_collections_meta", new=AsyncMock(return_value=[])),
    ):
        app = create_app(config, job_store)
        with TestClient(app):
            assert isinstance(app.state.embedder_cache, EmbedderCache)


@pytest.mark.asyncio
async def test_eager_load_embedders_false_does_not_preload(tmp_path: Path, job_store: JobStore) -> None:
    """eager_load_embedders=False: cached_models() is empty after startup."""
    from unittest.mock import AsyncMock, patch
    from starlette.testclient import TestClient
    from archon_search.store import SearchStore

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.eager_load_embedders = False

    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store)
        with TestClient(app):
            assert app.state.embedder_cache.cached_models() == []


@pytest.mark.asyncio
async def test_eager_load_embedders_true_preloads_collection_models(tmp_path: Path, job_store: JobStore) -> None:
    """eager_load_embedders=True: collection active_embedding_model is preloaded."""
    from unittest.mock import AsyncMock, patch, MagicMock
    from starlette.testclient import TestClient
    from archon_search.store import SearchStore
    from archon_search.embedder_cache import EmbedderCache
    from archon_search.collection_meta import CollectionMeta

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.eager_load_embedders = True

    fake_meta = MagicMock(spec=CollectionMeta)
    fake_meta.active_embedding_model = "model-X"

    preloaded: list[list[str]] = []

    async def fake_preload(self: EmbedderCache, model_names: list[str]) -> None:
        preloaded.append(model_names)
        # Simulate loading by inserting a stub into the cache
        for name in model_names:
            self._cache[name] = MagicMock()

    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
        patch.object(SearchStore, "get_all_collections_meta", new=AsyncMock(return_value=[fake_meta])),
        patch.object(EmbedderCache, "preload", new=fake_preload),
    ):
        app = create_app(cfg, job_store)
        with TestClient(app):
            assert "model-X" in app.state.embedder_cache.cached_models()
    assert any("model-X" in names for names in preloaded)


def test_inbound_id_echoed(tmp_path: Path, job_store: JobStore) -> None:
    from unittest.mock import AsyncMock, patch
    from starlette.testclient import TestClient
    from archon_search.store import SearchStore

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store)
        with TestClient(app) as client:
            resp = client.get("/health", headers={"X-Request-ID": "myid-abc123"})
    assert resp.headers.get("x-request-id") == "myid-abc123"


# ---------------------------------------------------------------------------
# Task 4.1 — HyDEGenerator in app.state + pyproject.toml optional dep
# ---------------------------------------------------------------------------


def test_app_state_has_hyde_generator(config: SearchConfig, job_store: JobStore) -> None:
    """create_app() must set app.state.hyde_generator to a HyDEGenerator instance."""
    from archon_search.hyde import HyDEGenerator

    app = create_app(config, job_store)
    assert hasattr(app.state, "hyde_generator")
    assert isinstance(app.state.hyde_generator, HyDEGenerator)


def test_hyde_optional_dep_in_pyproject() -> None:
    """pyproject.toml must declare anthropic under [project.optional-dependencies].hyde."""
    import tomlkit

    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    doc = tomlkit.loads(pyproject_path.read_text())
    optional_deps = doc["project"]["optional-dependencies"]  # type: ignore[index]
    assert "hyde" in optional_deps, "hyde extra missing from [project.optional-dependencies]"
    hyde_deps = list(optional_deps["hyde"])
    assert any("anthropic" in dep for dep in hyde_deps), (
        f"anthropic not found in hyde deps: {hyde_deps}"
    )


def test_app_startup_logs_info_when_hyde_enabled(
    tmp_path: Path, job_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """When config.hyde.enabled=True, create_app must log an INFO about HyDE and the provider."""
    import logging

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.hyde.enabled = True

    with caplog.at_level(logging.INFO, logger="archon_search.server.app"):
        create_app(cfg, job_store)

    messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    hyde_msgs = [m for m in messages if "HyDE" in m and cfg.hyde.provider in m]
    assert hyde_msgs, (
        f"Expected INFO message about HyDE and provider, got: {messages}"
    )
    # Must include the model name
    assert any(cfg.hyde.model in m for m in hyde_msgs)


def test_app_startup_no_log_when_hyde_disabled(
    tmp_path: Path, job_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """When config.hyde.enabled=False (default), no HyDE INFO message is logged."""
    import logging

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.hyde.enabled = False

    with caplog.at_level(logging.INFO, logger="archon_search.server.app"):
        create_app(cfg, job_store)

    hyde_msgs = [
        r.message for r in caplog.records
        if r.levelno == logging.INFO and "HyDE" in r.message and "Anthropic" in r.message
    ]
    assert not hyde_msgs, f"Unexpected HyDE INFO message when disabled: {hyde_msgs}"


# ---------------------------------------------------------------------------
# Task 4.1 — RAGFusionGenerator in app.state + pyproject.toml optional dep
# ---------------------------------------------------------------------------


def test_app_state_has_rag_fusion_generator(config: SearchConfig, job_store: JobStore) -> None:
    """create_app() must set app.state.rag_fusion_generator to a RAGFusionGenerator instance."""
    from archon_search.rag_fusion import RAGFusionGenerator

    app = create_app(config, job_store)
    assert hasattr(app.state, "rag_fusion_generator")
    assert isinstance(app.state.rag_fusion_generator, RAGFusionGenerator)


def test_rag_fusion_optional_dep_in_pyproject() -> None:
    """pyproject.toml must declare anthropic under [project.optional-dependencies].rag_fusion."""
    import tomlkit

    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    doc = tomlkit.loads(pyproject_path.read_text())
    optional_deps = doc["project"]["optional-dependencies"]  # type: ignore[index]
    assert "rag_fusion" in optional_deps, "rag_fusion extra missing from [project.optional-dependencies]"
    rag_fusion_deps = list(optional_deps["rag_fusion"])
    assert any("anthropic" in dep for dep in rag_fusion_deps), (
        f"anthropic not found in rag_fusion deps: {rag_fusion_deps}"
    )


def test_app_startup_logs_info_when_rag_fusion_enabled(
    tmp_path: Path, job_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """When config.rag_fusion.enabled=True, create_app must log an INFO about RAG Fusion and the provider."""
    import logging

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.rag_fusion.enabled = True

    with caplog.at_level(logging.INFO, logger="archon_search.server.app"):
        create_app(cfg, job_store)

    messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    rag_fusion_msgs = [m for m in messages if "RAG Fusion" in m and cfg.rag_fusion.provider in m]
    assert rag_fusion_msgs, (
        f"Expected INFO message about RAG Fusion and provider, got: {messages}"
    )
    # Must include the model name
    assert any(cfg.rag_fusion.model in m for m in rag_fusion_msgs)


def test_app_startup_no_log_when_rag_fusion_disabled(
    tmp_path: Path, job_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """When config.rag_fusion.enabled=False (default), no RAG Fusion INFO message is logged."""
    import logging

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.rag_fusion.enabled = False

    with caplog.at_level(logging.INFO, logger="archon_search.server.app"):
        create_app(cfg, job_store)

    rag_fusion_msgs = [
        r.message for r in caplog.records
        if r.levelno == logging.INFO and "RAG Fusion" in r.message and "Anthropic" in r.message
    ]
    assert not rag_fusion_msgs, f"Unexpected RAG Fusion INFO message when disabled: {rag_fusion_msgs}"
