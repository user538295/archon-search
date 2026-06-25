"""D8 / T-1 — e2e: core hashing behaviour.

Scenarios covered:
- S1: hash_doc_ids=false (default) → raw result_doc_ids, doc_ids_hashed=false
- S2: hash_doc_ids=true → 64-char HMAC hex, doc_ids_hashed=true, raw SHA-256 absent
- S6: non-search factory (from_error) → doc_ids_hashed=false, result_doc_ids=null
- S7: empty result_doc_ids list with hashing on → doc_ids_hashed=true, result_doc_ids=[]
- S8: concurrent searches → all JSONL entries consistent (N entries, all hashed)
- S9: MCP search + search_with_context tools hash correctly
- S13: determinism — same doc hashed identically across two separate requests
- S14: different docs → different hashes
- S16: explain and error entries have doc_ids_hashed=false, unaffected by hasher
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]

# Number of concurrent requests for the S8 concurrency test.
_N_CONCURRENT = 5


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read_telemetry_entries(cfg: Any) -> list[dict]:
    """Read all JSONL telemetry entries written to cfg.telemetry.log_dir."""
    log_dir = Path(cfg.telemetry.log_dir)
    entries: list[dict] = []
    if not log_dir.exists():
        return entries
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


def _ingest_and_poll(
    client: Any, col: str, file_path: Path, api_key: str, timeout_s: float = 30.0
) -> None:
    """POST /ingest via file path and poll until DONE."""
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = client.post(
        "/ingest", json={"collection": col, "path": str(file_path)}, headers=headers
    )
    assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
    job_id = resp.json()["job_id"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=headers)
        assert r.status_code == 200
        status = r.json()["status"]
        if status == "DONE":
            return
        if status == "FAILED":
            pytest.fail(f"ingest job failed: {r.json()}")
        time.sleep(0.1)
    pytest.fail(f"ingest did not complete in {timeout_s}s")


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
    """Send MCP initialize + notifications/initialized handshake; return session_id."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t1-hashing-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize response must return mcp-session-id header"
    # Send notifications/initialized (required by MCP spec before tool calls).
    notif_resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_mcp_headers(token, session_id),
    )
    assert notif_resp.status_code < 400, (
        f"notifications/initialized returned unexpected error: {notif_resp.status_code}"
    )
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
    data_lines = [
        line[5:].strip()
        for line in resp.text.split("\n")
        if line.startswith("data:")
    ]
    assert data_lines, f"No data: line in SSE response: {resp.text[:300]!r}"
    return json.loads(data_lines[-1])


# ---------------------------------------------------------------------------
# S1 — hashing disabled → raw doc_ids, doc_ids_hashed=false
# ---------------------------------------------------------------------------


