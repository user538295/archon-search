"""E1a / T-3 — e2e: graph error paths + MCP graph_mode roundtrip.

Scenarios covered:
- S6: graph.enabled=False + POST /search with graph_mode="naive" → HTTP 422
- S5: graph.enabled=True but zero graph nodes + graph_mode="naive" → 200,
      graph_expansion_applied=False (no-op expansion)
- S8: MCP search with graph_mode="naive" (graph enabled) → well-formed response
      with graph_expansion_applied field; no envelope-level exception
- (FE-3 guard): MCP search_with_context with graph_mode="naive" → result dict with
      code="graph_mode_not_supported"; not a Python exception

Completes S5, S6, S8.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# Helpers: spaCy stubs
# ---------------------------------------------------------------------------


def _install_spacy_stub_with_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy that returns two named entities for any text.

    Must be called BEFORE make_real_app (create_app imports spacy synchronously
    via _check_graph_deps).
    """

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents = [_FakeEnt("Alice", "PERSON"), _FakeEnt("Google", "ORG")]

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


def _install_spacy_stub_no_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy that returns NO named entities for any text.

    Graph tables will be created but remain empty after ingest, producing
    a zero-node graph (no-op expansion at query time).
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


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# MCP helpers (same pattern as test_mcp_roundtrip_t2.py)
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
                "clientInfo": {"name": "t3-e2e-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, (
        f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    )
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize must return mcp-session-id header"
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_mcp_headers(token, session_id),
    )
    return session_id


