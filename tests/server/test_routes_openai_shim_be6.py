"""Tests for BE-6 — Auth and validation error shapes for the OpenAI shim.

Covers:
- OpenAI401Middleware correctly rewrites bodyless 401s on /v1/* paths (S11)
- RequestValidationError handler rewrites 422s on /v1/* to OpenAI error shape (S12)
- Path-prefix filter leaves non-/v1/ 422s in FastAPI's native shape
- Handler absent when openai_shim.enabled=False
"""
from __future__ import annotations

import secrets
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("openai_shim")]

_VALID_KEY = secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_app(
    tmp_path,
    monkeypatch,
    *,
    openai_shim_enabled: bool,
    collections: list[str] | None = None,
):
    """Build a TestClient-wrapped app with a stub pipeline."""
    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", _VALID_KEY)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.openai_shim.enabled = openai_shim_enabled
    cfg.mcp.enabled = False

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    app = create_app(cfg, job_store, scheduler=scheduler)

    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    metas: list[CollectionMeta] = []
    for name in (collections or []):
        m = CollectionMeta(
            name=name,
            namespace=DEFAULT_NAMESPACE,
            description=None,
            chunk_count=0,
        )
        metas.append(m)

    stub_pipeline = MagicMock()
    stub_pipeline.get_all_collections_meta = AsyncMock(return_value=metas)
    stub_pipeline.get_collection_meta = AsyncMock(return_value=None)
    app.state.pipeline = stub_pipeline

    return app


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_VALID_KEY}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAuthFailureReturnsOpenAI401:
    def test_auth_failure_returns_openai_401(self, tmp_path, monkeypatch):
        """Invalid token on /v1/* returns OpenAI 401 shape (S11)."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "archon-search",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                headers={"Authorization": "Bearer invalid-token"},
            )

        assert resp.status_code == 401
        body = resp.json()
        assert "error" in body
        assert body["error"]["message"] == "Incorrect API key."
        assert body["error"]["type"] == "authentication_error"
        assert resp.headers.get("www-authenticate") == "Bearer"


class TestEmpty422OpenAIShape:
    def test_empty_messages_422_openai_shape(self, tmp_path, monkeypatch):
        """messages=[] → 422 with OpenAI error shape (S12)."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "archon-search", "messages": []},
                headers=_auth_headers(),
            )

        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        assert body["error"]["message"] == "messages must contain at least one user message"
        assert body["error"]["type"] == "invalid_request_error"


class TestHandlerNoUserMessage:
    def test_handler_422_no_user_message(self, tmp_path, monkeypatch):
        """messages with only system role → 422 with OpenAI error shape (S12)."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "archon-search",
                    "messages": [{"role": "system", "content": "You are helpful."}],
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["message"] == "messages must contain at least one user message"


class TestPydantic422MissingModelField:
    def test_pydantic_422_missing_model_field(self, tmp_path, monkeypatch):
        """Missing required 'model' field → 422 with OpenAI error shape (not FastAPI detail list)."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                # Missing required "model" field → Pydantic RequestValidationError
                json={"messages": [{"role": "user", "content": "hello"}]},
                headers=_auth_headers(),
            )

        assert resp.status_code == 422
        body = resp.json()
        # Must be OpenAI shape, NOT FastAPI's {"detail": [...]}
        assert "error" in body
        assert "detail" not in body
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["message"] == "Invalid value for field 'model'."


class TestNestedFieldValidationError:
    def test_pydantic_422_nested_field(self, tmp_path, monkeypatch):
        """Nested Pydantic error (e.g. messages[0].role invalid) produces OpenAI shape
        with a dotted field path like 'messages.0.role'.  This exercises the loc-join
        path for nested validation errors, the most common real-client 422."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                # messages[0].content must be a str; sending null triggers a nested Pydantic error
                json={"model": "archon-search", "messages": [{"role": "user", "content": None}]},
                headers=_auth_headers(),
            )

        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        assert "detail" not in body
        assert body["error"]["type"] == "invalid_request_error"
        # Field path derived from the nested loc — should reference the nested field
        assert body["error"]["message"] == "Invalid value for field 'messages.0.content'."


class TestGetModels401Shape:
    def test_get_models_auth_failure_returns_openai_401(self, tmp_path, monkeypatch):
        """GET /v1/models with invalid token → OpenAI 401 shape (middleware covers all /v1/* paths)."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(
                "/v1/models",
                headers={"Authorization": "Bearer invalid-token"},
            )

        assert resp.status_code == 401
        body = resp.json()
        assert "error" in body
        assert body["error"]["message"] == "Incorrect API key."
        assert body["error"]["type"] == "authentication_error"
        assert resp.headers.get("www-authenticate") == "Bearer"


class TestExistingRoute422ShapeUnchanged:
    def test_existing_route_422_shape_unchanged(self, tmp_path, monkeypatch):
        """POST /search with missing required field still returns FastAPI native 422 shape
        even when openai_shim.enabled=True — the path-prefix filter must protect non-/v1/ routes."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            # /search requires "query" and "collection" — sending neither triggers FastAPI 422
            resp = client.post(
                "/search",
                json={},  # missing "query" and "collection"
                headers=_auth_headers(),
            )

        assert resp.status_code == 422
        body = resp.json()
        # Must be FastAPI native shape {"detail": [...]}, NOT OpenAI shape
        assert "detail" in body
        assert "error" not in body
        assert isinstance(body["detail"], list)


class TestValidationErrorHandlerAbsentWhenDisabled:
    def test_validation_error_handler_absent_when_disabled(self, tmp_path, monkeypatch):
        """When openai_shim.enabled=False, /v1/chat/completions is not registered → 404."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=False)
        with TestClient(app, raise_server_exceptions=False) as client:
            # Missing "model" field; if shim were enabled this would be a 422
            resp = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hello"}]},
                headers=_auth_headers(),
            )

        # Route not registered → 404, not 422 — proves the guard worked
        assert resp.status_code == 404
        body = resp.json()
        assert body == {"detail": "Not Found"}
