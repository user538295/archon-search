"""Tests for FastAPI app factory (Task 5.2)."""
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
# C1-III-3: server startup collection derivation (FEAT-038 Task 11.2)
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
# Task 3.4: telemetry router registration (FEAT-039c)
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
# migrate_namespace lifespan wiring (Task 3.2 — FEAT-042)
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

    with (
        patch.object(SearchStore, "connect", new=fake_connect),
        patch.object(SearchStore, "migrate_namespace", new=fake_migrate),
        patch.object(SearchStore, "disconnect", new=fake_disconnect),
    ):
        app = create_app(config, job_store)
        from starlette.testclient import TestClient

        with TestClient(app):
            pass

        assert "connect" in call_order
        assert "migrate_namespace" in call_order
        assert call_order.index("connect") < call_order.index("migrate_namespace")


# ---------------------------------------------------------------------------
# Task 2.2 — create_app() passes namespaces to middleware (FEAT-043)
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
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store)
        with TestClient(app) as client:
            # No Authorization header
            response = client.get("/health")

    assert response.status_code == 200
