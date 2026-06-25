"""D8 / BE-4 — Wire doc_id_hasher into routes_search.py + mcp.py.

Covers:
- test_search_endpoint_with_hashing_enabled_writes_hashed_doc_ids (S2)
- test_search_endpoint_with_hashing_disabled_writes_raw_doc_ids (S1)
- test_mcp_search_tool_with_hashing_enabled_writes_hashed_doc_ids (S9)
- test_mcp_search_with_context_hashing (S9)
- test_concurrent_async_search_requests_all_entries_consistent (S8)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import textwrap
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]

# Number of concurrent requests for the S8 concurrency test.
_N_CONCURRENT = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_telemetry_entries(cfg: Any) -> list[dict]:
    """Read all JSONL telemetry entries written to cfg.telemetry.log_dir."""
    log_dir = Path(cfg.telemetry.log_dir)
    entries = []
    for jsonl_file in log_dir.glob("*.jsonl"):
        for line in jsonl_file.read_text().splitlines():
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))
    return entries


def _raw_doc_id(file_path: Path) -> str:
    """Compute the SHA-256 doc_id used by the pipeline for a file path.

    Must match pipeline.py's doc_id computation: sha256(str(path.resolve())).
    """
    return hashlib.sha256(str(file_path.resolve()).encode()).hexdigest()


def _ingest_and_poll(client: Any, col: str, file_path: Path, api_key: str) -> None:
    """POST /ingest via file path and poll until DONE."""
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = client.post(
        "/ingest", json={"collection": col, "path": str(file_path)}, headers=headers
    )
    assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
    job_id = resp.json()["job_id"]
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=headers)
        assert r.status_code == 200
        if r.json()["status"] == "DONE":
            return
        if r.json()["status"] == "FAILED":
            pytest.fail(f"ingest job failed: {r.json()}")
        time.sleep(0.1)
    pytest.fail("ingest did not complete in 30s")


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
                "clientInfo": {"name": "be4-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize response must return mcp-session-id header"
    return session_id


def _call_mcp_tool(
    client: Any, token: str, session_id: str, name: str, arguments: dict
) -> dict:
    """Call an MCP tool and return the parsed SSE result dict."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, f"MCP tools/call failed: {resp.status_code} {resp.text[:300]}"
    for line in resp.text.split("\n"):
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"No data: line in SSE response: {resp.text[:300]!r}")


# ---------------------------------------------------------------------------
# REST /search — hashing enabled (S2)
# ---------------------------------------------------------------------------


