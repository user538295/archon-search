"""Tests for E2a BE-12: MCP search/search_with_context/explain tools gain scope_filter.

Unit tests use the FastMCP stub + AsyncMock pipeline (same pattern as test_e2a_be5_mcp_ingest_ttl.py).
Integration test (5) uses make_real_app with MCP enabled.

Tests:
1. test_mcp_search_scope_filter_forwarded                    — scope_filter passed to pipeline.search
2. test_mcp_search_with_context_scope_filter_forwarded       — scope_filter passed to pipeline.search_with_context
3. test_mcp_explain_scope_filter_forwarded                   — scope_filter passed to pipeline.explain
4. test_document_info_schema_includes_scopes                 — DocumentInfoSchema.from_result maps scopes
5. test_mcp_search_with_scope_filter_returns_filtered_results — integration smoke test
6. test_mcp_search_scope_filter_bare_wildcard_returns_error  — '*' → invalid_scope_filter
7. test_mcp_search_scope_filter_leading_wildcard_returns_error — 'user:*alice' → invalid_scope_filter
8. test_mcp_explain_scope_filter_invalid_syntax_returns_error — '**' → invalid_scope_filter
9. test_mcp_search_scope_filter_with_graph_mode_returns_error — mutual exclusion
10. test_mcp_explain_scope_filter_with_graph_mode_returns_error — mutual exclusion for explain

Scenarios: C1 (MCP search contract), S17, S18 (scope filter validation).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# FastMCP stub for unit tests
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, name: str) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            return func
        return decorator

    def custom_route(self, path: str, methods: list[str] | None = None) -> Any:
        def decorator(func: Any) -> Any:
            return func
        return decorator


class _FakeFastMCP:
    def __new__(cls, name: str, **kwargs: Any) -> _FakeApp:  # type: ignore[misc]
        return _FakeApp(name)


# ---------------------------------------------------------------------------
# Pipeline mock helpers
# ---------------------------------------------------------------------------


def _make_search_result() -> Any:
    """Build a mock SearchPipelineResult with minimal required fields."""
    result = MagicMock()
    result.results = []
    result.acl_filtered = False
    result.excluded_collections = []
    result.rag_fusion_applied = False
    result.rag_fusion_warning = None
    result.rag_fusion_queries_used = 0
    result.graph_expansion_applied = False
    return result


def _make_explain_result() -> Any:
    """Build a mock ExplainPipelineResult with minimal required fields."""
    result = MagicMock()
    result.top_results = []
    result.near_misses = []
    result.acl_filtered = False
    result.graph_mode_applied = None
    result.rag_fusion_applied = False
    result.rag_fusion_warning = None
    result.rag_fusion_queries_used = 0
    result.rag_fusion_attempted = False
    result.rag_fusion_failure_reason = None
    result.rag_fusion_sub_query_results = []
    return result


def _make_search_pipeline() -> Any:
    """Return a mock pipeline suitable for search/search_with_context calls."""
    pipeline = MagicMock()
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])
    # get_collection_meta must return a non-None mock so the namespace gate passes
    _meta = MagicMock()
    _meta.active_embedding_model = ""
    pipeline.get_collection_meta = AsyncMock(return_value=_meta)
    pipeline.search = AsyncMock(return_value=_make_search_result())
    pipeline.search_with_context = AsyncMock(return_value=MagicMock(
        results=[],
        pipeline_result=_make_search_result(),
    ))
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    return pipeline


def _make_mcp_app(pipeline: Any) -> _FakeApp:
    """Build a stub-backed MCP app and return it."""
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_mod
        app = mcp_mod.create_app(pipeline, "col1")
    return app  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Unit test 1 — search passes scope_filter to pipeline.search
# ---------------------------------------------------------------------------


def test_mcp_search_scope_filter_forwarded() -> None:
    """MCP search tool passes scope_filter to pipeline.search."""
    pipeline = _make_search_pipeline()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["search"]

    result = asyncio.run(tool_fn(query="hello", collection="col1", scope_filter="user:alice"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") != "invalid_scope_filter", f"Unexpected validation error: {result!r}"
    # Verify pipeline.search was called with scope_filter='user:alice'
    pipeline.search.assert_called_once()
    call_kwargs = pipeline.search.call_args.kwargs
    assert call_kwargs.get("scope_filter") == "user:alice", (
        f"pipeline.search must receive scope_filter='user:alice'; got: {call_kwargs!r}"
    )


# ---------------------------------------------------------------------------
# Unit test 2 — search_with_context passes scope_filter to pipeline
# ---------------------------------------------------------------------------


def test_mcp_search_with_context_scope_filter_forwarded() -> None:
    """MCP search_with_context tool passes scope_filter to pipeline.search_with_context."""
    pipeline = _make_search_pipeline()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["search_with_context"]

    result = asyncio.run(
        tool_fn(query="hello", collection="col1", scope_filter="user:alice")
    )

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") != "invalid_scope_filter", f"Unexpected validation error: {result!r}"
    # Verify pipeline.search_with_context was called with scope_filter='user:alice'
    pipeline.search_with_context.assert_called_once()
    call_kwargs = pipeline.search_with_context.call_args.kwargs
    assert call_kwargs.get("scope_filter") == "user:alice", (
        f"pipeline.search_with_context must receive scope_filter='user:alice'; got: {call_kwargs!r}"
    )


# ---------------------------------------------------------------------------
# Unit test 3 — explain passes scope_filter to pipeline.explain
# ---------------------------------------------------------------------------


def test_mcp_explain_scope_filter_forwarded() -> None:
    """MCP explain tool passes scope_filter to pipeline.explain."""
    pipeline = _make_search_pipeline()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["explain"]

    result = asyncio.run(tool_fn(query="hello", collection="col1", scope_filter="user:alice"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") != "invalid_scope_filter", f"Unexpected validation error: {result!r}"
    # Verify pipeline.explain was called with scope_filter='user:alice'
    pipeline.explain.assert_called_once()
    call_kwargs = pipeline.explain.call_args.kwargs
    assert call_kwargs.get("scope_filter") == "user:alice", (
        f"pipeline.explain must receive scope_filter='user:alice'; got: {call_kwargs!r}"
    )


# ---------------------------------------------------------------------------
# Unit test 4 — DocumentInfoSchema.from_result maps scopes
# ---------------------------------------------------------------------------


def test_document_info_schema_includes_scopes() -> None:
    """DocumentInfoSchema.from_result populates scopes from DocumentInfo.scopes."""
    from archon_search._types import DocumentInfo
    from archon_search.server.mcp_schemas import DocumentInfoSchema

    doc_with_scopes = DocumentInfo(
        doc_id="abc123",
        source_path="/tmp/test.md",
        chunk_count=3,
        indexed_at="2026-07-03T00:00:00Z",
        scopes=["tag1", "tag2"],
    )
    schema = DocumentInfoSchema.from_result(doc_with_scopes)
    assert schema.scopes == ["tag1", "tag2"], (
        f"DocumentInfoSchema.scopes must equal ['tag1', 'tag2']; got: {schema.scopes!r}"
    )

    doc_no_scopes = DocumentInfo(
        doc_id="def456",
        source_path="/tmp/other.md",
        chunk_count=1,
        indexed_at="2026-07-03T00:00:00Z",
    )
    schema_no_scopes = DocumentInfoSchema.from_result(doc_no_scopes)
    assert schema_no_scopes.scopes == [], (
        f"DocumentInfoSchema.scopes must default to []; got: {schema_no_scopes.scopes!r}"
    )


# ---------------------------------------------------------------------------
# Integration helpers (shared with test 5)
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


def _mcp_initialize(client: Any, token: str) -> str:
    """Send MCP initialize + notifications/initialized; return session_id."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "be12-e2a-test", "version": "1.0"},
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


