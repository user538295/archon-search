"""Tests for APIKeyMiddleware and startup wire-up (Task 1.2)."""
from __future__ import annotations

import logging
import secrets
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.server.middleware_auth import APIKeyMiddleware

VALID_KEY = "a" * 64


# ---------------------------------------------------------------------------
# Minimal test app with middleware
# ---------------------------------------------------------------------------


@pytest.fixture
def mini_app() -> FastAPI:
    """Minimal FastAPI app with APIKeyMiddleware and one protected route + health."""
    app = FastAPI()
    app.add_middleware(APIKeyMiddleware, api_key=VALID_KEY)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/health")
    async def health_post() -> dict:
        return {"status": "ok"}

    @app.delete("/health")
    async def health_delete() -> dict:
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> dict:
        return {"status": "running"}

    return app


@pytest.fixture
def client(mini_app: FastAPI) -> TestClient:
    return TestClient(mini_app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Middleware unit tests
# ---------------------------------------------------------------------------


class TestAPIKeyMiddlewareAuth:
    def test_valid_key_passes(self, client: TestClient) -> None:
        resp = client.get("/status", headers={"Authorization": f"Bearer {VALID_KEY}"})
        assert resp.status_code == 200

    def test_missing_header_401(self, client: TestClient) -> None:
        resp = client.get("/status")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_wrong_key_401(self, client: TestClient) -> None:
        resp = client.get("/status", headers={"Authorization": f"Bearer {'b' * 64}"})
        assert resp.status_code == 401

    def test_malformed_header_basic(self, client: TestClient) -> None:
        resp = client.get("/status", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401

    def test_empty_bearer_value(self, client: TestClient) -> None:
        resp = client.get("/status", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401

    def test_malformed_header_no_space(self, client: TestClient) -> None:
        resp = client.get("/status", headers={"Authorization": f"Bearer{VALID_KEY}"})
        assert resp.status_code == 401

    def test_get_health_exempt(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_post_health_requires_auth(self, client: TestClient) -> None:
        resp = client.post("/health")
        assert resp.status_code == 401

    def test_get_health_trailing_slash_not_exempt(self, client: TestClient) -> None:
        # /health/ is NOT the same as /health — must not pass auth for free
        resp = client.get("/health/", follow_redirects=False)
        # Must NOT be 200 (unauthenticated access must never succeed on /health/)
        assert resp.status_code != 200, f"GET /health/ returned 200 without auth — exempt path too broad"

    def test_delete_health_requires_auth(self, client: TestClient) -> None:
        resp = client.delete("/health")
        assert resp.status_code == 401

    def test_compare_digest_used(self, mini_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify secrets.compare_digest is called (not ==)."""
        calls: list[tuple[str, str]] = []
        real_digest = secrets.compare_digest

        def tracking_digest(a: str, b: str) -> bool:
            calls.append((a, b))
            return real_digest(a, b)

        with patch("archon_search.server.middleware_auth.secrets.compare_digest", side_effect=tracking_digest):
            test_client = TestClient(mini_app, raise_server_exceptions=False)
            test_client.get("/status", headers={"Authorization": f"Bearer {VALID_KEY}"})

        assert len(calls) >= 1, "secrets.compare_digest was not called"


# ---------------------------------------------------------------------------
# Startup log tests (through create_app)
# ---------------------------------------------------------------------------


def _make_full_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str]:
    """Create a full app client with a known key injected via env var. Returns (client, key)."""
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", VALID_KEY)
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    return TestClient(app, raise_server_exceptions=False), VALID_KEY


class TestStartupLog:
    def test_startup_log_contains_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", VALID_KEY)
        config = SearchConfig()
        config.db_path = str(tmp_path / "search")
        job_store = JobStore(path=tmp_path / "jobs.json")
        with caplog.at_level(logging.INFO, logger="archon-search"):
            create_app(config, job_store)
        # Must log INFO about auth with a source
        auth_logs = [r for r in caplog.records if "auth" in r.getMessage().lower() and r.levelname == "INFO"]
        assert len(auth_logs) >= 1
        assert any("source" in r.getMessage().lower() or "env" in r.getMessage().lower() for r in auth_logs)

    def test_startup_log_key_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", VALID_KEY)
        config = SearchConfig()
        config.db_path = str(tmp_path / "search")
        job_store = JobStore(path=tmp_path / "jobs.json")
        with caplog.at_level(logging.DEBUG, logger="archon-search"):
            create_app(config, job_store)
        # Key value must NEVER appear in any log
        for record in caplog.records:
            assert VALID_KEY not in record.getMessage(), (
                f"Key appeared in log [{record.levelname}]: {record.getMessage()}"
            )


# ---------------------------------------------------------------------------
# Integration: full app auth enforcement
# ---------------------------------------------------------------------------


class TestFullAppAuth:
    def test_protected_route_requires_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, key = _make_full_client(tmp_path, monkeypatch)
        resp = client.get("/status")
        assert resp.status_code == 401

    def test_protected_route_with_valid_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, key = _make_full_client(tmp_path, monkeypatch)
        resp = client.get("/status", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200

    def test_health_exempt_in_full_app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = _make_full_client(tmp_path, monkeypatch)
        resp = client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.skip(reason="Requires SearchApiKeyAuth from Task 4.1 (not yet implemented)")
    def test_key_roundtrip_generate_then_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Integration: generate key via key_manager, use SearchApiKeyAuth to request a protected endpoint."""
        import archon_search.key_manager as km
        from archon_search.ai.search_client import SearchApiKeyAuth  # noqa: F401 — Task 4.1

        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        monkeypatch.setattr(km, "KEY_FILE", tmp_path / ".search.env")
        key, _ = km.load_or_generate_key()

        auth = SearchApiKeyAuth()
        assert auth._KEY_FILE == km.KEY_FILE

        config = SearchConfig()
        config.db_path = str(tmp_path / "search")
        job_store = JobStore(path=tmp_path / "jobs.json")
        app = create_app(config, job_store)

        import httpx

        with TestClient(app) as tc:
            transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
            # Make a request via SearchApiKeyAuth — would need async client
            resp = tc.get("/status", headers={"Authorization": f"Bearer {key}"})
            assert resp.status_code == 200
