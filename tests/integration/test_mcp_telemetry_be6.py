"""D9 / BE-6 — Asymmetry fix #3: wire telemetry writer + key_store from lifespan.

Covers:
- test_mcp_writer_none_tool_executes_normally (unit) — create_mcp_http_app(writer=None)
  → calling a tool via the full stack succeeds with no AttributeError.
- test_mcp_telemetry_entry_written (integration) — real TelemetryWriter wired; calling
  the MCP `search` tool writes a JSONL entry with expected fields.
- test_mcp_telemetry_disabled_no_entry (integration) — writer=None; calling MCP `search`
  produces no JSONL entry.

Scenarios completed: S9 (telemetry entry logged), S13 (writer=None → no errors).
Contract completed: C2 (completed — writer + key_store wired from lifespan).
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# Helpers shared across tests
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
    """Send the MCP initialize handshake and return the session_id."""
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
    assert session_id, "MCP initialize response must return mcp-session-id header"
    return session_id


def _call_mcp_search(client, token: str, session_id: str) -> dict:
    """Call the MCP `search` tool and return the parsed SSE result."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {"query": "hello world", "collection": "test-col"},
            },
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, f"MCP tools/call failed: {resp.status_code} {resp.text[:300]}"
    for line in resp.text.split("\n"):
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"No data: line in SSE response: {resp.text[:300]!r}")


# ---------------------------------------------------------------------------
# Unit — writer=None: tool call must not raise AttributeError
# ---------------------------------------------------------------------------


def test_mcp_writer_none_tool_executes_normally(tmp_path, monkeypatch) -> None:
    """create_mcp_http_app(writer=None) → MCP search tool call succeeds, no AttributeError.

    Proves that all `if writer is not None:` guards in tool closures are correct —
    no guard-missing code path attempts `writer.enqueue(...)` when writer is None.

    Uses the full-stack MCP app (via make_real_app + TestClient) with writer=None
    (telemetry disabled), then calls the `search` tool and asserts no AttributeError
    or 500 response. The search returns an empty result (no real data) — that is
    expected and irrelevant to this test's goal.

    S13: writer=None → no errors.
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        # telemetry is disabled by default → writer=None is wired into create_mcp_http_app()
        session_id = _mcp_initialize(client, api_key)
        result = _call_mcp_search(client, api_key, session_id)

        # The result must not be an isError=True response — a missing `if writer is not None:`
        # guard would cause `None.enqueue()` which raises AttributeError, serialized by FastMCP
        # as isError=True. An empty result list (collection not found → success path, 0 results)
        # is acceptable and expected here; we only care that no AttributeError occurred.
        assert not result.get("result", {}).get("isError"), (
            f"Tool returned isError=True — likely AttributeError from missing writer=None guard. "
            f"Full result: {result!r}"
        )


# ---------------------------------------------------------------------------
# Integration — telemetry enabled: JSONL entry written after MCP search call
# ---------------------------------------------------------------------------


def test_mcp_telemetry_entry_written(tmp_path, monkeypatch) -> None:
    """Real TelemetryWriter wired; MCP `search` tool call writes a JSONL telemetry entry.

    Enables telemetry via make_real_app(telemetry_enabled=True). Creates a real collection
    via ingest_file_via_path so the search succeeds and the success-path telemetry code
    (mcp.py:393) fires — not the error-path. After the MCP search call, reads the JSONL
    log directory and asserts at least one entry with endpoint=="search" and status=="ok".

    Note: the telemetry writer uses async queuing; the TestClient context-manager exit
    triggers drain_and_stop() which flushes all pending entries before returning.
    The log is read AFTER exiting the context so all entries are guaranteed on disk.

    S9: telemetry entry logged after MCP tool call.
    C2: writer from lifespan wired to create_mcp_http_app().
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, telemetry_enabled=True) as (
        client,
        _cfg,
        api_key,
    ):
        # Create a real collection so the search succeeds (success-path telemetry fires).
        text_file = tmp_path / "test-col" / "doc.txt"
        text_file.parent.mkdir(parents=True, exist_ok=True)
        text_file.write_text(
            textwrap.dedent("hello world document for mcp telemetry test")
        )
        ingest_file_via_path(client, "test-col", str(text_file), api_key=api_key, timeout_s=30.0)

        session_id = _mcp_initialize(client, api_key)
        _call_mcp_search(client, api_key, session_id)
        # Context exit triggers drain_and_stop() — all queued entries flushed to disk.

    # Read the JSONL log after full shutdown (drain_and_stop guarantees flush).
    log_dir = Path(_cfg.telemetry.log_dir)  # always tmp_path/search-logs per make_real_app
    jsonl_files = list(log_dir.glob("*.jsonl"))
    assert jsonl_files, (
        "No JSONL telemetry files found after MCP search call. "
        "writer must be wired from lifespan into create_mcp_http_app() (BE-6)."
    )

    entries = []
    for jsonl_file in jsonl_files:
        for line in jsonl_file.read_text().splitlines():
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))

    assert entries, "JSONL file(s) found but contain no entries."

    # Assert at least one entry with endpoint=="search" (the MCP search tool writes one)
    search_entries = [e for e in entries if e.get("endpoint") == "search"]
    assert search_entries, (
        f"Expected a 'search' telemetry entry from MCP tool call; got endpoints: "
        f"{[e.get('endpoint') for e in entries]!r}. "
        "Check that the search tool's `if writer is not None: writer.enqueue(...)` path fires."
    )

    # The search must have succeeded (status=="ok") — not hit the error path.
    entry = search_entries[0]
    assert entry.get("status") == "ok", (
        f"Expected telemetry entry status='ok' (success path), got status={entry.get('status')!r}. "
        f"Full entry: {entry!r}. "
        "The test collection must exist so the search succeeds."
    )

    # Structural invariant: no raw query string in the entry (per telemetry no-raw-query guarantee)
    assert "query" not in entry, (
        f"Telemetry entry must not contain a raw 'query' field (no-raw-query invariant). "
        f"Entry keys: {list(entry.keys())}"
    )


