"""E0c / BE-6 — GET /collections/{name}/documents REST endpoint and MCP list_documents cursor.

Tests:
- GET /collections/{name}/documents?limit=N returns DocumentListResponse
- Cursor-based pagination: first page → second page → last page
- 404 on unknown collection
- 422 on invalid limit (0 or > 200)
- MCP list_documents still works without cursor (backward compat)
- MCP list_documents accepts optional cursor and returns next page

Scenarios covered: S1–S7 (C1)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

# Embedding dimension for stub fastembed backend (384-dim zeros).
_EMBEDDING_DIM = 384


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _doc_id(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


def _make_chunk(doc_id: str, chunk_idx: int, text: str, source_path: str):
    from archon_search._types import ChunkRecord, normalize_iso_utc

    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{chunk_idx:06d}",
        text=text,
        vector=[0.0] * _EMBEDDING_DIM,
        source_path=source_path,
        indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
        acl=None,
    )


async def _inject_n_docs(store, col: str, n: int, *, namespace: str) -> list[str]:
    """Directly inject N documents (one chunk each) into the store.

    Returns sorted list of doc_ids so callers can reason about pagination order.
    """
    from archon_search.collection_meta import CollectionMeta

    await store.ensure_collection(col, _EMBEDDING_DIM)

    doc_ids = []
    for i in range(n):
        path = f"/fake/doc_{i:05d}.txt"
        doc_id = _doc_id(path)
        chunk = _make_chunk(doc_id, 0, f"document {i}", path)
        await store.ingest_chunks(col, [chunk], namespace=namespace)
        doc_ids.append(doc_id)

    await store.rebuild_fts_index(col)

    meta = CollectionMeta(
        name=col,
        active_embedding_model="test-model",
        doc_count=n,
        chunk_count=n,
        namespace=namespace,
    )
    await store.update_collection_meta(meta)
    return sorted(doc_ids)  # sort so order matches store ordering (doc_id ascending)


# ---------------------------------------------------------------------------
# REST endpoint tests
# ---------------------------------------------------------------------------


def test_list_documents_endpoint_first_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest 12 docs, GET ?limit=5 → 5 items, next_cursor set, total=12 (S1)."""
    col = "be6-pagination-first"
    n_docs = 12
    page_size = 5

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store
        asyncio.run(
            _inject_n_docs(store, col, n_docs, namespace="default")
        )

        resp = client.get(
            f"/collections/{col}/documents",
            params={"limit": page_size},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()

        assert "items" in data, f"'items' key missing: {list(data.keys())}"
        assert "next_cursor" in data, f"'next_cursor' key missing: {list(data.keys())}"
        assert "total" in data, f"'total' key missing: {list(data.keys())}"

        assert len(data["items"]) == page_size, (
            f"expected {page_size} items on first page, got {len(data['items'])}"
        )
        assert data["next_cursor"] is not None, (
            "next_cursor should be set when more pages exist"
        )
        assert data["total"] == n_docs, (
            f"expected total={n_docs}, got total={data['total']}"
        )

        # Each item must have the expected fields.
        for item in data["items"]:
            assert "doc_id" in item, f"item missing 'doc_id': {item}"
            assert "source_path" in item, f"item missing 'source_path': {item}"
            assert "chunk_count" in item, f"item missing 'chunk_count': {item}"
            assert "indexed_at" in item, f"item missing 'indexed_at': {item}"


def test_list_documents_endpoint_second_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use next_cursor from first page to get second page of results (S2)."""
    col = "be6-pagination-second"
    n_docs = 12
    page_size = 5

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store
        asyncio.run(
            _inject_n_docs(store, col, n_docs, namespace="default")
        )

        # First page
        resp1 = client.get(
            f"/collections/{col}/documents",
            params={"limit": page_size},
            headers=_auth(api_key),
        )
        assert resp1.status_code == 200
        first_page = resp1.json()
        cursor = first_page["next_cursor"]
        assert cursor is not None, "first page must have a next_cursor for this test"
        first_doc_ids = {item["doc_id"] for item in first_page["items"]}

        # Second page
        resp2 = client.get(
            f"/collections/{col}/documents",
            params={"limit": page_size, "cursor": cursor},
            headers=_auth(api_key),
        )
        assert resp2.status_code == 200, f"second page GET failed: {resp2.status_code} {resp2.text}"
        second_page = resp2.json()

        assert len(second_page["items"]) > 0, "second page must not be empty"
        second_doc_ids = {item["doc_id"] for item in second_page["items"]}
        assert first_doc_ids.isdisjoint(second_doc_ids), (
            "second page must not overlap with first page"
        )
        assert second_page["total"] == n_docs, (
            f"total must be consistent across pages, got {second_page['total']}"
        )


def test_list_documents_endpoint_last_page_no_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Last page returns next_cursor = null (S3)."""
    col = "be6-pagination-last"
    n_docs = 6
    page_size = 5

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store
        asyncio.run(
            _inject_n_docs(store, col, n_docs, namespace="default")
        )

        # First page
        resp1 = client.get(
            f"/collections/{col}/documents",
            params={"limit": page_size},
            headers=_auth(api_key),
        )
        assert resp1.status_code == 200
        cursor = resp1.json()["next_cursor"]
        assert cursor is not None, "first page should have a cursor (n_docs > page_size)"

        # Last page
        resp2 = client.get(
            f"/collections/{col}/documents",
            params={"limit": page_size, "cursor": cursor},
            headers=_auth(api_key),
        )
        assert resp2.status_code == 200
        last_page = resp2.json()
        assert last_page["next_cursor"] is None, (
            f"last page must have next_cursor=null, got {last_page['next_cursor']!r}"
        )
        assert len(last_page["items"]) == n_docs - page_size, (
            f"last page should have {n_docs - page_size} items, got {len(last_page['items'])}"
        )


def test_list_documents_endpoint_deleted_cursor_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cursor pointing to a deleted doc silently resumes from next position; no 4xx (S4)."""
    col = "be6-deleted-cursor"
    n_docs = 8
    page_size = 5

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store
        asyncio.run(
            _inject_n_docs(store, col, n_docs, namespace="default")
        )

        # Get first page to obtain next_cursor
        resp = client.get(
            f"/collections/{col}/documents",
            params={"limit": page_size},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200
        data = resp.json()
        cursor = data["next_cursor"]
        assert cursor is not None, "first page must have a next_cursor for this test"

        # Delete the document whose doc_id == cursor
        pipeline_obj = client.app.state.pipeline
        asyncio.run(
            pipeline_obj.delete_document(cursor, col, namespace="default")
        )

        # GET with the now-stale cursor — must not return 4xx
        resp2 = client.get(
            f"/collections/{col}/documents",
            params={"limit": page_size, "cursor": cursor},
            headers=_auth(api_key),
        )
        assert resp2.status_code == 200, (
            f"Expected 200 with stale cursor, got {resp2.status_code}: {resp2.text}"
        )
        data2 = resp2.json()
        assert "items" in data2
        assert "next_cursor" in data2
        assert "total" in data2
        # The deleted doc must not appear in the results
        assert all(item["doc_id"] != cursor for item in data2["items"]), (
            "Deleted document must not appear in results when cursor is stale"
        )


def test_list_documents_endpoint_collection_not_found_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown collection → 404 (S5)."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        resp = client.get(
            "/collections/nonexistent-collection-xyz/documents",
            headers=_auth(api_key),
        )
        assert resp.status_code == 404, (
            f"expected 404 for unknown collection, got {resp.status_code}: {resp.text}"
        )


def test_list_documents_endpoint_limit_0_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """limit=0 → 422 (S6)."""
    col = "be6-limit-zero"

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        store = client.app.state.search_store
        asyncio.run(
            _inject_n_docs(store, col, 1, namespace="default")
        )

        resp = client.get(
            f"/collections/{col}/documents",
            params={"limit": 0},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, (
            f"expected 422 for limit=0, got {resp.status_code}: {resp.text}"
        )


def test_list_documents_endpoint_limit_201_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """limit=201 → 422 (above maximum of 200) (S6)."""
    col = "be6-limit-201"

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        store = client.app.state.search_store
        asyncio.run(
            _inject_n_docs(store, col, 1, namespace="default")
        )

        resp = client.get(
            f"/collections/{col}/documents",
            params={"limit": 201},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, (
            f"expected 422 for limit=201, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# MCP list_documents backward compat + cursor tests
# ---------------------------------------------------------------------------


def _mcp_headers(token: str, session_id: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return headers


def _mcp_initialize(client, token: str) -> str:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "be6-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize must return mcp-session-id header"
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_mcp_headers(token, session_id),
    )
    return session_id


def _mcp_call_tool(client, token: str, session_id: str, tool_name: str, arguments: dict) -> dict:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, (
        f"MCP tools/call ({tool_name}) failed: {resp.status_code} {resp.text[:300]}"
    )
    data_lines = [line[5:].strip() for line in resp.text.split("\n") if line.startswith("data:")]
    assert data_lines, f"No data: lines in SSE response for {tool_name}: {resp.text[:300]!r}"
    return json.loads(data_lines[-1])


def _get_tool_payload(result: dict, tool_name: str) -> list | dict:
    """Extract the parsed JSON payload from an MCP tool SSE response."""
    rpc_result = result.get("result", {})
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content: {rpc_result!r}"
    text = content[0].get("text", "[]")
    return json.loads(text)


@pytest.mark.xdist_group("mcp")
def test_list_documents_mcp_cursor_backward_compat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP list_documents without cursor param still works (S7 — backward compat)."""
    col = "be6-mcp-no-cursor"

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, cfg, api_key):
        store = client.app.state.search_store
        asyncio.run(
            _inject_n_docs(store, col, 3, namespace="default")
        )

        session_id = _mcp_initialize(client, api_key)
        result = _mcp_call_tool(
            client, api_key, session_id, "list_documents",
            {"collection": col},
        )
        payload = _get_tool_payload(result, "list_documents")
        # Backward compat: should return a list (not an error)
        assert isinstance(payload, list), (
            f"Expected list response from list_documents (no cursor), got: {type(payload)} {payload!r}"
        )
        assert len(payload) == 3, (
            f"Expected 3 documents, got {len(payload)}: {payload!r}"
        )


