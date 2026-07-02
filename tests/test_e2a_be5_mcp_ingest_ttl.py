"""Tests for E2a BE-5: MCP ingest_file and ingest_directory gain chunk_ttl_seconds + chunk_scopes.

Unit tests (1–4) use a FastMCP stub + AsyncMock pipeline.
Integration test (5) uses make_real_app with MCP enabled.

Tests:
- test_mcp_ingest_file_accepts_chunk_ttl_seconds  — param passed to pipeline
- test_mcp_ingest_directory_accepts_chunk_scopes   — param passed to pipeline
- test_mcp_ingest_file_invalid_ttl_zero_returns_error — 0 → code='invalid_parameter'
- test_mcp_ingest_file_invalid_scopes_overlong_returns_error — 256-char scope → code='invalid_parameter'
- test_mcp_ingest_file_with_ttl_stores_expires_at — integration: MCP call → expires_at in store

Scenarios: C1 (MCP ingest contract), S15, S16 (validation).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._types import IngestResult

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]

# ---------------------------------------------------------------------------
# Named constants (no magic numbers)
# ---------------------------------------------------------------------------

INT32_MAX: int = 2**31 - 1
MAX_SCOPE_ITEM_LEN: int = 255
MAX_SCOPE_LIST_ITEMS: int = 100
OVERLONG_SCOPE: str = "x" * (MAX_SCOPE_ITEM_LEN + 1)  # 256 chars

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
# Helpers for unit tests
# ---------------------------------------------------------------------------


def _make_pipeline_with_ingest_result() -> Any:
    """Return a mock pipeline whose ingest_file/ingest_directory return IngestResult."""
    result = IngestResult(doc_id="doc1", chunks_created=3, status="ok")
    pipeline = MagicMock()
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])
    pipeline.get_collection_meta = AsyncMock(return_value=None)
    pipeline.ingest_file = AsyncMock(return_value=result)
    pipeline.ingest_directory = AsyncMock(return_value=[result])
    return pipeline


def _make_mcp_app(pipeline: Any) -> _FakeApp:
    """Build a stub-backed MCP app and return it.

    Uses patch() to replace mcp.FastMCP with the stub BEFORE calling create_app.
    No importlib.reload — that would re-execute `from fastmcp import FastMCP`
    and overwrite the patch.
    """
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_mod
        app = mcp_mod.create_app(pipeline, "col1")
    return app  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Unit test 1 — ingest_file passes chunk_ttl_seconds to pipeline
# ---------------------------------------------------------------------------


def test_mcp_ingest_file_accepts_chunk_ttl_seconds() -> None:
    """MCP ingest_file tool accepts chunk_ttl_seconds and passes it to pipeline.ingest_file."""
    pipeline = _make_pipeline_with_ingest_result()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["ingest_file"]

    result = asyncio.run(tool_fn(path="/tmp/test.md", collection="col1", chunk_ttl_seconds=3600))

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") != "invalid_parameter", f"Unexpected validation error: {result!r}"
    assert result.get("status") == "ok", f"Expected status='ok': {result!r}"
    # Verify pipeline.ingest_file was called with chunk_ttl_seconds=3600
    pipeline.ingest_file.assert_called_once()
    call_kwargs = pipeline.ingest_file.call_args.kwargs
    assert call_kwargs.get("chunk_ttl_seconds") == 3600, (
        f"pipeline.ingest_file must receive chunk_ttl_seconds=3600; got: {call_kwargs!r}"
    )


# ---------------------------------------------------------------------------
# Unit test 2 — ingest_directory passes chunk_scopes to pipeline
# ---------------------------------------------------------------------------


def test_mcp_ingest_directory_accepts_chunk_scopes() -> None:
    """MCP ingest_directory tool accepts chunk_scopes and passes it to pipeline.ingest_directory."""
    pipeline = _make_pipeline_with_ingest_result()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["ingest_directory"]

    scopes = ["user:alice", "project:xyz"]
    result = asyncio.run(
        tool_fn(path="/tmp/", collection="col1", chunk_scopes=scopes)
    )

    assert isinstance(result, list), f"Expected list, got: {type(result)!r}: {result!r}"
    if result and isinstance(result[0], dict):
        assert result[0].get("code") != "invalid_parameter", (
            f"Unexpected validation error: {result!r}"
        )
    # Verify pipeline.ingest_directory was called with chunk_scopes
    pipeline.ingest_directory.assert_called_once()
    call_kwargs = pipeline.ingest_directory.call_args.kwargs
    assert call_kwargs.get("chunk_scopes") == scopes, (
        f"pipeline.ingest_directory must receive chunk_scopes={scopes!r}; got: {call_kwargs!r}"
    )


# ---------------------------------------------------------------------------
# Unit test 3 — ingest_file with chunk_ttl_seconds=0 → invalid_parameter
# ---------------------------------------------------------------------------


def test_mcp_ingest_file_invalid_ttl_zero_returns_error() -> None:
    """MCP ingest_file with chunk_ttl_seconds=0 returns code='invalid_parameter'."""
    pipeline = _make_pipeline_with_ingest_result()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["ingest_file"]

    result = asyncio.run(tool_fn(path="/tmp/test.md", collection="col1", chunk_ttl_seconds=0))

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") == "invalid_parameter", (
        f"Expected code='invalid_parameter' for chunk_ttl_seconds=0; got: {result!r}"
    )
    assert "error" in result, f"'error' key missing from result: {result!r}"
    # Pipeline must NOT be called — validation fires before pipeline invocation
    pipeline.ingest_file.assert_not_called()


# ---------------------------------------------------------------------------
# Unit test 4 — ingest_file with 256-char scope → invalid_parameter
# ---------------------------------------------------------------------------


def test_mcp_ingest_file_invalid_scopes_overlong_returns_error() -> None:
    """MCP ingest_file with a 256-char scope string returns code='invalid_parameter'."""
    pipeline = _make_pipeline_with_ingest_result()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["ingest_file"]

    result = asyncio.run(
        tool_fn(path="/tmp/test.md", collection="col1", chunk_scopes=[OVERLONG_SCOPE])
    )

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") == "invalid_parameter", (
        f"Expected code='invalid_parameter' for 256-char scope; got: {result!r}"
    )
    assert "error" in result, f"'error' key missing from result: {result!r}"
    # Pipeline must NOT be called — validation fires before pipeline invocation
    pipeline.ingest_file.assert_not_called()


# ---------------------------------------------------------------------------
# Unit test 5 — ingest_file with negative TTL → invalid_parameter
# ---------------------------------------------------------------------------


def test_mcp_ingest_file_invalid_ttl_negative_returns_error() -> None:
    """MCP ingest_file with chunk_ttl_seconds=-1 returns code='invalid_parameter'."""
    pipeline = _make_pipeline_with_ingest_result()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["ingest_file"]

    result = asyncio.run(tool_fn(path="/tmp/test.md", collection="col1", chunk_ttl_seconds=-1))

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") == "invalid_parameter", (
        f"Expected code='invalid_parameter' for chunk_ttl_seconds=-1; got: {result!r}"
    )
    pipeline.ingest_file.assert_not_called()


# ---------------------------------------------------------------------------
# Unit test 6 — ingest_file with scope list > 100 → invalid_parameter
# ---------------------------------------------------------------------------


def test_mcp_ingest_file_invalid_scopes_too_many_returns_error() -> None:
    """MCP ingest_file with 101 scopes returns code='invalid_parameter'."""
    pipeline = _make_pipeline_with_ingest_result()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["ingest_file"]

    too_many_scopes = [f"scope:{i}" for i in range(MAX_SCOPE_LIST_ITEMS + 1)]  # 101 items
    result = asyncio.run(
        tool_fn(path="/tmp/test.md", collection="col1", chunk_scopes=too_many_scopes)
    )

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") == "invalid_parameter", (
        f"Expected code='invalid_parameter' for 101 scopes; got: {result!r}"
    )
    pipeline.ingest_file.assert_not_called()


# ---------------------------------------------------------------------------
# Unit test 7 — ingest_directory with invalid TTL → invalid_parameter
# ---------------------------------------------------------------------------


def test_mcp_ingest_directory_invalid_ttl_zero_returns_error() -> None:
    """MCP ingest_directory with chunk_ttl_seconds=0 returns code='invalid_parameter'."""
    pipeline = _make_pipeline_with_ingest_result()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["ingest_directory"]

    result = asyncio.run(tool_fn(path="/tmp/", collection="col1", chunk_ttl_seconds=0))

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") == "invalid_parameter", (
        f"Expected code='invalid_parameter' for ingest_directory chunk_ttl_seconds=0; got: {result!r}"
    )
    pipeline.ingest_directory.assert_not_called()


# ---------------------------------------------------------------------------
# Unit test 8 — ingest_directory with overlong scope → invalid_parameter
# ---------------------------------------------------------------------------


def test_mcp_ingest_directory_invalid_scopes_overlong_returns_error() -> None:
    """MCP ingest_directory with a 256-char scope string returns code='invalid_parameter'."""
    pipeline = _make_pipeline_with_ingest_result()
    app = _make_mcp_app(pipeline)
    tool_fn = app.tools["ingest_directory"]

    result = asyncio.run(
        tool_fn(path="/tmp/", collection="col1", chunk_scopes=[OVERLONG_SCOPE])
    )

    assert isinstance(result, dict), f"Expected dict, got: {type(result)!r}: {result!r}"
    assert result.get("code") == "invalid_parameter", (
        f"Expected code='invalid_parameter' for ingest_directory 256-char scope; got: {result!r}"
    )
    pipeline.ingest_directory.assert_not_called()


# ---------------------------------------------------------------------------
# Integration test 9 — real MCP: ingest_file with chunk_ttl_seconds stores expires_at
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
                "clientInfo": {"name": "be5-e2a-test", "version": "1.0"},
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


def test_mcp_ingest_file_with_ttl_stores_expires_at(tmp_path: Any, monkeypatch: Any) -> None:
    """Real MCP app: ingest_file with chunk_ttl_seconds=3600 → GET /expiring returns the chunk.

    Verifies that chunk_ttl_seconds is threaded through the MCP tool to the pipeline
    and the store records expires_at correctly.
    """
    from tests.integration.conftest import make_real_app

    # Write a small text file to ingest
    test_file = tmp_path / "note.md"
    test_file.write_text("# Hello\nThis is a test document for TTL integration.", encoding="utf-8")

    collection = "test-col-e2a-be5"

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        # Initialize MCP session
        session_id = _mcp_initialize(client, api_key)

        # Ingest via MCP with a 2-hour TTL.
        # Fresh collections (created by the current codebase) already have expires_at
        # in their schema — no migration step is required for new stores.
        ingest_rpc = _mcp_call_tool(
            client, api_key, session_id,
            "ingest_file",
            {"path": str(test_file), "collection": collection, "chunk_ttl_seconds": 7200},
        )
        ingest_result = _extract_tool_text(ingest_rpc, "ingest_file")

        assert isinstance(ingest_result, dict), f"Expected dict: {ingest_result!r}"
        assert ingest_result.get("status") == "ok", (
            f"Expected status='ok' after ingest with TTL; got: {ingest_result!r}"
        )
        assert ingest_result.get("code") != "invalid_parameter", (
            f"Unexpected invalid_parameter error: {ingest_result!r}"
        )

        # Verify the chunk appears in the /expiring endpoint (within_hours=3 > 2h TTL).
        # The /expiring route only requires the collection to exist in the namespace
        # meta table — no config-path registration needed.
        expiring_resp = client.get(
            f"/collections/{collection}/expiring?within_hours=3",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert expiring_resp.status_code == 200, (
            f"GET /expiring failed: {expiring_resp.status_code} {expiring_resp.text[:300]}"
        )
        expiring_data = expiring_resp.json()
        items = expiring_data.get("items", [])
        assert len(items) > 0, (
            f"Expected at least one chunk in /expiring after ingest with chunk_ttl_seconds=7200; "
            f"got empty list. expiring_data={expiring_data!r}"
        )
        # Verify expires_at is present on the item
        first_item = items[0]
        assert first_item.get("expires_at") is not None, (
            f"expires_at must not be null on expiring item; got: {first_item!r}"
        )
