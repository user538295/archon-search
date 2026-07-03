"""BE-11: scope_filter field on SearchRequest / ExplainRequest — route-layer tests.

Plan: Documentation/Backlog/e2a-ttl-scoping-team-plan.md Task BE-11.

TDD: tests are written first; implementation goes in routes_search.py,
routes_explain.py, and (schema check) schemas.py.

Covers:
- 400 guard: invalid scope_filter patterns returned before pipeline is called
- 422 guard: scope_filter + graph_mode together → incompatible combination
- Forwarding: scope_filter passed through to pipeline.search, pipeline.search_many,
  pipeline.explain
- Schema: DocumentInfoItem has scopes field; GET /documents populates it
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.pipeline import ExplainPipelineResult, SearchPipelineResult
from archon_search.server.app import create_app


# ---------------------------------------------------------------------------
# App / pipeline mock helpers
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path) -> tuple:
    """Create app + TestClient with a stub chunker (avoids tokenizer download)."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    with patch("archon_search.chunker.DocumentChunker.__init__", return_value=None):
        app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {key}"})
    return app, client


def _make_search_pipeline_mock() -> MagicMock:
    """Return a mock SearchPipeline suitable for /search tests."""
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(results=[], acl_filtered=False)
    )
    pipeline.search_many = AsyncMock(
        return_value=SearchPipelineResult(results=[], acl_filtered=False)
    )
    pipeline.get_all_collections_meta = AsyncMock(return_value=[])
    return pipeline


def _make_explain_pipeline_mock() -> MagicMock:
    """Return a mock SearchPipeline suitable for /explain tests."""
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(
            top_results=[], near_misses=[], acl_filtered=False
        )
    )
    pipeline.get_all_collections_meta = AsyncMock(return_value=[])
    return pipeline


# ---------------------------------------------------------------------------
# 1. scope_filter 400 guard — bare wildcard
# ---------------------------------------------------------------------------


