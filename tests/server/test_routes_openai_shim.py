"""Tests for the OpenAI shim GET /v1/models endpoint and OpenAI401Middleware (BE-3)."""
from __future__ import annotations

import secrets
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("openai_shim")]

# A fixed valid API key injected via env var in tests that use _make_stub_app.
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
    """Build a TestClient-wrapped app with a stub pipeline.

    Uses ``monkeypatch.setenv`` so env vars auto-revert after each test.
    The stub pipeline's ``get_all_collections_meta`` returns one CollectionMeta
    per name in ``collections`` (or an empty list when None).
    """
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

    # Stub out the pipeline so real LanceDB / embedder is never needed.
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
    app.state.pipeline = stub_pipeline

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetModelsDisabled:
    def test_get_models_disabled_returns_404(self, tmp_path, monkeypatch):
        """When openai_shim.enabled=False, GET /v1/models returns plain FastAPI 404."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=False)
        with TestClient(app) as client:
            resp = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {_VALID_KEY}"},
            )
        assert resp.status_code == 404
        body = resp.json()
        assert body == {"detail": "Not Found"}


class TestGetModelsShape:
    def test_get_models_returns_model_list_shape(self, tmp_path, monkeypatch):
        """With two collections, response shape matches ModelList with three entries."""
        app = _make_stub_app(
            tmp_path,
            monkeypatch,
            openai_shim_enabled=True,
            collections=["col-a", "col-b"],
        )
        with TestClient(app) as client:
            resp = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {_VALID_KEY}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        data = body["data"]
        assert len(data) == 3  # col-a, col-b, catch-all

        ids = {entry["id"] for entry in data}
        assert "archon-search" in ids
        assert "archon-search/col-a" in ids
        assert "archon-search/col-b" in ids

        for entry in data:
            assert entry["object"] == "model"
            assert entry["owned_by"] == "archon-search"
            assert isinstance(entry["created"], int)


class TestGetModelsWithCollections:
    def test_get_models_with_collections(self, tmp_path, monkeypatch):
        """make_real_app + ingest into two collections; GET /v1/models returns expected IDs."""
        from tests.integration.conftest import ingest_file_via_path, make_real_app

        doc_a = tmp_path / "doc_a.txt"
        doc_a.write_text("Alpha content for collection A.")
        doc_b = tmp_path / "doc_b.txt"
        doc_b.write_text("Beta content for collection B.")

        with make_real_app(
            tmp_path,
            monkeypatch,
            openai_shim_enabled=True,
        ) as (client, cfg, api_key):
            headers = {"Authorization": f"Bearer {api_key}"}
            ingest_file_via_path(client, "col-a", str(doc_a), api_key=api_key)
            ingest_file_via_path(client, "col-b", str(doc_b), api_key=api_key)

            resp = client.get("/v1/models", headers=headers)

        assert resp.status_code == 200
        ids = {entry["id"] for entry in resp.json()["data"]}
        assert "archon-search" in ids
        assert "archon-search/col-a" in ids
        assert "archon-search/col-b" in ids


class TestGetModelsEmptyNamespace:
    def test_get_models_empty_namespace(self, tmp_path, monkeypatch):
        """No ingest: GET /v1/models returns only the catch-all archon-search entry."""
        from tests.integration.conftest import make_real_app

        with make_real_app(
            tmp_path,
            monkeypatch,
            openai_shim_enabled=True,
        ) as (client, cfg, api_key):
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = client.get("/v1/models", headers=headers)

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["id"] == "archon-search"


class TestMiddleware401Shape:
    def test_middleware_401_shape(self, tmp_path, monkeypatch):
        """OpenAI401Middleware rewrites a bodyless 401 on /v1/* to OpenAI error shape."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            # No Authorization header → APIKeyMiddleware returns 401
            resp = client.get("/v1/models")

        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"
        body = resp.json()
        assert "error" in body
        error = body["error"]
        assert error["message"] == "Incorrect API key."
        assert error["type"] == "authentication_error"

    def test_middleware_401_shape_on_unregistered_v1_path(self, tmp_path, monkeypatch):
        """OpenAI401Middleware fires on unregistered /v1/* paths (e.g. /v1/chat/completions)."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/v1/chat/completions")

        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"
        assert resp.json()["error"]["type"] == "authentication_error"

    def test_middleware_passthrough_non_v1_path(self, tmp_path, monkeypatch):
        """OpenAI401Middleware does NOT rewrite 401s on non-/v1/ paths."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            # /search requires auth; no Authorization header → plain 401 with empty body
            resp = client.post("/search", json={"query": "test", "collection": "col"})

        assert resp.status_code == 401
        # The raw APIKeyMiddleware 401 has no body — OpenAI shape was NOT applied
        assert resp.content == b""
