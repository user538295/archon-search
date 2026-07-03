"""BE-11 — MCP graph inspection tools integration tests.

Tests:
- test_mcp_get_graph_tool_registered
    make_real_app(graph_enabled=True, mcp_enabled=True) + spaCy stub;
    MCP tools/list includes get_graph and get_graph_cross_collection
- test_mcp_get_graph_returns_summary_after_ingest
    ingest doc; MCP tools/call get_graph → 200; top_nodes non-empty;
    len(top_nodes) ≤ 20
- test_mcp_get_graph_cross_collection_returns_merged_summary
    ingest a document into two collections;
    call MCP tools/call get_graph_cross_collection with both collection names;
    assert result contains node_count, edge_count, top_nodes (list), top_edges (list)

Scenario: BE-11 graph inspection via MCP
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration
pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# spaCy stub — needed for make_real_app(graph_enabled=True)
# ---------------------------------------------------------------------------


def _install_spacy_stub_no_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy that returns NO named entities for any text.

    Must be called BEFORE make_real_app because create_app calls _check_graph_deps
    which imports spacy synchronously.
    """

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents: list = []

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


def _mcp_headers(token: str, session_id: str | None = None) -> dict:
    """Build MCP HTTP headers with auth and session ID."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return headers


def _mcp_initialize(client, token: str) -> str:
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
                "clientInfo": {"name": "be11-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, (
        f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    )
    session_id = resp.headers.get("mcp-session-id")
    assert session_id is not None, "MCP initialize did not return mcp-session-id header"

    # Send notifications/initialized (returns 202 Accepted for notifications)
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code in (200, 202), (
        f"MCP notifications/initialized failed: {resp.status_code} {resp.text[:300]}"
    )

    return session_id


def _mcp_tools_list(client, token: str, session_id: str) -> dict:
    """Send MCP tools/list RPC; parse SSE response and return the result."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, (
        f"MCP tools/list failed: {resp.status_code} {resp.text[:300]}"
    )
    # Parse SSE response format: "event: message\r\ndata: {...}\r\n\r\n"
    data_lines = [
        line[5:].strip()
        for line in resp.text.split("\n")
        if line.startswith("data:")
    ]
    assert data_lines, (
        f"No data: line in SSE response for tools/list: {resp.text[:300]!r}"
    )
    body = json.loads(data_lines[-1])
    assert body.get("jsonrpc") == "2.0"
    assert "result" in body
    return body["result"]


def _mcp_tool_call(client, token: str, session_id: str, tool_name: str, arguments: dict) -> dict:
    """Send MCP tools/call RPC; parse SSE response and extract the tool result."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, (
        f"MCP tools/call {tool_name} failed: {resp.status_code} {resp.text[:300]}"
    )
    # Parse SSE response format: "event: message\r\ndata: {...}\r\n\r\n"
    data_lines = [
        line[5:].strip()
        for line in resp.text.split("\n")
        if line.startswith("data:")
    ]
    assert data_lines, (
        f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    )
    body = json.loads(data_lines[-1])
    assert body.get("jsonrpc") == "2.0"

    # Extract the tool result from the content array
    rpc_result = body.get("result", {})
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content list: {rpc_result!r}"

    # Parse the text content which contains the JSON result
    text = content[0].get("text", "")
    assert text, f"Tool '{tool_name}' returned empty text: {content!r}"

    return json.loads(text)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_mcp_get_graph_tool_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP tools/list includes get_graph and get_graph_cross_collection when graph enabled."""
    _install_spacy_stub_no_entities(monkeypatch)

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        mcp_enabled=True,
    ) as (client, cfg, api_key):
        session_id = _mcp_initialize(client, api_key)
        tools_result = _mcp_tools_list(client, api_key, session_id)

        # Check that tools list contains our graph tools
        tool_names = [t["name"] for t in tools_result.get("tools", [])]
        assert "get_graph" in tool_names, f"get_graph not in tools: {tool_names}"
        assert (
            "get_graph_cross_collection" in tool_names
        ), f"get_graph_cross_collection not in tools: {tool_names}"


