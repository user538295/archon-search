"""D9 / BE-3 — Slice 1 integration tests: tool count, lifecycle, enable gate.

Covers:
- test_mcp_tool_list_returns_17_tools — JSON-RPC tools/list with key_store wired
  returns exactly 17 tools.
- test_mcp_tool_list_returns_13_tools_without_key_store — 13 tools when key_store=None.
- test_lifecycle_shutdown — TestClient context-manager exit shuts down MCP cleanly;
  no errors or resource leaks.
- test_mcp_enabled_false_gate — mcp.enabled=False → /mcp returns 404
  (routing gate only; GET /status mcp=null assertion deferred to BE-11).

Scenarios completed (routing gate): S1, S2, S6, S10 (partial — routing only), S14.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _do_mcp_handshake(client, api_key: str) -> str:
    """Perform MCP initialize + notifications/initialized; return session_id."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "be3-test", "version": "1.0"},
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200, f"initialize failed: {resp.status_code} {resp.text}"
    session_id = resp.headers["mcp-session-id"]
    # Send notifications/initialized (no response body expected — 202)
    resp2 = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**headers, "mcp-session-id": session_id},
    )
    assert resp2.status_code in (200, 202, 204), (
        f"notifications/initialized failed: {resp2.status_code} {resp2.text}"
    )
    return session_id


def _mcp_tools_list(client, api_key: str) -> list[dict]:
    """Run a full MCP handshake and call tools/list; return the list of tool dicts."""
    session_id = _do_mcp_handshake(client, api_key)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "mcp-session-id": session_id,
    }
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=headers,
    )
    assert resp.status_code == 200, f"tools/list failed: {resp.status_code} {resp.text}"
    # Response is SSE: lines like "event: message\ndata: {...}\n\n"
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: "):])
            if "result" not in payload:
                raise AssertionError(
                    f"tools/list SSE data line has no 'result' key "
                    f"(got error or unexpected payload): {payload!r}"
                )
            return payload["result"]["tools"]
    raise AssertionError(f"No SSE data line in tools/list response: {resp.text!r}")


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_mcp_tool_list_returns_19_tools(tmp_path, monkeypatch) -> None:
    """JSON-RPC tools/list with key_store wired returns exactly 19 tools.

    create_app() in app.py always wires app.state.key_store to
    create_mcp_http_app(), so all 19 tools (15 base + 4 key-management) are
    registered.  Completes S2 (19-tool list, updated for E2b) and S14 (key_store present).
    Base tools: search, search_with_context, explain, ingest_file, ingest_directory,
    list_collections, get_collections_meta, get_collection_meta, list_documents,
    delete_document, update_collection, export_collection, import_collection,
    get_graph, get_graph_cross_collection (15 total).
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        tools = _mcp_tools_list(client, api_key)
        tool_names = [t["name"] for t in tools]
        assert len(tools) == 19, f"Expected 19 tools, got {len(tools)}: {tool_names}"
        # Verify the 4 key-management tools are among them (S14)
        for key_tool in ("create_key", "list_keys", "revoke_key", "rotate_key"):
            assert key_tool in tool_names, f"Key-management tool {key_tool!r} missing"
        # Verify the 2 graph inspection tools are present (E2b)
        for graph_tool in ("get_graph", "get_graph_cross_collection"):
            assert graph_tool in tool_names, f"Graph tool {graph_tool!r} missing"


def test_mcp_tool_list_returns_15_tools_without_key_store(tmp_path, monkeypatch) -> None:
    """15 base tools when key_store=None (4 key-management tools are not registered).

    Patches create_mcp_http_app in its defining module (archon_search.server.mcp)
    to pass key_store=None so the key-management tools are omitted.  The lifespan
    in app.py imports the function with a local import, so patching the module
    attribute is the correct target.  Completes S14 (absence side).
    Base tools: 13 (original) + 2 (E2b graph inspection) = 15.
    """
    from archon_search.server.mcp import create_mcp_http_app as _real_factory

    def _factory_no_key_store(**kwargs):
        kwargs["key_store"] = None
        return _real_factory(**kwargs)

    with patch("archon_search.server.mcp.create_mcp_http_app", side_effect=_factory_no_key_store):
        with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (
            client,
            _cfg,
            api_key,
        ):
            tools = _mcp_tools_list(client, api_key)
            tool_names = [t["name"] for t in tools]
            assert len(tools) == 15, (
                f"Expected 15 base tools (no key_store), got {len(tools)}: {tool_names}"
            )
            # Key-management tools must be absent
            for key_tool in ("create_key", "list_keys", "revoke_key", "rotate_key"):
                assert key_tool not in tool_names, (
                    f"Key-management tool {key_tool!r} present without key_store"
                )


def test_lifecycle_shutdown(tmp_path, monkeypatch, caplog) -> None:
    """TestClient context-manager exit shuts down MCP mount cleanly.

    Entering the TestClient context starts the FastAPI lifespan (which mounts
    the MCP app and enters the FastMCP StreamableHTTPSessionManager task-group
    lifespan).  Exiting the TestClient context triggers the lifespan teardown,
    which exits the AsyncExitStack containing the MCP sub-app lifespan context.
    No exceptions must escape this teardown path.  Completes S6.
    """
    # The TestClient context manager raises on any lifespan error.
    # If shutdown is unclean, it would propagate here.
    with caplog.at_level(logging.WARNING, logger="archon_search"):
        with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
            # Confirm the app is live inside the context
            assert client.get("/health").status_code == 200
            # Also confirm MCP is reachable before context exit (proves session manager active)
            resp = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "shutdown-test", "version": "1.0"},
                    },
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert resp.status_code == 200
            assert resp.headers.get("mcp-session-id"), "MCP session manager was not active"
    # No exception reaching here means clean shutdown.  S6 proven.
    # No WARNING/ERROR related to MCP during startup or shutdown confirms clean lifecycle.
    # Catches loggers whose name contains "mcp" (e.g. archon_search.server.mcp, mcp.*)
    # AND loggers whose message references MCP (e.g. archon_search.server.app warning
    # "MCP server failed to start; continuing without MCP").
    mcp_errors = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and (
            "mcp" in r.name.lower()
            or "mcp" in r.getMessage().lower()
        )
    ]
    assert mcp_errors == [], (
        f"MCP-related warnings/errors during lifecycle: {[r.getMessage() for r in mcp_errors]}"
    )


def test_mcp_enabled_false_gate(tmp_path, monkeypatch) -> None:
    """mcp.enabled=False → /mcp returns 404; GET /status returns mcp field as null.

    Completes S10.  The status mcp-null assertion requires BE-8 to be
    implemented; this test confirms the absence of the /mcp mount only.
    For the status null assertion see test_status_mcp_null_when_disabled in
    BE-11 — here we test the routing-level gate.
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=False) as (
        client,
        _cfg,
        api_key,
    ):
        # /mcp must not be mounted — any method returns 404
        resp_get = client.get("/mcp", headers={"Authorization": f"Bearer {api_key}"})
        assert resp_get.status_code == 404, (
            f"Expected 404 on /mcp when disabled, got {resp_get.status_code}"
        )
        resp_post = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        assert resp_post.status_code == 404, (
            f"Expected 404 POST /mcp when disabled, got {resp_post.status_code}"
        )
        # REST must still be healthy
        assert client.get("/health").status_code == 200