@pytest.mark.xdist_group("mcp")
def test_list_documents_mcp_cursor_returns_next_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP list_documents with cursor returns the next page of results (S7)."""
    col = "be6-mcp-with-cursor"
    n_docs = 8
    page_size = 5

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, cfg, api_key):
        store = client.app.state.search_store
        asyncio.run(
            _inject_n_docs(store, col, n_docs, namespace="default")
        )

        session_id = _mcp_initialize(client, api_key)

        # First call: no cursor — use REST endpoint to get next_cursor since MCP
        # currently returns a flat list (we'll update MCP to return cursor too)
        # Actually, we'll call the REST endpoint to get the first page cursor,
        # then call MCP with that cursor to verify cursor acceptance.

        # Get first page via REST to get a cursor
        rest_resp = client.get(
            f"/collections/{col}/documents",
            params={"limit": page_size},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert rest_resp.status_code == 200
        rest_data = rest_resp.json()
        cursor = rest_data["next_cursor"]
        assert cursor is not None, "first REST page must have next_cursor"
        first_page_doc_ids = {item["doc_id"] for item in rest_data["items"]}

        # MCP list_documents with cursor — should return remaining docs
        result = _mcp_call_tool(
            client, api_key, session_id, "list_documents",
            {"collection": col, "cursor": cursor, "limit": page_size},
        )
        payload = _get_tool_payload(result, "list_documents")
        assert isinstance(payload, list), (
            f"Expected list from MCP list_documents with cursor, got {type(payload)}: {payload!r}"
        )
        assert len(payload) > 0, "MCP list_documents with cursor must return at least one item"
        second_page_doc_ids = {item["doc_id"] for item in payload}
        assert first_page_doc_ids.isdisjoint(second_page_doc_ids), (
            "MCP second page must not overlap with first page REST results"
        )