def _mcp_call_tool(
    client: Any, token: str, session_id: str, tool_name: str, arguments: dict
) -> dict:
    """Call an MCP tool; return the parsed SSE result payload."""
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
    data_lines = [
        line[5:].strip()
        for line in resp.text.split("\n")
        if line.startswith("data:")
    ]
    assert data_lines, f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    return json.loads(data_lines[-1])


def _extract_tool_text(result: dict, tool_name: str) -> Any:
    """Extract and parse the JSON text from an MCP tool response."""
    rpc_result = result.get("result")
    assert rpc_result is not None, (
        f"Tool '{tool_name}' RPC result missing 'result' key: {result!r}"
    )
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content list: {rpc_result!r}"
    text = content[0].get("text", "")
    assert text, f"Tool '{tool_name}' returned empty text: {content!r}"
    return json.loads(text)


# ---------------------------------------------------------------------------
# Integration test 5 — smoke test: MCP search with scope_filter returns no error
# ---------------------------------------------------------------------------


def test_mcp_search_with_scope_filter_returns_filtered_results(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Real MCP app: search with scope_filter='user:alice' returns results without error.

    Smoke test — filter correctness is tested in BE-11. This verifies that
    scope_filter is accepted and threaded through the MCP layer without errors.
    """
    from tests.integration.conftest import make_real_app

    test_file = tmp_path / "doc.md"
    test_file.write_text("# Hello\nThis is a document for scope filter smoke test.", encoding="utf-8")

    collection = "test-col-be12"

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        session_id = _mcp_initialize(client, api_key)

        # Ingest a file first so the collection exists
        ingest_rpc = _mcp_call_tool(
            client, api_key, session_id,
            "ingest_file",
            {"path": str(test_file), "collection": collection},
        )
        ingest_result = _extract_tool_text(ingest_rpc, "ingest_file")
        assert ingest_result.get("status") == "ok", (
            f"Expected status='ok' after ingest; got: {ingest_result!r}"
        )

        # Call search with scope_filter
        search_rpc = _mcp_call_tool(
            client, api_key, session_id,
            "search",
            {"query": "hello", "collection": collection, "scope_filter": "user:alice"},
        )
        search_result = _extract_tool_text(search_rpc, "search")

        assert isinstance(search_result, dict), f"Expected dict; got: {search_result!r}"
        assert "code" not in search_result or search_result.get("code") not in (
            "invalid_scope_filter", "validation_error"
        ), f"Unexpected error from search with scope_filter: {search_result!r}"
        assert "results" in search_result, (
            f"Expected 'results' key in search response; got: {search_result!r}"
        )


# ---------------------------------------------------------------------------
# Unit test 6 — search with bare '*' → invalid_scope_filter
# ---------------------------------------------------------------------------


def test_mcp_search_scope_filter_bare_wildcard_returns_error() -> None:
    """MCP search with scope_filter='*' returns code='invalid_scope_filter'."""
    pipeline = _make_search_pipeline()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["search"]

    result = asyncio.run(tool_fn(query="hello", collection="col1", scope_filter="*"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") == "invalid_scope_filter", (
        f"Expected code='invalid_scope_filter' for scope_filter='*'; got: {result!r}"
    )
    assert "error" in result, f"'error' key missing from result: {result!r}"
    pipeline.search.assert_not_called()


# ---------------------------------------------------------------------------
# Unit test 7 — search with leading '*' → invalid_scope_filter
# ---------------------------------------------------------------------------


def test_mcp_search_scope_filter_leading_wildcard_returns_error() -> None:
    """MCP search with scope_filter='user:*alice' returns code='invalid_scope_filter'."""
    pipeline = _make_search_pipeline()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["search"]

    result = asyncio.run(tool_fn(query="hello", collection="col1", scope_filter="user:*alice"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") == "invalid_scope_filter", (
        f"Expected code='invalid_scope_filter' for scope_filter='user:*alice'; got: {result!r}"
    )
    assert "error" in result, f"'error' key missing from result: {result!r}"
    pipeline.search.assert_not_called()


# ---------------------------------------------------------------------------
# Unit test 8 — explain with '**' → invalid_scope_filter
# ---------------------------------------------------------------------------


def test_mcp_explain_scope_filter_invalid_syntax_returns_error() -> None:
    """MCP explain with scope_filter='**' returns code='invalid_scope_filter'."""
    pipeline = _make_search_pipeline()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["explain"]

    result = asyncio.run(tool_fn(query="hello", collection="col1", scope_filter="**"))

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") == "invalid_scope_filter", (
        f"Expected code='invalid_scope_filter' for scope_filter='**'; got: {result!r}"
    )
    assert "error" in result, f"'error' key missing from result: {result!r}"
    pipeline.explain.assert_not_called()


# ---------------------------------------------------------------------------
# Unit test 9 — search with scope_filter + graph_mode → mutual exclusion error
# ---------------------------------------------------------------------------


def test_mcp_search_scope_filter_with_graph_mode_returns_error() -> None:
    """MCP search with scope_filter='user:alice' + graph_mode='naive' → scope_filter_graph_mode_incompatible."""
    from archon_search.config import GraphConfig, SearchConfig
    pipeline = _make_search_pipeline()

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_mod
        app = mcp_mod.create_app(pipeline, "col1", config=config)

    tool_fn = app.tools["search"]
    result = asyncio.run(
        tool_fn(query="hello", collection="col1", scope_filter="user:alice", graph_mode="naive")
    )

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") == "scope_filter_graph_mode_incompatible", (
        f"Expected code='scope_filter_graph_mode_incompatible'; got: {result!r}"
    )
    assert "error" in result, f"'error' key missing from result: {result!r}"
    pipeline.search.assert_not_called()


# ---------------------------------------------------------------------------
# Unit test 10 — explain with scope_filter + graph_mode → mutual exclusion error
# ---------------------------------------------------------------------------


def test_mcp_explain_scope_filter_with_graph_mode_returns_error() -> None:
    """MCP explain with scope_filter='user:alice' + graph_mode='naive' → scope_filter_graph_mode_incompatible."""
    from archon_search.config import GraphConfig, SearchConfig
    pipeline = _make_search_pipeline()

    config = SearchConfig()
    config.graph = GraphConfig(enabled=True)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_mod
        app = mcp_mod.create_app(pipeline, "col1", config=config)

    tool_fn = app.tools["explain"]
    result = asyncio.run(
        tool_fn(query="hello", collection="col1", scope_filter="user:alice", graph_mode="naive")
    )

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") == "scope_filter_graph_mode_incompatible", (
        f"Expected code='scope_filter_graph_mode_incompatible'; got: {result!r}"
    )
    assert "error" in result, f"'error' key missing from result: {result!r}"
    pipeline.explain.assert_not_called()