# ---------------------------------------------------------------------------
# Integration — telemetry disabled (writer=None): no JSONL entry after MCP call
# ---------------------------------------------------------------------------


def test_mcp_telemetry_disabled_no_entry(tmp_path, monkeypatch) -> None:
    """writer=None (telemetry disabled); calling MCP `search` produces no JSONL entry.

    Proves the `if writer is not None:` guard prevents enqueue() from being called
    when telemetry is off. The log directory remains empty (no files created).

    Uses make_real_app (telemetry disabled by default) and checks _cfg.telemetry.log_dir
    — the actual path the app would write to — rather than a manually created directory
    that app code never references.

    S13: writer=None → no errors, no JSONL written.
    C2: writer=None correctly passed through; no crash.
    """
    # telemetry is disabled by default in make_real_app → writer=None wired to MCP
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        session_id = _mcp_initialize(client, api_key)
        _call_mcp_search(client, api_key, session_id)

    # After context exit (drain_and_stop() called), the actual telemetry log_dir must
    # have no entries.  make_real_app always sets cfg.telemetry.log_dir to
    # tmp_path/search-logs so this check is definitively correct.
    log_dir = Path(_cfg.telemetry.log_dir)
    jsonl_files = list(log_dir.glob("*.jsonl")) if log_dir.exists() else []
    entries = []
    for jsonl_file in jsonl_files:
        for line in jsonl_file.read_text().splitlines():
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))

    assert not entries, (
        f"Expected no telemetry entries with writer=None, but got {len(entries)} entries: "
        f"{entries[:3]!r}. "
        "The `if writer is not None:` guard in the search tool closure must prevent enqueue()."
    )