def test_search_request_scope_filter_bare_wildcard_400(tmp_path: Path) -> None:
    """POST /search with scope_filter='*' must return 400 with code='invalid_scope_filter'."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_search_pipeline_mock()

    response = client.post(
        "/search",
        json={"collection": "col", "query": "hello", "scope_filter": "*"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_scope_filter"


# ---------------------------------------------------------------------------
# 2. scope_filter 400 guard — leading wildcard
# ---------------------------------------------------------------------------


def test_search_request_scope_filter_leading_wildcard_400(tmp_path: Path) -> None:
    """POST /search with scope_filter='*alice' must return 400."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_search_pipeline_mock()

    response = client.post(
        "/search",
        json={"collection": "col", "query": "hello", "scope_filter": "*alice"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_scope_filter"


# ---------------------------------------------------------------------------
# 3. scope_filter 400 guard — double wildcard
# ---------------------------------------------------------------------------


def test_search_request_scope_filter_double_wildcard_400(tmp_path: Path) -> None:
    """POST /search with scope_filter='user:**' must return 400."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_search_pipeline_mock()

    response = client.post(
        "/search",
        json={"collection": "col", "query": "hello", "scope_filter": "user:**"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_scope_filter"


# ---------------------------------------------------------------------------
# 4. scope_filter 400 guard — mid-string wildcard
# ---------------------------------------------------------------------------


def test_search_request_scope_filter_mid_string_wildcard_400(tmp_path: Path) -> None:
    """POST /search with scope_filter='user:*alice' must return 400."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_search_pipeline_mock()

    response = client.post(
        "/search",
        json={"collection": "col", "query": "hello", "scope_filter": "user:*alice"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_scope_filter"


# ---------------------------------------------------------------------------
# 4b. scope_filter 400 guard on /explain — bare wildcard
# ---------------------------------------------------------------------------


def test_explain_request_scope_filter_bare_wildcard_400(tmp_path: Path) -> None:
    """POST /explain with scope_filter='*' must return 400 — exercises /explain's own guard copy."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_explain_pipeline_mock()

    response = client.post(
        "/explain",
        json={"collection": "col", "query": "hello", "scope_filter": "*"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_scope_filter"


# ---------------------------------------------------------------------------
# 5. scope_filter valid — exact string passes
# ---------------------------------------------------------------------------


def test_search_request_scope_filter_valid_exact(tmp_path: Path) -> None:
    """POST /search with scope_filter='user:alice' (exact) must pass validation and return 200."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_search_pipeline_mock()

    response = client.post(
        "/search",
        json={"collection": "col", "query": "hello", "scope_filter": "user:alice"},
    )

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 6. scope_filter valid — trailing wildcard passes
# ---------------------------------------------------------------------------


def test_search_request_scope_filter_valid_wildcard_suffix(tmp_path: Path) -> None:
    """POST /search with scope_filter='user:*' (trailing wildcard) must pass validation and return 200."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_search_pipeline_mock()

    response = client.post(
        "/search",
        json={"collection": "col", "query": "hello", "scope_filter": "user:*"},
    )

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 7. scope_filter + graph_mode → 422 (/search)
# ---------------------------------------------------------------------------


def test_search_request_scope_filter_with_graph_mode_422(tmp_path: Path) -> None:
    """POST /search with scope_filter AND graph_mode must return 422."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.graph.enabled = True  # enable graph so the graph.enabled guard doesn't fire first
    job_store = JobStore(path=tmp_path / "jobs.json")
    with patch("archon_search.chunker.DocumentChunker.__init__", return_value=None):
        with patch("archon_search.server.app._check_graph_deps"):
            app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(
        app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {key}"}
    )
    app.state.pipeline = _make_search_pipeline_mock()

    response = client.post(
        "/search",
        json={
            "collection": "col",
            "query": "hello",
            "scope_filter": "user:alice",
            "graph_mode": "naive",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "scope_filter" in detail
    assert "graph_mode" in detail


# ---------------------------------------------------------------------------
# 8. scope_filter + graph_mode → 422 (/explain)
# ---------------------------------------------------------------------------


def test_explain_request_scope_filter_with_graph_mode_422(tmp_path: Path) -> None:
    """POST /explain with scope_filter AND graph_mode must return 422."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.graph.enabled = True  # enable graph so the graph.enabled guard doesn't fire first
    job_store = JobStore(path=tmp_path / "jobs.json")
    with patch("archon_search.chunker.DocumentChunker.__init__", return_value=None):
        with patch("archon_search.server.app._check_graph_deps"):
            app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(
        app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {key}"}
    )
    app.state.pipeline = _make_explain_pipeline_mock()

    response = client.post(
        "/explain",
        json={
            "collection": "col",
            "query": "hello",
            "scope_filter": "user:alice",
            "graph_mode": "naive",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "scope_filter" in detail
    assert "graph_mode" in detail


# ---------------------------------------------------------------------------
# 9. scope_filter + graph_mode → 422 (/explain) — e2e with real app
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_explain_scope_filter_with_graph_mode_422_e2e(tmp_path: Path, monkeypatch) -> None:
    """E2E: POST /explain with scope_filter + graph_mode returns 422 (real app, real HTTP)."""
    from tests.integration.conftest import make_real_app

    with patch("archon_search.server.app._check_graph_deps"):
        with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
            response = client.post(
                "/explain",
                json={
                    "collection": "col",
                    "query": "hello",
                    "scope_filter": "user:alice",
                    "graph_mode": "naive",
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "scope_filter" in detail
    assert "graph_mode" in detail


# ---------------------------------------------------------------------------
# 10. DocumentInfoItem has scopes field
# ---------------------------------------------------------------------------


def test_document_info_item_has_scopes_field() -> None:
    """DocumentInfoItem schema must have a scopes: list[str] field (default empty list)."""
    from archon_search.server.schemas import DocumentInfoItem

    item = DocumentInfoItem(
        doc_id="d" * 64,
        source_path="/foo/bar.txt",
        chunk_count=1,
        indexed_at="2026-07-03T00:00:00+00:00",
    )
    assert hasattr(item, "scopes")
    assert isinstance(item.scopes, list)
    assert item.scopes == []


# ---------------------------------------------------------------------------
# 11. GET /documents populates scopes per document — integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_documents_includes_scopes_per_document(tmp_path: Path, monkeypatch) -> None:
    """GET /collections/{name}/documents includes scopes on each document item."""
    import time

    from tests.integration.conftest import make_real_app

    col = "test-scopes-col"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        # Write a file with scoped chunks into tmp_path
        doc_path = tmp_path / "scoped_doc.txt"
        doc_path.write_text("This is a test document about authentication.", encoding="utf-8")

        # Ingest via path with chunk_scopes
        resp = client.post(
            "/ingest",
            json={
                "collection": col,
                "path": str(doc_path),
                "chunk_scopes": ["user:alice"],
            },
            headers=headers,
        )
        assert resp.status_code == 202, f"ingest failed: {resp.status_code} {resp.text}"
        job_id = resp.json()["job_id"]

        # Poll until done
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=headers)
            if r.json()["status"] == "DONE":
                break
            if r.json()["status"] == "FAILED":
                pytest.fail(f"ingest FAILED: {r.json()}")
            time.sleep(0.1)
        else:
            pytest.fail("ingest did not complete in 15s")

        # GET /collections/{col}/documents
        resp = client.get(f"/collections/{col}/documents", headers=headers)
        assert resp.status_code == 200, f"list documents failed: {resp.status_code} {resp.text}"
        items = resp.json()["items"]
        assert len(items) > 0
        # At least one item should have "user:alice" in scopes
        all_scopes = [scope for item in items for scope in item.get("scopes", [])]
        assert "user:alice" in all_scopes, f"Expected 'user:alice' in scopes, got: {all_scopes}"


# ---------------------------------------------------------------------------
# 12. GET /documents — scopes are deduplicated set union — integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_documents_scopes_are_deduplicated_set_union(tmp_path: Path, monkeypatch) -> None:
    """GET /documents returns deduplicated, sorted union of chunk scopes per document."""
    import time

    from tests.integration.conftest import make_real_app

    col = "test-dedup-scopes-col"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        # Write a multi-paragraph doc (parser will produce multiple chunks)
        doc_path = tmp_path / "multi_chunk.txt"
        # Three distinct paragraphs to maximize chunk count
        doc_path.write_text(
            "Alice is an engineer.\n\n"
            "Bob is a manager.\n\n"
            "Carol is a designer.",
            encoding="utf-8",
        )

        # Ingest with multiple scopes including duplicates
        resp = client.post(
            "/ingest",
            json={
                "collection": col,
                "path": str(doc_path),
                "chunk_scopes": ["user:alice", "team:eng", "user:alice"],  # duplicate alice
            },
            headers=headers,
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=headers)
            if r.json()["status"] == "DONE":
                break
            if r.json()["status"] == "FAILED":
                pytest.fail(f"ingest FAILED: {r.json()}")
            time.sleep(0.1)
        else:
            pytest.fail("ingest did not complete in 15s")

        resp = client.get(f"/collections/{col}/documents", headers=headers)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) > 0

        # Every item's scopes must be deduplicated (no duplicates)
        for item in items:
            scopes = item.get("scopes", [])
            assert len(scopes) == len(set(scopes)), (
                f"Duplicate scopes found on doc {item['source_path']}: {scopes}"
            )


# ---------------------------------------------------------------------------
# 13. scope_filter forwarded to pipeline.search (single-collection)
# ---------------------------------------------------------------------------


def test_post_search_scope_filter_forwarded_to_pipeline(tmp_path: Path) -> None:
    """POST /search with scope_filter must forward scope_filter to pipeline.search."""
    app, client = _make_app(tmp_path)
    pipeline = _make_search_pipeline_mock()
    app.state.pipeline = pipeline

    response = client.post(
        "/search",
        json={"collection": "col", "query": "hello", "scope_filter": "user:alice"},
    )

    assert response.status_code == 200
    pipeline.search.assert_called_once()
    call_kwargs = pipeline.search.call_args.kwargs
    assert "scope_filter" in call_kwargs
    assert call_kwargs["scope_filter"] == "user:alice"


# ---------------------------------------------------------------------------
# 14. scope_filter forwarded to pipeline.search_many (multi-collection)
# ---------------------------------------------------------------------------


def test_post_search_scope_filter_forwarded_to_pipeline_multi_collection(tmp_path: Path) -> None:
    """POST /search with collections list must forward scope_filter to pipeline.search_many."""
    app, client = _make_app(tmp_path)
    pipeline = _make_search_pipeline_mock()
    app.state.pipeline = pipeline

    response = client.post(
        "/search",
        json={
            "collections": ["c1", "c2"],
            "query": "hello",
            "scope_filter": "user:alice",
        },
    )

    assert response.status_code == 200
    # search_many must be called (not search)
    pipeline.search_many.assert_called_once()
    pipeline.search.assert_not_called()
    call_kwargs = pipeline.search_many.call_args.kwargs
    assert "scope_filter" in call_kwargs
    assert call_kwargs["scope_filter"] == "user:alice"


# ---------------------------------------------------------------------------
# 15. scope_filter forwarded to pipeline.explain
# ---------------------------------------------------------------------------


def test_post_explain_scope_filter_forwarded_to_pipeline(tmp_path: Path) -> None:
    """POST /explain with scope_filter must forward scope_filter to pipeline.explain."""
    app, client = _make_app(tmp_path)
    pipeline = _make_explain_pipeline_mock()
    app.state.pipeline = pipeline

    response = client.post(
        "/explain",
        json={"collection": "col", "query": "hello", "scope_filter": "user:alice"},
    )

    assert response.status_code == 200
    pipeline.explain.assert_called_once()
    call_kwargs = pipeline.explain.call_args.kwargs
    assert "scope_filter" in call_kwargs
    assert call_kwargs["scope_filter"] == "user:alice"


# ---------------------------------------------------------------------------
# 16. scope_filter forwarded to pipeline.explain (multi-collection)
# ---------------------------------------------------------------------------


def test_post_explain_scope_filter_forwarded_to_pipeline_multi_collection(tmp_path: Path) -> None:
    """POST /explain with collections list must forward scope_filter to pipeline.explain."""
    app, client = _make_app(tmp_path)
    pipeline = _make_explain_pipeline_mock()
    app.state.pipeline = pipeline

    response = client.post(
        "/explain",
        json={
            "collections": ["c1", "c2"],
            "query": "hello",
            "scope_filter": "user:alice",
        },
    )

    assert response.status_code == 200
    pipeline.explain.assert_called_once()
    call_kwargs = pipeline.explain.call_args.kwargs
    assert "scope_filter" in call_kwargs
    assert call_kwargs["scope_filter"] == "user:alice"
