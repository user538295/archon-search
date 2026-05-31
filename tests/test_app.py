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
# migrate_namespace lifespan wiring 
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_calls_migrate_namespace(config: SearchConfig, job_store: JobStore) -> None:
    from unittest.mock import AsyncMock, patch

    from archon_search.store import SearchStore

    call_order: list[str] = []

    async def fake_connect(self: SearchStore) -> None:  # type: ignore[override]
        call_order.append("connect")

    async def fake_migrate(self: SearchStore) -> None:  # type: ignore[override]
        call_order.append("migrate_namespace")

    async def fake_disconnect(self: SearchStore) -> None:  # type: ignore[override]
        call_order.append("disconnect")

    async def fake_migrate_acl(self: SearchStore) -> None:  # type: ignore[override]
        call_order.append("migrate_acl")

    async def fake_migrate_description_embedding(self: SearchStore) -> None:  # type: ignore[override]
        call_order.append("migrate_description_embedding")

    async def fake_migrate_centroid_sum(self: SearchStore) -> None:  # type: ignore[override]
        call_order.append("migrate_centroid_sum")

    async def fake_migrate_per_collection_model(self: SearchStore) -> None:  # type: ignore[override]
        call_order.append("migrate_per_collection_model")

    with (
        patch.object(SearchStore, "connect", new=fake_connect),
        patch.object(SearchStore, "migrate_namespace", new=fake_migrate),
        patch.object(SearchStore, "migrate_description_embedding", new=fake_migrate_description_embedding),
        patch.object(SearchStore, "migrate_acl", new=fake_migrate_acl),
        patch.object(SearchStore, "migrate_centroid_sum", new=fake_migrate_centroid_sum),
        patch.object(SearchStore, "migrate_per_collection_model", new=fake_migrate_per_collection_model),
        patch.object(SearchStore, "disconnect", new=fake_disconnect),
    ):
        app = create_app(config, job_store)
        from starlette.testclient import TestClient

        with TestClient(app):
            pass

        assert "connect" in call_order
        assert "migrate_namespace" in call_order
        assert "migrate_description_embedding" in call_order
        assert call_order.index("connect") < call_order.index("migrate_namespace")
        assert call_order.index("migrate_namespace") < call_order.index("migrate_description_embedding")
        assert call_order.index("migrate_description_embedding") < call_order.index("migrate_acl")
        assert call_order.index("migrate_acl") < call_order.index("migrate_centroid_sum")
        assert call_order.index("migrate_centroid_sum") < call_order.index("migrate_per_collection_model")


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
        patch.object(SearchStore, "migrate_namespace", new=AsyncMock()),
        patch.object(SearchStore, "migrate_description_embedding", new=AsyncMock()),
        patch.object(SearchStore, "migrate_acl", new=AsyncMock()),
        patch.object(SearchStore, "migrate_centroid_sum", new=AsyncMock()),
        patch.object(SearchStore, "migrate_per_collection_model", new=AsyncMock()),
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
        patch.object(SearchStore, "migrate_namespace", new=AsyncMock()),
        patch.object(SearchStore, "migrate_description_embedding", new=AsyncMock()),
        patch.object(SearchStore, "migrate_acl", new=AsyncMock()),
        patch.object(SearchStore, "migrate_centroid_sum", new=AsyncMock()),
        patch.object(SearchStore, "migrate_per_collection_model", new=AsyncMock()),
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
        patch.object(SearchStore, "migrate_namespace", new=AsyncMock()),
        patch.object(SearchStore, "migrate_description_embedding", new=AsyncMock()),
        patch.object(SearchStore, "migrate_acl", new=AsyncMock()),
        patch.object(SearchStore, "migrate_centroid_sum", new=AsyncMock()),
        patch.object(SearchStore, "migrate_per_collection_model", new=AsyncMock()),
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
        patch.object(SearchStore, "migrate_namespace", new=AsyncMock()),
        patch.object(SearchStore, "migrate_description_embedding", new=AsyncMock()),
        patch.object(SearchStore, "migrate_acl", new=AsyncMock()),
        patch.object(SearchStore, "migrate_centroid_sum", new=AsyncMock()),
        patch.object(SearchStore, "migrate_per_collection_model", new=AsyncMock()),
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
        patch.object(SearchStore, "migrate_namespace", new=AsyncMock()),
        patch.object(SearchStore, "migrate_description_embedding", new=AsyncMock()),
        patch.object(SearchStore, "migrate_acl", new=AsyncMock()),
        patch.object(SearchStore, "migrate_centroid_sum", new=AsyncMock()),
        patch.object(SearchStore, "migrate_per_collection_model", new=AsyncMock()),
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
        patch.object(SearchStore, "migrate_namespace", new=AsyncMock()),
        patch.object(SearchStore, "migrate_description_embedding", new=AsyncMock()),
        patch.object(SearchStore, "migrate_acl", new=AsyncMock()),
        patch.object(SearchStore, "migrate_centroid_sum", new=AsyncMock()),
        patch.object(SearchStore, "migrate_per_collection_model", new=AsyncMock()),
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


def test_inbound_id_echoed(tmp_path: Path, job_store: JobStore) -> None:
    from unittest.mock import AsyncMock, patch
    from starlette.testclient import TestClient
    from archon_search.store import SearchStore

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "migrate_namespace", new=AsyncMock()),
        patch.object(SearchStore, "migrate_description_embedding", new=AsyncMock()),
        patch.object(SearchStore, "migrate_acl", new=AsyncMock()),
        patch.object(SearchStore, "migrate_centroid_sum", new=AsyncMock()),
        patch.object(SearchStore, "migrate_per_collection_model", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store)
        with TestClient(app) as client:
            resp = client.get("/health", headers={"X-Request-ID": "myid-abc123"})
    assert resp.headers.get("x-request-id") == "myid-abc123"