def test_search_endpoint_with_hashing_enabled_writes_hashed_doc_ids(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """REST /search with hash_doc_ids=True → JSONL entry has hashed (not raw) doc_ids (S2).

    Asserts:
    - doc_ids_hashed == True in the telemetry entry.
    - Every result_doc_id is 64 hex chars (HMAC-SHA256 output width).
    - The raw SHA-256 path-derived doc_id does NOT appear in result_doc_ids (proves hashing
      was genuinely applied — raw SHA-256 is also 64 hex so format alone is insufficient).
    """
    text_file = tmp_path / "col-on" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text(textwrap.dedent("hello world document for hashing enabled test"))

    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "col-on", text_file, api_key)
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.post(
            "/search",
            json={"collection": "col-on", "query": "hello"},
            headers=headers,
        )
        assert resp.status_code == 200, f"search failed: {resp.text}"
        results = resp.json()["results"]
        # The search must return at least one result — we ingested a document for exactly this.
        assert results, (
            "Search returned 0 results after ingest — the test fixture did not produce retrievable "
            "data. Cannot verify hashing without a non-empty result_doc_ids list."
        )
        # Context exit triggers drain_and_stop() — all queued entries flushed.

    entries = _read_telemetry_entries(cfg)
    search_ok = [e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"]
    assert search_ok, f"No search/ok telemetry entries found. All entries: {entries!r}"
    entry = search_ok[0]

    assert entry.get("doc_ids_hashed") is True, (
        f"Expected doc_ids_hashed=True with hashing enabled; got {entry.get('doc_ids_hashed')!r}. "
        "routes_search.py must pass doc_id_hasher=getattr(request.app.state, 'doc_id_hasher', None). "
        f"Entry: {entry!r}"
    )

    raw_doc_id = _raw_doc_id(text_file)
    result_doc_ids = entry.get("result_doc_ids") or []
    assert result_doc_ids, (
        f"Expected non-empty result_doc_ids in telemetry entry — search returned {len(results)} "
        f"results so the entry must have doc_ids. Entry: {entry!r}"
    )
    for hashed_id in result_doc_ids:
        assert len(hashed_id) == 64, (
            f"Expected 64-char HMAC hex; got length {len(hashed_id)}: {hashed_id!r}"
        )
    assert raw_doc_id not in result_doc_ids, (
        f"Raw doc_id {raw_doc_id!r} must not appear in hashed result_doc_ids — "
        "hashing was not applied."
    )


# ---------------------------------------------------------------------------
# REST /search — hashing disabled (S1)
# ---------------------------------------------------------------------------


def test_search_endpoint_with_hashing_disabled_writes_raw_doc_ids(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """REST /search with hash_doc_ids=False (default) → raw doc_ids and doc_ids_hashed=False (S1)."""
    text_file = tmp_path / "col-off" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text(textwrap.dedent("hello world document for raw hash test"))

    with make_real_app(tmp_path, monkeypatch, telemetry_enabled=True) as (client, cfg, api_key):
        ingest_file_via_path(client, "col-off", str(text_file), api_key=api_key, timeout_s=30.0)
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.post(
            "/search",
            json={"collection": "col-off", "query": "hello"},
            headers=headers,
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results, (
            "Search returned 0 results after ingest — cannot verify raw doc_id without a result."
        )

    entries = _read_telemetry_entries(cfg)
    search_ok = [e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"]
    assert search_ok, f"No search/ok telemetry entries found. All entries: {entries!r}"
    entry = search_ok[0]

    assert entry.get("doc_ids_hashed") is False, (
        f"Expected doc_ids_hashed=False with hashing disabled; got {entry.get('doc_ids_hashed')!r}"
    )

    raw_doc_id = _raw_doc_id(text_file)
    result_doc_ids = entry.get("result_doc_ids") or []
    assert result_doc_ids, "Expected non-empty result_doc_ids in telemetry entry."
    assert raw_doc_id in result_doc_ids, (
        f"Expected raw doc_id {raw_doc_id!r} in result_doc_ids when hashing disabled; "
        f"got: {result_doc_ids!r}"
    )


# ---------------------------------------------------------------------------
# MCP search tool — hashing enabled (S9)
# ---------------------------------------------------------------------------


def test_mcp_search_tool_with_hashing_enabled_writes_hashed_doc_ids(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """MCP search tool with hash_doc_ids=True → JSONL entry has hashed doc_ids (S9).

    Proves that create_mcp_http_app() accepts and threads doc_id_hasher from
    app.state into the MCP search tool closure.
    """
    text_file = tmp_path / "mcp-col" / "mcp-doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("mcp search telemetry hashing test document")

    with make_real_app(
        tmp_path, monkeypatch, mcp_enabled=True, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "mcp-col", text_file, api_key)
        session_id = _mcp_initialize(client, api_key)
        _call_mcp_tool(
            client, api_key, session_id,
            "search",
            {"query": "mcp search hashing", "collection": "mcp-col"},
        )
        # Context exit triggers drain_and_stop()

    entries = _read_telemetry_entries(cfg)
    search_ok = [e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"]
    assert search_ok, (
        f"No search/ok telemetry entries found after MCP search. All entries: {entries!r}"
    )
    entry = search_ok[0]

    assert entry.get("doc_ids_hashed") is True, (
        f"Expected doc_ids_hashed=True via MCP search path; got {entry.get('doc_ids_hashed')!r}. "
        "create_mcp_http_app() must accept and pass doc_id_hasher from app.state. "
        f"Entry: {entry!r}"
    )

    raw_doc_id = _raw_doc_id(text_file)
    result_doc_ids = entry.get("result_doc_ids") or []
    assert result_doc_ids, (
        f"MCP search returned no doc_ids — ingest did not produce retrievable data. "
        f"Cannot verify that hashing was applied. Entry: {entry!r}"
    )
    for hashed_id in result_doc_ids:
        assert len(hashed_id) == 64, (
            f"Expected 64-char HMAC hex; got length {len(hashed_id)}: {hashed_id!r}"
        )
    assert raw_doc_id not in result_doc_ids, (
        "Raw doc_id must not appear in hashed result_doc_ids via MCP search path."
    )


# ---------------------------------------------------------------------------
# MCP search_with_context tool — hashing enabled (S9)
# ---------------------------------------------------------------------------


def test_mcp_search_with_context_hashing(tmp_path: Path, monkeypatch: Any) -> None:
    """MCP search_with_context tool with hash_doc_ids=True → JSONL entry has hashed doc_ids (S9)."""
    text_file = tmp_path / "swc-col" / "swc-doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("search with context hashing test document content here")

    with make_real_app(
        tmp_path, monkeypatch, mcp_enabled=True, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "swc-col", text_file, api_key)
        session_id = _mcp_initialize(client, api_key)
        _call_mcp_tool(
            client, api_key, session_id,
            "search_with_context",
            {"query": "context hashing", "collection": "swc-col"},
        )

    entries = _read_telemetry_entries(cfg)
    swc_ok = [
        e for e in entries
        if e.get("endpoint") == "search_with_context" and e.get("status") == "ok"
    ]
    assert swc_ok, (
        f"No search_with_context/ok entries found after MCP call. All entries: {entries!r}"
    )
    entry = swc_ok[0]

    assert entry.get("doc_ids_hashed") is True, (
        f"Expected doc_ids_hashed=True for search_with_context via MCP; "
        f"got {entry.get('doc_ids_hashed')!r}. "
        f"mcp.py search_with_context tool must pass doc_id_hasher. Entry: {entry!r}"
    )

    raw_doc_id = _raw_doc_id(text_file)
    result_doc_ids = entry.get("result_doc_ids") or []
    assert result_doc_ids, (
        f"MCP search_with_context returned no doc_ids — ingest did not produce retrievable data. "
        f"Cannot verify that hashing was applied. Entry: {entry!r}"
    )
    for hashed_id in result_doc_ids:
        assert len(hashed_id) == 64, (
            f"Expected 64-char HMAC hex; got length {len(hashed_id)}: {hashed_id!r}"
        )
    assert raw_doc_id not in result_doc_ids, (
        "Raw doc_id must not appear in hashed result_doc_ids via search_with_context MCP path."
    )


# ---------------------------------------------------------------------------
# Concurrent REST searches — consistency (S8)
# ---------------------------------------------------------------------------


def test_concurrent_async_search_requests_all_entries_consistent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """N concurrent async searches — all JSONL entries consistent (S8).

    Verifies:
    - Exactly N search/ok telemetry entries are written.
    - Each entry has doc_ids_hashed=True (hashing enabled).
    - Each result_doc_id is 64 hex chars (HMAC output width).
    - All N entries for the same document have the same hashed doc_ids (HMAC determinism).
    - The telemetry writer (asyncio.Queue put_nowait) handles concurrent enqueues without
      corruption or count discrepancy.

    Uses httpx.AsyncClient with ASGITransport for async-concurrent HTTP requests.
    TestClient's ASGI portal is synchronous; asyncio.run() creates a fresh event loop
    in the test thread (valid on Python 3.12). The TelemetryWriter.enqueue() uses
    asyncio.Queue.put_nowait() which is synchronous and not loop-bound — safe to call
    from the new event loop. All entries are drained during TestClient.__exit__.
    """
    text_file = tmp_path / "concurrent-col" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("concurrent hashing document for telemetry consistency test")

    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "concurrent-col", text_file, api_key)
        headers = {"Authorization": f"Bearer {api_key}"}

        # Use httpx.AsyncClient + ASGITransport to drive N concurrent requests within one
        # event loop. TestClient is synchronous; asyncio.run() is valid here (Python 3.12+)
        # because TestClient's anyio portal runs in a background thread, leaving the test
        # thread without a running event loop. ASGITransport calls the ASGI app directly.
        app = client.app

        async def _do_concurrent_searches() -> list:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as aclient:
                tasks = [
                    aclient.post(
                        "/search",
                        json={"collection": "concurrent-col", "query": "concurrent hashing"},
                        headers=headers,
                        timeout=30.0,
                    )
                    for _ in range(_N_CONCURRENT)
                ]
                return await asyncio.gather(*tasks)

        responses = asyncio.run(_do_concurrent_searches())
        # Context exit triggers drain_and_stop() — all queued entries flushed to disk.

    for i, resp in enumerate(responses):
        assert resp.status_code == 200, (
            f"Concurrent search {i} failed: {resp.status_code} {resp.text[:200]}"
        )

    entries = _read_telemetry_entries(cfg)
    search_ok = [e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"]

    assert len(search_ok) == _N_CONCURRENT, (
        f"Expected exactly {_N_CONCURRENT} search/ok telemetry entries; "
        f"got {len(search_ok)}. All entries: {entries!r}"
    )

    all_doc_id_sets: list[list[str]] = []
    for entry in search_ok:
        assert entry.get("doc_ids_hashed") is True, (
            f"Expected doc_ids_hashed=True for all concurrent entries; "
            f"got {entry.get('doc_ids_hashed')!r} in {entry!r}"
        )
        result_doc_ids = entry.get("result_doc_ids") or []
        for hashed_id in result_doc_ids:
            assert len(hashed_id) == 64, (
                f"Expected 64-char HMAC hex; got length {len(hashed_id)}: {hashed_id!r}"
            )
        all_doc_id_sets.append(result_doc_ids)

    # All N concurrent searches for the same document must produce the same hashed doc_ids,
    # proving HMAC determinism under concurrency (same salt + same input → same output).
    if all_doc_id_sets and all_doc_id_sets[0]:
        reference = all_doc_id_sets[0]
        for i, doc_ids in enumerate(all_doc_id_sets[1:], start=1):
            assert doc_ids == reference, (
                f"Concurrent entry {i} has different result_doc_ids than entry 0: "
                f"{doc_ids!r} != {reference!r}. HMAC must be deterministic."
            )