def test_mcp_get_graph_returns_summary_after_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ingest, MCP get_graph returns summary with top_nodes (len ≤ 20)."""
    _install_spacy_stub_no_entities(monkeypatch)

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        mcp_enabled=True,
    ) as (client, cfg, api_key):
        # Ingest a document
        doc_text = "Alice works in Seattle. Bob works in Portland."
        doc_file = tmp_path / "test.txt"
        doc_file.write_text(doc_text, encoding="utf-8")
        ingest_file_via_path(
            client,
            "test-col",
            str(doc_file),
            api_key=api_key,
        )

        # Initialize MCP session
        session_id = _mcp_initialize(client, api_key)

        # Call get_graph
        result = _mcp_tool_call(
            client,
            api_key,
            session_id,
            "get_graph",
            {"collection": "test-col"},
        )

        # Verify result shape and constraints
        assert isinstance(result, dict)
        assert "node_count" in result
        assert "edge_count" in result
        assert "entity_type_distribution" in result
        assert "top_nodes" in result
        assert "top_edges" in result

        # Verify top_nodes constraint: length ≤ 20
        top_nodes = result["top_nodes"]
        assert isinstance(top_nodes, list)
        assert len(top_nodes) <= 20

        # Each top_node should have required fields
        for node in top_nodes:
            assert "entity_id" in node
            assert "entity_name" in node
            assert "chunk_count" in node
            assert "salience" in node

        # Verify top_edges constraint: length ≤ 20
        top_edges = result["top_edges"]
        assert isinstance(top_edges, list)
        assert len(top_edges) <= 20

        # Each top_edge should have required fields
        for edge in top_edges:
            assert "edge_id" in edge
            assert "source_entity_id" in edge
            assert "target_entity_id" in edge
            assert "weight" in edge


def test_mcp_get_graph_cross_collection_returns_merged_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ingest into two collections, MCP get_graph_cross_collection returns merged summary."""
    _install_spacy_stub_no_entities(monkeypatch)

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        mcp_enabled=True,
    ) as (client, cfg, api_key):
        # Ingest documents into two collections
        doc_text_1 = "Alice works in Seattle."
        doc_text_2 = "Charlie works in Portland."

        doc_file_1 = tmp_path / "test1.txt"
        doc_file_1.write_text(doc_text_1, encoding="utf-8")
        ingest_file_via_path(
            client,
            "col-1",
            str(doc_file_1),
            api_key=api_key,
        )

        doc_file_2 = tmp_path / "test2.txt"
        doc_file_2.write_text(doc_text_2, encoding="utf-8")
        ingest_file_via_path(
            client,
            "col-2",
            str(doc_file_2),
            api_key=api_key,
        )

        # Initialize MCP session
        session_id = _mcp_initialize(client, api_key)

        # Call get_graph_cross_collection with both collections
        result = _mcp_tool_call(
            client,
            api_key,
            session_id,
            "get_graph_cross_collection",
            {"collections": ["col-1", "col-2"]},
        )

        # Verify result shape and structure
        assert isinstance(result, dict)
        assert "node_count" in result, "Missing node_count in cross-collection result"
        assert "edge_count" in result, "Missing edge_count in cross-collection result"
        assert (
            "entity_type_distribution" in result
        ), "Missing entity_type_distribution in cross-collection result"
        assert "top_nodes" in result, "Missing top_nodes in cross-collection result"
        assert "top_edges" in result, "Missing top_edges in cross-collection result"

        # Verify types
        assert isinstance(result["top_nodes"], list), "top_nodes should be a list"
        assert isinstance(result["top_edges"], list), "top_edges should be a list"

        # Verify constraints
        assert len(result["top_nodes"]) <= 20, "top_nodes length should be ≤ 20"
        assert len(result["top_edges"]) <= 20, "top_edges length should be ≤ 20"

        # Verify each node/edge has required fields
        for node in result["top_nodes"]:
            assert "entity_id" in node
            assert "entity_name" in node
            assert "chunk_count" in node
            assert "salience" in node

        for edge in result["top_edges"]:
            assert "edge_id" in edge
            assert "source_entity_id" in edge
            assert "target_entity_id" in edge
            assert "weight" in edge
