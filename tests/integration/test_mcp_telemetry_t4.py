"""D9 / T-4 — Telemetry wiring e2e: MCP tool call → entry in telemetry log.

Tests:
- ``test_mcp_telemetry_entry_in_log``: start app with telemetry enabled; call ``search``
  via MCP; read JSONL log; assert entry present with expected field shapes and no raw
  query string (per telemetry no-raw-query structural invariant).

Scenario completed: S9 (e2e — MCP tool call produces telemetry entry).
"""
from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]

# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers (self-contained — no cross-test coupling)
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
                "clientInfo": {"name": "t4-telemetry-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, (
        f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    )
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize must return mcp-session-id header"
    # Send notifications/initialized (fire-and-forget; 200/202/204 all valid per MCP spec)
    notif_resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_mcp_headers(token, session_id),
    )
    assert notif_resp.status_code < 400, (
        f"notifications/initialized returned unexpected error: {notif_resp.status_code}"
    )
    return session_id


def _mcp_call_search(client, token: str, session_id: str, collection: str, query: str) -> dict:
    """Call the MCP ``search`` tool and return the parsed SSE result payload."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {"query": query, "collection": collection},
            },
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, (
        f"MCP tools/call (search) failed: {resp.status_code} {resp.text[:300]}"
    )
    # Collect all SSE data: lines; take the last — the final frame carries the result.
    # Intermediate frames (e.g., progress events) appear before the final result frame.
    data_lines = [
        line[5:].strip()
        for line in resp.text.split("\n")
        if line.startswith("data:")
    ]
    assert data_lines, f"No data: line in SSE response: {resp.text[:300]!r}"
    return json.loads(data_lines[-1])


# ---------------------------------------------------------------------------
# T-4 e2e test — telemetry entry in JSONL log after MCP search call
# ---------------------------------------------------------------------------


def test_mcp_telemetry_entry_in_log(tmp_path, monkeypatch) -> None:
    """Start app with telemetry enabled; call search via MCP; read JSONL log;
    assert entry present with expected field shapes (no raw query string).

    This test proves the end-to-end telemetry wiring for MCP tool calls:
    1. The TelemetryWriter is wired into create_mcp_http_app() from the lifespan.
    2. A successful MCP search call writes a JSONL entry.
    3. The entry has the correct shape: endpoint="search", status="ok",
       query_id present, timestamp present, latency_ms present.
    4. The no-raw-query structural invariant is satisfied: no "query" key in the entry.

    A real document is ingested first so the search succeeds (status="ok") and the
    success-path telemetry code fires rather than the error-path.

    TestClient context-manager exit triggers drain_and_stop() which flushes all pending
    telemetry entries before returning. The JSONL log is read after the context exits
    to guarantee all entries are on disk.

    Scenario S9 (e2e proof): MCP tool call → telemetry entry in JSONL log.
    """
    collection = "mcp-telemetry-t4"
    query = "photosynthesis chlorophyll light reaction"
    doc_content = textwrap.dedent(f"""\
        This document is the T-4 telemetry e2e test fixture.
        {query} — distinctive phrase for retrieval verification.
        Archon Search MCP telemetry end-to-end test document.
    """)

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, telemetry_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Ingest a real document so the search hits the success path (status="ok").
        # A missing collection causes a not-found error path, writing status!="ok".
        doc_file = tmp_path / "t4-telemetry" / "doc.txt"
        doc_file.parent.mkdir(parents=True, exist_ok=True)
        doc_file.write_text(doc_content)
        ingest_file_via_path(client, collection, str(doc_file), api_key=api_key, timeout_s=30.0)

        session_id = _mcp_initialize(client, api_key)
        result = _mcp_call_search(client, api_key, session_id, collection, query)

        # Guard against JSON-RPC transport-level errors (distinct from tool-level isError).
        assert "error" not in result, (
            f"MCP search returned JSON-RPC transport error: {result.get('error')!r}. "
            f"Full result: {result!r}"
        )
        # Verify MCP search itself succeeded (not an isError envelope) and returned results.
        rpc_result = result.get("result", {})
        assert not rpc_result.get("isError"), (
            f"MCP search returned isError=True — unexpected tool error. "
            f"Full result: {result!r}"
        )
        # Verify content items returned and the fixture phrase appears in the response.
        content = rpc_result.get("content", [])
        assert content, (
            f"MCP search returned no content items — the pipeline may have returned zero results. "
            f"Ingest must succeed so the search finds the document. "
            f"rpc_result: {rpc_result!r}"
        )
        # Extract text from content dicts (MCP returns [{"type": "text", "text": "..."}]).
        # Using c.get("text", "") avoids dict repr via str(c) which gives wrong substring matches.
        content_text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        assert query in content_text or collection in content_text, (
            f"MCP search content does not reference the query phrase or collection — "
            f"result may be from the wrong document. content_text: {content_text[:300]!r}"
        )
        # Context-manager exit triggers drain_and_stop() — all queued entries flushed.

    # Read the JSONL log after full shutdown (drain_and_stop guarantees flush).
    log_dir = Path(cfg.telemetry.log_dir)
    jsonl_files = list(log_dir.glob("*.jsonl")) if log_dir.exists() else []
    assert jsonl_files, (
        "No JSONL telemetry files found after MCP search call with telemetry enabled. "
        "The TelemetryWriter must be passed into create_mcp_http_app() from the lifespan, "
        "not instantiated separately — check wiring in server/app.py lifespan."
    )

    entries: list[dict] = []
    for jsonl_file in jsonl_files:
        for line in jsonl_file.read_text().splitlines():
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))

    assert entries, "JSONL telemetry file(s) found but contain no entries."

    # Filter to entries from the MCP search call.
    search_entries = [e for e in entries if e.get("endpoint") == "search"]
    assert search_entries, (
        f"Expected at least one telemetry entry with endpoint='search', "
        f"got endpoints: {[e.get('endpoint') for e in entries]!r}. "
        "Check that the MCP search tool closure calls writer.enqueue() on the success path."
    )
    # Assert at least one search entry — proves the writer fired. Avoid over-constraining
    # to exactly-one, since future features (e.g. RAG Fusion sub-queries) may produce
    # additional entries without breaking the telemetry wiring under test.
    assert len(search_entries) >= 1, (
        f"Expected at least 1 telemetry entry with endpoint='search', "
        f"got {len(search_entries)}. "
        f"All entries: {entries!r}"
    )

    entry = search_entries[0]

    # --- Status must be "ok" (proves success path, not error path) ---
    assert entry.get("status") == "ok", (
        f"Expected telemetry entry status='ok' (success path), "
        f"got status={entry.get('status')!r}. "
        f"Full entry: {entry!r}. "
        "The test collection must exist so the MCP search succeeds."
    )

    # --- Structural field shapes ---
    assert "query_id" in entry, (
        f"Telemetry entry must have 'query_id' field. Entry keys: {list(entry.keys())}"
    )
    assert isinstance(entry["query_id"], str) and entry["query_id"], (
        f"'query_id' must be a non-empty string, got: {entry['query_id']!r}"
    )

    assert "timestamp" in entry, (
        f"Telemetry entry must have 'timestamp' field. Entry keys: {list(entry.keys())}"
    )
    assert isinstance(entry["timestamp"], str) and entry["timestamp"], (
        f"'timestamp' must be a non-empty string (ISO-8601), got: {entry['timestamp']!r}"
    )

    assert "latency_ms" in entry, (
        f"Telemetry entry must have 'latency_ms' field. Entry keys: {list(entry.keys())}"
    )
    assert isinstance(entry["latency_ms"], (int, float)) and entry["latency_ms"] >= 0, (
        f"'latency_ms' must be a non-negative number, got: {entry['latency_ms']!r}"
    )

    assert entry.get("collection") == collection, (
        f"Telemetry entry 'collection' must be {collection!r}, "
        f"got: {entry.get('collection')!r}"
    )

    # --- result_count and result_doc_ids: prove the search returned actual results ---
    # Without these, a zero-result search (status="ok" with empty list) passes silently.
    assert entry.get("result_count", 0) >= 1, (
        f"Telemetry entry 'result_count' must be >= 1 — the ingested document must be found. "
        f"Got result_count={entry.get('result_count')!r}. Full entry: {entry!r}"
    )
    result_doc_ids = entry.get("result_doc_ids")
    assert isinstance(result_doc_ids, list) and len(result_doc_ids) >= 1, (
        f"Telemetry entry 'result_doc_ids' must be a non-empty list. "
        f"Got: {result_doc_ids!r}. Full entry: {entry!r}"
    )
    assert all(isinstance(d, str) and d for d in result_doc_ids), (
        f"All 'result_doc_ids' must be non-empty strings. Got: {result_doc_ids!r}"
    )
    assert entry.get("result_count") == len(result_doc_ids), (
        f"'result_count' ({entry.get('result_count')}) must equal len(result_doc_ids) "
        f"({len(result_doc_ids)}). Internal consistency check failed."
    )
    # Assert the specific document we ingested appears — proves correct-document retrieval.
    # doc_id is sha256(str(resolved_path).encode()).hexdigest() per pipeline.py:287.
    expected_doc_id = hashlib.sha256(str(doc_file.resolve()).encode()).hexdigest()
    assert expected_doc_id in result_doc_ids, (
        f"Expected doc_id {expected_doc_id!r} (derived from {str(doc_file)!r}) "
        f"not found in result_doc_ids: {result_doc_ids!r}. "
        "The search must return the specific ingested document, not an unrelated one."
    )

    # --- filter_flags: privacy-safe shape (no raw filter values leaked) ---
    # The search call uses no filters, so all filter_flags must be False.
    filter_flags = entry.get("filter_flags", {})
    assert isinstance(filter_flags, dict), (
        f"'filter_flags' must be a dict (FilterFlags shape). Got: {filter_flags!r}"
    )
    assert all(isinstance(v, bool) for v in filter_flags.values()), (
        f"All 'filter_flags' values must be booleans (privacy-safe: no raw values). "
        f"Got: {filter_flags!r}"
    )
    assert all(v is False for v in filter_flags.values()), (
        f"No filters were applied in this search, so all filter_flags must be False. "
        f"Got: {filter_flags!r}"
    )

    # --- No-raw-query structural invariant ---
    # The telemetry schema has no 'query' field by design (CLAUDE.md structural invariant).
    assert "query" not in entry, (
        f"Telemetry entry must NOT contain a 'query' field (no-raw-query structural invariant). "
        f"Entry keys: {list(entry.keys())}"
    )