def _mcp_call_tool(client, token: str, session_id: str, tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool and return the parsed SSE result payload."""
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
    assert data_lines, (
        f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    )
    return json.loads(data_lines[-1])


def _extract_tool_text(result: dict, tool_name: str):
    """Extract and parse the JSON text from an MCP tool response.

    Does NOT assert isError=False — some tests exercise error responses where
    the tool returns a well-formed error dict, not an envelope-level exception.
    """
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
# T-3 tests
# ---------------------------------------------------------------------------


def test_e2e_graph_mode_422_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph.enabled=False + POST /search with graph_mode="naive" → HTTP 422.

    Covers S6: graph disabled → 422 with actionable error message.
    The route handler body rejects graph_mode when graph.enabled=False
    (consistent with Q10 resolution: handler-body check for operational clarity).
    """
    col = "e1a-t3-graph-disabled"

    with make_real_app(tmp_path, monkeypatch, graph_enabled=False) as (client, cfg, api_key):
        assert cfg.graph.enabled is False, (
            f"Expected graph.enabled=False, got {cfg.graph.enabled}"
        )

        resp = client.post(
            "/search",
            json={"collection": col, "query": "AuthService", "graph_mode": "naive"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, (
            f"Expected 422 when graph disabled + graph_mode=naive; "
            f"got {resp.status_code}: {resp.text}"
        )
        # Response body should contain an actionable error message.
        body = resp.json()
        assert "detail" in body, f"Expected 'detail' key in 422 body: {body!r}"
        detail_text = str(body["detail"])
        assert "graph" in detail_text.lower(), (
            f"Expected 'graph' mentioned in 422 detail; got: {detail_text!r}"
        )


def test_e2e_graph_mode_noop_empty_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph.enabled=True but no graph nodes → graph_mode="naive" is a no-op.

    POST /search → 200, graph_expansion_applied=False.

    Covers S5: when no query tokens match any entity in the graph (or the graph
    is empty), expansion is a no-op and the search proceeds normally.

    The spaCy stub returns no entities so ingest creates the collection with chunks
    but leaves graph tables at zero nodes. GraphExpander finds no entity name
    matches in the query → expansionApplied=False → response has
    graph_expansion_applied=False.
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "e1a-t3-empty-graph"
    doc = tmp_path / "simple.txt"
    doc.write_text(
        "The quick brown fox jumps over the lazy dog.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        assert cfg.graph.enabled is True, (
            f"Expected graph.enabled=True, got {cfg.graph.enabled}"
        )

        # Ingest doc so the collection exists (with chunks) but graph tables have 0 nodes.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "AuthService", "graph_mode": "naive"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"Expected 200 (no-op expansion) when graph enabled but empty; "
            f"got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        # Confirm the response is a normal search response (not a fallback error).
        assert "results" in data, (
            f"Expected 'results' key in search response; got: {list(data.keys())}"
        )
        # Presence check first so the subsequent identity check produces a clear message.
        assert "graph_expansion_applied" in data, (
            f"'graph_expansion_applied' key missing from response: {list(data.keys())}"
        )
        # Value check: no entities in the graph → no query token matches → no-op.
        assert data["graph_expansion_applied"] is False, (
            f"Expected graph_expansion_applied=False when graph has zero nodes; "
            f"got {data['graph_expansion_applied']!r}. Full response: {data}"
        )


def test_e2e_mcp_search_graph_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP search with graph_mode="naive" (graph enabled) → well-formed response.

    Covers S8: MCP search graph_mode=naive roundtrip.
    Verifies:
    - No envelope-level exception (isError not set/False).
    - Response has 'results' and 'graph_expansion_applied' keys.
    - graph_expansion_applied is a bool (True or False — depends on whether query
      tokens matched any of the ingested entity names).
    """
    _install_spacy_stub_with_entities(monkeypatch)

    col = "e1a-t3-mcp-search"
    doc = tmp_path / "entity_doc.txt"
    doc.write_text(
        "Alice is a senior engineer at Google. "
        "She maintains the AuthService and coordinates with the TokenValidator team.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        mcp_enabled=True,
    ) as (client, cfg, api_key):
        assert cfg.graph.enabled is True, (
            f"Expected graph.enabled=True, got {cfg.graph.enabled}"
        )

        # Ingest via REST so graph tables are populated with Alice + Google entities.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        session_id = _mcp_initialize(client, api_key)

        # MCP search with graph_mode=naive.
        raw = _mcp_call_tool(client, api_key, session_id, "search", {
            "collection": col,
            "query": "Alice",
            "graph_mode": "naive",
        })

        # Must not be an envelope-level exception.
        rpc_result = raw.get("result", {})
        assert not rpc_result.get("isError"), (
            f"MCP search with graph_mode=naive raised an unhandled exception; "
            f"got isError=True: {rpc_result!r}"
        )

        parsed = _extract_tool_text(raw, "search")

        assert isinstance(parsed, dict), (
            f"MCP search must return a dict; got {type(parsed)!r}: {parsed!r}"
        )
        assert "results" in parsed, (
            f"MCP search response missing 'results' key: {list(parsed.keys())}"
        )
        assert isinstance(parsed["results"], list), (
            f"'results' must be a list; got {type(parsed['results'])!r}"
        )
        assert len(parsed["results"]) > 0, (
            f"Expected at least one search result after ingesting entity-rich doc; "
            f"got empty results list. Full response: {parsed}"
        )
        # graph_expansion_applied must be present as a bool.
        assert "graph_expansion_applied" in parsed, (
            f"'graph_expansion_applied' key missing from MCP search response: "
            f"{list(parsed.keys())}"
        )
        assert isinstance(parsed["graph_expansion_applied"], bool), (
            f"'graph_expansion_applied' must be a bool; "
            f"got {type(parsed['graph_expansion_applied'])!r}: {parsed['graph_expansion_applied']!r}"
        )
        # The query "Alice" matches the "Alice" entity in the graph (stub extracts Alice + Google
        # from every chunk; GraphExpander finds the "alice" node and appends neighbour "Google").
        # Expansion must have applied — this is the positive-path proof for S8.
        assert parsed["graph_expansion_applied"] is True, (
            f"Expected graph_expansion_applied=True: query 'Alice' should match the "
            f"'Alice' entity and expand with neighbour 'Google'; "
            f"got {parsed['graph_expansion_applied']!r}. Full response: {parsed}"
        )


def test_e2e_mcp_search_with_context_graph_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP search_with_context with graph_mode="naive" → code="graph_mode_not_supported".

    Covers FE-3 guard: graph_mode on search_with_context is deferred to E1c.
    The tool must return a well-formed error dict, NOT raise a Python exception
    (no envelope-level isError=True). This follows the same pattern as the
    file_too_large error in ingest_file (E0d / T-2).

    graph_enabled=True is used deliberately: the guard at mcp.py:534 fires on
    ``if graph_mode is not None`` BEFORE any graph-config check, so the error code
    must be returned even when the graph subsystem is fully active. This is the
    stronger and more realistic scenario (a developer with graph enabled attempting
    graph_mode on search_with_context).
    """
    # spaCy stub required because graph_enabled=True triggers _check_graph_deps
    # during create_app, which imports spacy synchronously.
    _install_spacy_stub_with_entities(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, graph_enabled=True) as (client, cfg, api_key):
        assert cfg.graph.enabled is True, (
            f"Expected graph.enabled=True to prove the guard fires even with graph active; "
            f"got {cfg.graph.enabled}"
        )
        session_id = _mcp_initialize(client, api_key)

        raw = _mcp_call_tool(client, api_key, session_id, "search_with_context", {
            "query": "AuthService",
            "graph_mode": "naive",
        })

        # Must NOT be an envelope-level exception.
        rpc_result = raw.get("result", {})
        assert not rpc_result.get("isError"), (
            f"search_with_context with graph_mode must not raise an unhandled exception; "
            f"got isError=True: {rpc_result!r}"
        )

        parsed = _extract_tool_text(raw, "search_with_context")

        assert isinstance(parsed, dict), (
            f"search_with_context must return a dict; got {type(parsed)!r}: {parsed!r}"
        )
        assert parsed.get("code") == "graph_mode_not_supported", (
            f"Expected code='graph_mode_not_supported' for search_with_context + graph_mode; "
            f"got: {parsed!r}"
        )
        assert "error" in parsed, (
            f"Expected 'error' key in error dict; got: {parsed!r}"
        )
        error_msg = parsed["error"]
        assert error_msg, f"error message must be non-empty; got: {parsed!r}"
