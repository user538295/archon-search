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
    def test_key_roundtrip_generate_then_auth(  # noqa: ANN201
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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


# ---------------------------------------------------------------------------
# Task 2.1 — Multi-key namespace resolution tests
# ---------------------------------------------------------------------------

KEY_A = "a" * 64
KEY_B = "b" * 64
KEY_DEFAULT = VALID_KEY  # same as 'a' * 64 — reuse for clarity


def _make_ns_app(api_key: str, namespaces: dict[str, str] | None = None) -> FastAPI:
    """Minimal app with APIKeyMiddleware configured with given namespaces."""
    app = FastAPI()
    app.add_middleware(APIKeyMiddleware, api_key=api_key, namespaces=namespaces)

    @app.get("/me")
    async def me(request: Request) -> dict:
        return {"namespace": request.state.namespace}

    return app


class TestNamespaceResolution:
    def test_middleware_single_key_no_namespaces_fallback(self) -> None:
        """namespaces={}, valid token matches api_key → DEFAULT_NAMESPACE."""
        from archon_search.constants import DEFAULT_NAMESPACE

        app = _make_ns_app(api_key=KEY_A, namespaces={})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/me", headers={"Authorization": f"Bearer {KEY_A}"})
        assert resp.status_code == 200
        assert resp.json()["namespace"] == DEFAULT_NAMESPACE

    def test_middleware_named_key_resolves_namespace(self) -> None:
        """Key in namespaces dict resolves to its mapped namespace."""
        DISTINCT_DEFAULT = "d" * 64
        app = _make_ns_app(api_key=DISTINCT_DEFAULT, namespaces={KEY_A: "tenantA"})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/me", headers={"Authorization": f"Bearer {KEY_A}"})
        assert resp.status_code == 200
        assert resp.json()["namespace"] == "tenantA"

    def test_middleware_second_key_resolves_different_namespace(self) -> None:
        """Two keys; key B resolves to 'tenantB', not 'tenantA'."""
        app = _make_ns_app(api_key=KEY_DEFAULT, namespaces={KEY_A: "tenantA", KEY_B: "tenantB"})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/me", headers={"Authorization": f"Bearer {KEY_B}"})
        assert resp.status_code == 200
        assert resp.json()["namespace"] == "tenantB"

    def test_middleware_unknown_key_401(self) -> None:
        """Token not in namespaces and not matching api_key → 401."""
        unknown = "c" * 64
        app = _make_ns_app(api_key=KEY_A, namespaces={KEY_B: "tenantB"})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/me", headers={"Authorization": f"Bearer {unknown}"})
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_middleware_invalid_namespace_500(self) -> None:
        """Key maps to invalid namespace ('has space') → 500."""
        app = _make_ns_app(api_key=KEY_DEFAULT, namespaces={KEY_A: "has space"})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/me", headers={"Authorization": f"Bearer {KEY_A}"})
        assert resp.status_code == 500

    def test_middleware_no_early_exit(self) -> None:
        """All namespace keys evaluated even after a match (no early exit)."""
        call_count = 0
        real_compare = secrets.compare_digest

        def counting_compare(a: str, b: str) -> bool:
            nonlocal call_count
            call_count += 1
            return real_compare(a, b)

        namespaces = {KEY_A: "tenantA", KEY_B: "tenantB"}
        app = _make_ns_app(api_key=KEY_DEFAULT, namespaces=namespaces)

        with patch("archon_search.server.middleware_auth.secrets.compare_digest", side_effect=counting_compare):
            client = TestClient(app, raise_server_exceptions=False)
            client.get("/me", headers={"Authorization": f"Bearer {KEY_A}"})

        # Must have been called for both namespace entries (2 calls) + possibly the fallback check
        assert call_count >= 2, f"Expected >= 2 compare_digest calls, got {call_count}"

    def test_middleware_namespace_on_request_state(self) -> None:
        """After successful dispatch, request.state.namespace is accessible in handler."""
        app = _make_ns_app(api_key=KEY_DEFAULT, namespaces={KEY_A: "tenantA"})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/me", headers={"Authorization": f"Bearer {KEY_A}"})
        assert resp.status_code == 200
        assert "namespace" in resp.json()
        assert resp.json()["namespace"] == "tenantA"

    def test_middleware_multiple_keys_same_namespace(self) -> None:
        """Key rotation: two keys mapping to same namespace both resolve correctly."""
        namespaces = {KEY_A: "tenantA", KEY_B: "tenantA"}
        app = _make_ns_app(api_key=KEY_DEFAULT, namespaces=namespaces)
        client = TestClient(app, raise_server_exceptions=False)

        resp_a = client.get("/me", headers={"Authorization": f"Bearer {KEY_A}"})
        resp_b = client.get("/me", headers={"Authorization": f"Bearer {KEY_B}"})

        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        assert resp_a.json()["namespace"] == "tenantA"
        assert resp_b.json()["namespace"] == "tenantA"

    def test_middleware_api_key_also_in_namespaces(self) -> None:
        """If api_key is also in namespaces, the namespace loop wins (not DEFAULT_NAMESPACE)."""
        from archon_search.constants import DEFAULT_NAMESPACE

        # api_key == KEY_A; namespaces maps the same KEY_A to "tenantA"
        app = _make_ns_app(api_key=KEY_A, namespaces={KEY_A: "tenantA"})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/me", headers={"Authorization": f"Bearer {KEY_A}"})
        assert resp.status_code == 200
        # Must resolve to "tenantA" (namespace loop precedence), not DEFAULT_NAMESPACE
        assert resp.json()["namespace"] == "tenantA"
        assert resp.json()["namespace"] != DEFAULT_NAMESPACE

    def test_middleware_api_key_distinct_from_namespaces_fallback(self) -> None:
        """api_key is distinct from all namespace keys; token matching api_key → DEFAULT_NAMESPACE."""
        from archon_search.constants import DEFAULT_NAMESPACE

        DISTINCT_API_KEY = "x" * 64
        app = _make_ns_app(api_key=DISTINCT_API_KEY, namespaces={KEY_A: "tenantA"})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/me", headers={"Authorization": f"Bearer {DISTINCT_API_KEY}"})
        assert resp.status_code == 200
        assert resp.json()["namespace"] == DEFAULT_NAMESPACE