def test_e2e_hashing_disabled_raw_doc_ids_in_jsonl(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Real server, hashing off: JSONL entries have raw doc_ids and doc_ids_hashed=false (S1).

    Verifies:
    - doc_ids_hashed == False in the telemetry entry.
    - The raw SHA-256 path-derived doc_id IS present in result_doc_ids.
    """
    text_file = tmp_path / "col-s1" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("hello world document for S1 hashing-disabled test")

    with make_real_app(tmp_path, monkeypatch, telemetry_enabled=True) as (
        client, cfg, api_key
    ):
        _ingest_and_poll(client, "col-s1", text_file, api_key)
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.post(
            "/search",
            json={"collection": "col-s1", "query": "hello"},
            headers=headers,
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results, "Search must return at least one result for S1 verification."

    entries = _read_telemetry_entries(cfg)
    search_ok = [
        e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert search_ok, f"No search/ok telemetry entries. All: {entries!r}"
    entry = search_ok[0]

    assert entry.get("doc_ids_hashed") is False, (
        f"Expected doc_ids_hashed=False with hashing disabled; got {entry.get('doc_ids_hashed')!r}"
    )
    raw_doc_id = _raw_doc_id(text_file)
    result_doc_ids = entry.get("result_doc_ids") or []
    assert result_doc_ids, "Expected non-empty result_doc_ids in telemetry entry (S1)."
    assert raw_doc_id in result_doc_ids, (
        f"Expected raw doc_id {raw_doc_id!r} in result_doc_ids when hashing disabled; "
        f"got: {result_doc_ids!r}"
    )


# ---------------------------------------------------------------------------
# S2 — hashing enabled → HMAC hex, raw SHA-256 absent, doc_ids_hashed=true
# ---------------------------------------------------------------------------


def test_e2e_hashing_enabled_hmac_doc_ids_in_jsonl(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Real server, hashing on: JSONL entries have 64-char HMAC hex, doc_ids_hashed=true (S2).

    Asserting "64 hex" alone is insufficient — raw SHA-256 is also 64 hex;
    the test proves that none of the raw path-derived SHA-256 values appear in
    result_doc_ids. (S2)
    """
    text_file = tmp_path / "col-s2" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("hello world document for S2 hashing-enabled test")

    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "col-s2", text_file, api_key)
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.post(
            "/search",
            json={"collection": "col-s2", "query": "hello"},
            headers=headers,
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results, "Search must return at least one result for S2 verification."

    entries = _read_telemetry_entries(cfg)
    search_ok = [
        e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert search_ok, f"No search/ok telemetry entries. All: {entries!r}"
    entry = search_ok[0]

    assert entry.get("doc_ids_hashed") is True, (
        f"Expected doc_ids_hashed=True with hashing enabled; got {entry.get('doc_ids_hashed')!r}. "
        f"Entry: {entry!r}"
    )
    raw_doc_id = _raw_doc_id(text_file)
    result_doc_ids = entry.get("result_doc_ids") or []
    assert result_doc_ids, (
        f"Expected non-empty result_doc_ids in telemetry entry (S2). Entry: {entry!r}"
    )
    for hashed_id in result_doc_ids:
        assert len(hashed_id) == 64, (
            f"Expected 64-char HMAC hex; got length {len(hashed_id)}: {hashed_id!r}"
        )
    assert raw_doc_id not in result_doc_ids, (
        f"Raw doc_id {raw_doc_id!r} must NOT appear in hashed result_doc_ids (S2). "
        "Hashing was not applied."
    )


# ---------------------------------------------------------------------------
# S6 — non-search factory (from_error) → doc_ids_hashed=false, result_doc_ids=null
# ---------------------------------------------------------------------------


def test_e2e_non_search_entry_unaffected_by_hashing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Non-search factory entries have doc_ids_hashed=false even when hashing is enabled (S6, S16).

    The S6 scenario says: when result_doc_ids is None (the model default — only from_search_tool_result
    takes a list[str]; other factories default result_doc_ids to None), doc_ids_hashed must be False.

    Trigger: POST /explain on a real collection → from_explain_result() writes a telemetry entry
    with result_doc_ids=None (explain does not populate result_doc_ids) and doc_ids_hashed=False.
    This is the reliable e2e path that proves the invariant: non-search factories are untouched
    by the hasher. Note: the collection-not-found 404 path does NOT write a telemetry entry
    (it returns before reaching the telemetry block), so explain is the correct trigger here.
    """
    text_file = tmp_path / "col-s6" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("document for S6 non-search factory telemetry test")

    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "col-s6", text_file, api_key)
        headers = {"Authorization": f"Bearer {api_key}"}
        # Trigger an explain entry: from_explain_result() produces result_doc_ids=None.
        explain_resp = client.post(
            "/explain",
            json={"collection": "col-s6", "query": "explain query for S6"},
            headers=headers,
        )
        # /explain writes a telemetry entry regardless of success/failure (both emit_ok
        # and emit_err paths write an entry). We don't assert the HTTP status code.
        _ = explain_resp.status_code

    entries = _read_telemetry_entries(cfg)
    # The explain entry has endpoint="explain" and result_doc_ids=None.
    explain_entries = [e for e in entries if e.get("endpoint") == "explain"]
    assert explain_entries, (
        f"Expected at least one explain telemetry entry (S6). All entries: {entries!r}"
    )
    for entry in explain_entries:
        assert entry.get("doc_ids_hashed") is False, (
            f"Explain entry must have doc_ids_hashed=False — from_explain_result has no hasher param (S6). "
            f"Entry: {entry!r}"
        )
        assert entry.get("result_doc_ids") is None, (
            f"Explain entry must have result_doc_ids=None — explain does not populate this field (S6). "
            f"Entry: {entry!r}"
        )


# ---------------------------------------------------------------------------
# S7 — empty result_doc_ids with hashing on → doc_ids_hashed=true
# ---------------------------------------------------------------------------


def test_e2e_empty_result_doc_ids_with_hashing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Search with hashing on → doc_ids_hashed=true for every search/ok entry (S7).

    S7 scenario: empty result_doc_ids list with hashing active → doc_ids_hashed=True.
    The core factory invariant ("hasher active → doc_ids_hashed=True even for [] list") is
    tested at unit level by test_from_search_tool_result_empty_list_with_hasher.

    At the e2e level, this test confirms the invariant holds end-to-end: with hashing on,
    every search/ok telemetry entry has doc_ids_hashed=True and result_doc_ids is a list
    (possibly empty). We ingest a document, then delete the entire collection and recreate
    it empty by ingesting then immediately searching — the post-deletion search returns 404
    because the collection no longer exists. Instead, we verify the invariant on the initial
    search entry where the collection has data.

    Note: producing an empty result_doc_ids=[] end-to-end requires infrastructure-level control
    (e.g. a collection with data in the store but no matching chunks). The factory-level empty-list
    handling is exercised by the unit test. This e2e test covers doc_ids_hashed=True correctness.
    """
    text_file = tmp_path / "col-s7" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("document for S7 hashing invariant e2e test")

    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "col-s7", text_file, api_key)
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.post(
            "/search",
            json={"collection": "col-s7", "query": "hello world"},
            headers=headers,
        )
        assert resp.status_code == 200

    entries = _read_telemetry_entries(cfg)
    search_ok = [
        e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert search_ok, f"No search/ok telemetry entries. All: {entries!r}"
    # S7 core assertion: with hashing enabled, ALL search/ok entries must have
    # doc_ids_hashed=True and result_doc_ids must be a list (possibly empty, but always a list).
    for entry in search_ok:
        assert entry.get("doc_ids_hashed") is True, (
            f"Expected doc_ids_hashed=True with hashing enabled (S7). Entry: {entry!r}"
        )
        result_doc_ids = entry.get("result_doc_ids")
        assert isinstance(result_doc_ids, list), (
            f"result_doc_ids must be a list (possibly empty) when from_search_tool_result is used. "
            f"Got: {result_doc_ids!r}"
        )
        for hashed_id in result_doc_ids:
            assert len(hashed_id) == 64, (
                f"Expected 64-char HMAC hex; got length {len(hashed_id)}: {hashed_id!r}"
            )


# ---------------------------------------------------------------------------
# S8 — concurrent searches → all entries consistent
# ---------------------------------------------------------------------------


def test_e2e_concurrent_searches_all_entries_consistent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Parallel search calls: all JSONL entries present, all doc_ids_hashed=true (S8).

    Uses httpx.AsyncClient with ASGITransport to fire N concurrent requests.
    The TelemetryWriter (asyncio.Queue put_nowait) handles concurrent enqueues safely.
    """
    text_file = tmp_path / "col-s8" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("concurrent hashing document for S8 e2e consistency test")

    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "col-s8", text_file, api_key)
        headers = {"Authorization": f"Bearer {api_key}"}
        app = client.app

        async def _fire() -> list:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as aclient:
                tasks = [
                    aclient.post(
                        "/search",
                        json={"collection": "col-s8", "query": "concurrent hashing"},
                        headers=headers,
                        timeout=30.0,
                    )
                    for _ in range(_N_CONCURRENT)
                ]
                return await asyncio.gather(*tasks)

        responses = asyncio.run(_fire())

    for i, resp in enumerate(responses):
        assert resp.status_code == 200, (
            f"Concurrent search {i} failed: {resp.status_code} {resp.text[:200]}"
        )

    entries = _read_telemetry_entries(cfg)
    search_ok = [
        e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert len(search_ok) == _N_CONCURRENT, (
        f"Expected {_N_CONCURRENT} search/ok entries; got {len(search_ok)}. "
        f"All entries: {entries!r}"
    )
    for entry in search_ok:
        assert entry.get("doc_ids_hashed") is True, (
            f"Expected doc_ids_hashed=True for all concurrent entries. Entry: {entry!r}"
        )
        for hashed_id in (entry.get("result_doc_ids") or []):
            assert len(hashed_id) == 64, (
                f"Expected 64-char HMAC hex; got length {len(hashed_id)}: {hashed_id!r}"
            )


# ---------------------------------------------------------------------------
# S9 — MCP search tool hashes doc_ids correctly
# ---------------------------------------------------------------------------


def test_e2e_mcp_search_hashes_doc_ids(tmp_path: Path, monkeypatch: Any) -> None:
    """MCP search tool via real MCP endpoint → JSONL entry has hashed doc_ids (S9)."""
    text_file = tmp_path / "col-s9-search" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("mcp search hashing e2e test document content")

    with make_real_app(
        tmp_path, monkeypatch, mcp_enabled=True, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "col-s9-search", text_file, api_key)
        session_id = _mcp_initialize(client, api_key)
        _call_mcp_tool(
            client, api_key, session_id,
            "search",
            {"query": "mcp search hashing", "collection": "col-s9-search"},
        )

    entries = _read_telemetry_entries(cfg)
    search_ok = [
        e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert search_ok, f"No search/ok entries after MCP search. All: {entries!r}"
    entry = search_ok[0]

    assert entry.get("doc_ids_hashed") is True, (
        f"Expected doc_ids_hashed=True via MCP search path (S9). Entry: {entry!r}"
    )
    raw_doc_id = _raw_doc_id(text_file)
    result_doc_ids = entry.get("result_doc_ids") or []
    assert result_doc_ids, (
        f"Expected non-empty result_doc_ids via MCP search path (S9) — "
        f"the file was ingested and the query matches its content. Entry: {entry!r}"
    )
    for hashed_id in result_doc_ids:
        assert len(hashed_id) == 64, (
            f"Expected 64-char HMAC hex via MCP; got length {len(hashed_id)}: {hashed_id!r}"
        )
    assert raw_doc_id not in result_doc_ids, (
        "Raw doc_id must not appear in hashed result_doc_ids via MCP search path (S9)."
    )


def test_e2e_mcp_search_with_context_hashes_doc_ids(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """MCP search_with_context tool via real MCP endpoint → hashed doc_ids in JSONL (S9)."""
    text_file = tmp_path / "col-s9-swc" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("mcp search_with_context hashing e2e test document content")

    with make_real_app(
        tmp_path, monkeypatch, mcp_enabled=True, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "col-s9-swc", text_file, api_key)
        session_id = _mcp_initialize(client, api_key)
        _call_mcp_tool(
            client, api_key, session_id,
            "search_with_context",
            {"query": "mcp context hashing", "collection": "col-s9-swc"},
        )

    entries = _read_telemetry_entries(cfg)
    swc_ok = [
        e for e in entries
        if e.get("endpoint") == "search_with_context" and e.get("status") == "ok"
    ]
    assert swc_ok, f"No search_with_context/ok entries after MCP call. All: {entries!r}"
    entry = swc_ok[0]

    assert entry.get("doc_ids_hashed") is True, (
        f"Expected doc_ids_hashed=True via MCP search_with_context path (S9). Entry: {entry!r}"
    )
    raw_doc_id = _raw_doc_id(text_file)
    result_doc_ids = entry.get("result_doc_ids") or []
    assert result_doc_ids, (
        f"Expected non-empty result_doc_ids via MCP search_with_context path (S9) — "
        f"the file was ingested and the query matches its content. Entry: {entry!r}"
    )
    for hashed_id in result_doc_ids:
        assert len(hashed_id) == 64, (
            f"Expected 64-char HMAC hex via MCP swc; got length {len(hashed_id)}: {hashed_id!r}"
        )
    assert raw_doc_id not in result_doc_ids, (
        "Raw doc_id must not appear in hashed result_doc_ids via search_with_context (S9)."
    )


# ---------------------------------------------------------------------------
# S13 — determinism: same doc hashed identically across two requests
# ---------------------------------------------------------------------------


def test_e2e_hash_doc_id_deterministic_across_requests(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Two searches for the same document produce the same hashed doc_id in JSONL (S13).

    Proves HMAC determinism: same salt + same input → same output across separate requests.
    """
    text_file = tmp_path / "col-s13" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("determinism test document for S13 repeated hashing")

    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "col-s13", text_file, api_key)
        headers = {"Authorization": f"Bearer {api_key}"}

        # First search
        resp1 = client.post(
            "/search",
            json={"collection": "col-s13", "query": "determinism test"},
            headers=headers,
        )
        assert resp1.status_code == 200
        assert resp1.json()["results"], "S13: first search must return results."

        # Second search for the same document
        resp2 = client.post(
            "/search",
            json={"collection": "col-s13", "query": "determinism test"},
            headers=headers,
        )
        assert resp2.status_code == 200
        assert resp2.json()["results"], "S13: second search must return results."

    entries = _read_telemetry_entries(cfg)
    search_ok = [
        e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert len(search_ok) >= 2, (
        f"Expected at least 2 search/ok entries for S13 determinism check. "
        f"Got {len(search_ok)}. All: {entries!r}"
    )

    # Extract the hashed doc_id sets for the two requests — they must be identical.
    ids_first = sorted(search_ok[0].get("result_doc_ids") or [])
    ids_second = sorted(search_ok[1].get("result_doc_ids") or [])
    assert ids_first, "S13: first entry must have non-empty result_doc_ids."
    assert ids_first == ids_second, (
        f"HMAC must be deterministic: same doc must produce same hashed doc_id "
        f"on both requests (S13). First: {ids_first!r}, Second: {ids_second!r}"
    )


# ---------------------------------------------------------------------------
# S14 — different docs → different hashes
# ---------------------------------------------------------------------------


def test_e2e_different_docs_different_hashes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Two different docs produce distinct hashed doc_ids in JSONL (S14).

    A smoke check that distinct inputs produce distinct HMAC outputs.
    """
    file_a = tmp_path / "col-s14" / "doc_a.txt"
    file_b = tmp_path / "col-s14" / "doc_b.txt"
    file_a.parent.mkdir(parents=True, exist_ok=True)
    file_a.write_text("document alpha for S14 distinct hash test")
    file_b.write_text("document beta for S14 distinct hash test")

    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "col-s14", file_a, api_key)
        _ingest_and_poll(client, "col-s14", file_b, api_key)

        headers = {"Authorization": f"Bearer {api_key}"}
        # Search specifically for each document using unique keywords.
        resp_a = client.post(
            "/search",
            json={"collection": "col-s14", "query": "document alpha S14"},
            headers=headers,
        )
        assert resp_a.status_code == 200

        resp_b = client.post(
            "/search",
            json={"collection": "col-s14", "query": "document beta S14"},
            headers=headers,
        )
        assert resp_b.status_code == 200

    entries = _read_telemetry_entries(cfg)
    search_ok = [
        e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert len(search_ok) >= 2, (
        f"Expected at least 2 search/ok entries for S14 check. Got {len(search_ok)}."
    )

    # Collect all hashed doc_ids across both entries.
    all_hashed_ids: set[str] = set()
    for entry in search_ok:
        for hid in (entry.get("result_doc_ids") or []):
            all_hashed_ids.add(hid)

    # Raw doc_ids for the two files must be distinct — prove distinct inputs.
    raw_a = _raw_doc_id(file_a)
    raw_b = _raw_doc_id(file_b)
    assert raw_a != raw_b, "The two files must have distinct raw doc_ids."

    # The hashed set must contain at least 2 distinct values (one per doc).
    # This is a smoke check, not a proof of HMAC collision resistance.
    assert len(all_hashed_ids) >= 2, (
        f"Expected at least 2 distinct hashed doc_ids for S14 (one per document). "
        f"Got {len(all_hashed_ids)} distinct values: {all_hashed_ids!r}"
    )


# ---------------------------------------------------------------------------
# S16 — explain and error entries have doc_ids_hashed=false and are unaffected by hasher
# ---------------------------------------------------------------------------


def test_e2e_explain_and_error_entries_unaffected(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Explain entries have doc_ids_hashed=false, unaffected by the hasher (S16).

    Verifies that from_explain_result and from_error factories are NOT modified by D8:
    - Explain entries (from_explain_result): doc_ids_hashed=False, result_doc_ids=None.
    - No entry from a non-search factory has doc_ids_hashed=True.

    Note on error entries: the collection-not-found path in routes_search.py returns 404
    before reaching the telemetry block, so no from_error entry is written for that trigger.
    The from_error behavior (doc_ids_hashed=False) is tested at the unit level by
    test_other_factories_have_no_doc_id_hasher_param. This e2e test focuses on explain entries,
    which reliably write telemetry via from_explain_result.
    """
    text_file = tmp_path / "col-s16" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("document for S16 explain entry check")

    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        _ingest_and_poll(client, "col-s16", text_file, api_key)
        headers = {"Authorization": f"Bearer {api_key}"}

        # Trigger an explain entry: from_explain_result() produces doc_ids_hashed=False.
        explain_resp = client.post(
            "/explain",
            json={"collection": "col-s16", "query": "explain query for S16"},
            headers=headers,
        )
        # /explain writes a telemetry entry regardless of success/failure.
        _ = explain_resp.status_code

    entries = _read_telemetry_entries(cfg)

    # Explain entries must have doc_ids_hashed=False (from_explain_result has no hasher param).
    explain_entries = [e for e in entries if e.get("endpoint") == "explain"]
    assert explain_entries, (
        f"Expected at least one explain telemetry entry (S16). All entries: {entries!r}"
    )
    for entry in explain_entries:
        assert entry.get("doc_ids_hashed") is False, (
            f"Explain entry must have doc_ids_hashed=False — from_explain_result has no hasher param (S16). "
            f"Entry: {entry!r}"
        )

    # All non-search entries in the log must have doc_ids_hashed=False (global invariant).
    for entry in entries:
        if entry.get("endpoint") in ("explain", "route"):
            assert entry.get("doc_ids_hashed") is False, (
                f"Entry for endpoint={entry.get('endpoint')!r} must have doc_ids_hashed=False (S16). "
                f"Entry: {entry!r}"
            )
        # Even if an error entry exists (status != "ok"), it must not have doc_ids_hashed=True.
        if entry.get("status") != "ok":
            assert entry.get("doc_ids_hashed") is False, (
                f"Non-ok entry must have doc_ids_hashed=False (S16). Entry: {entry!r}"
            )


# ---------------------------------------------------------------------------
# Toggle continuity: log segment boundary visible via doc_ids_hashed field
# ---------------------------------------------------------------------------


def test_e2e_toggle_continuity_visible_in_jsonl(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Hashing off then on: JSONL contains entries with both doc_ids_hashed=false and =true.

    Simulates the toggle-transition gap (Known Limitations): the doc_ids_hashed field
    lets log consumers detect the boundary between hashing-off and hashing-on segments.

    Implementation: run two separate app sessions against the same tmp_path (same log_dir),
    one with hashing off and one with hashing on, then verify the JSONL contains both values.
    """
    text_file = tmp_path / "col-toggle" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("toggle continuity test document for hashing boundary check")

    # Session 1: hashing OFF
    with make_real_app(tmp_path, monkeypatch, telemetry_enabled=True) as (
        client1, cfg, api_key1
    ):
        _ingest_and_poll(client1, "col-toggle", text_file, api_key1)
        headers1 = {"Authorization": f"Bearer {api_key1}"}
        resp1 = client1.post(
            "/search",
            json={"collection": "col-toggle", "query": "toggle"},
            headers=headers1,
        )
        assert resp1.status_code == 200
        assert resp1.json()["results"], "Toggle session 1: must return results."

    # Session 2: hashing ON (same tmp_path → same log_dir → JSONL accumulates)
    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client2, cfg2, api_key2):
        headers2 = {"Authorization": f"Bearer {api_key2}"}
        resp2 = client2.post(
            "/search",
            json={"collection": "col-toggle", "query": "toggle"},
            headers=headers2,
        )
        assert resp2.status_code == 200
        assert resp2.json()["results"], "Toggle session 2: must return results."

    # cfg and cfg2 should have the same log_dir — use cfg2 to read.
    entries = _read_telemetry_entries(cfg2)
    search_ok = [
        e for e in entries if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert len(search_ok) >= 2, (
        f"Expected at least 2 search/ok entries (one from each session). "
        f"Got {len(search_ok)}. All: {entries!r}"
    )

    hashed_values = {e.get("doc_ids_hashed") for e in search_ok}
    assert False in hashed_values, (
        "Expected at least one entry with doc_ids_hashed=False (hashing-off session). "
        f"Values seen: {hashed_values!r}"
    )
    assert True in hashed_values, (
        "Expected at least one entry with doc_ids_hashed=True (hashing-on session). "
        f"Values seen: {hashed_values!r}"
    )
