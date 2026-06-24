"""D9 / T-3 — Namespace data-isolation e2e: valid cross-namespace token cannot see wrong namespace's data.

Tests:
- ``test_mcp_namespace_data_isolation``: ingest doc-A under ns-a AND doc-B under ns-b; with
  the ns-a token, MCP ``search`` finds doc-A and does NOT find doc-B; with the ns-b token,
  ``search`` finds doc-B and does NOT find doc-A. Proves bidirectional data isolation.

The test is NOT a vacuous pass against an empty namespace, and is NOT just auth rejection —
it injects real data into both namespaces and verifies search results are correctly scoped.

Scenarios completed: S8 (namespace propagation — e2e isolation proof).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers
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
    """Send the MCP initialize handshake; return session_id."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t3-isolation-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, (
        f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    )
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize must return mcp-session-id header"
    # Fire-and-forget notifications/initialized
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_mcp_headers(token, session_id),
    )
    return session_id


def _mcp_call_tool(client, token: str, session_id: str, tool_name: str, arguments: dict, request_id: int = 2) -> dict:
    """Call an MCP tool and return the parsed SSE result dict (last data: line)."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
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
    if not data_lines:
        raise AssertionError(
            f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
        )
    return json.loads(data_lines[-1])


# ---------------------------------------------------------------------------
# Store injection helper — bypasses HTTP to set namespace on injected data
# ---------------------------------------------------------------------------

_EMBEDDING_DIM = 384  # must match the stub fastembed TextEmbedding (384-dim zeros)


def _inject_namespace_data(client, col: str, namespace: str, doc_text: str) -> None:
    """Inject a document under a specific namespace directly via the store's async API.

    Bypasses the HTTP ingest route so namespace can be set explicitly without
    needing a token scoped to that namespace during setup.

    Both a chunk table and a CollectionMeta row are created so that
    pipeline.get_all_collections_meta(namespace) and pipeline.search(namespace=...) work.

    NOTE: ingest_chunks accepts a namespace kwarg but the namespace setter for CollectionMeta
    is update_collection_meta — do not rely on ingest_chunks' namespace kwarg alone for the
    meta row, as the meta write path goes through a separate code path.
    """
    import hashlib
    from datetime import datetime, timezone

    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.collection_meta import CollectionMeta

    store = client.app.state.search_store

    source_path = f"/data/{namespace}/{col}/doc.txt"
    doc_id = hashlib.sha256(source_path.encode()).hexdigest()
    chunk = ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text=doc_text,
        vector=[0.0] * _EMBEDDING_DIM,
        source_path=source_path,
        indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
        acl=None,
    )
    meta = CollectionMeta(
        name=col,
        active_embedding_model="BAAI/bge-small-en-v1.5",
        doc_count=1,
        chunk_count=1,
        namespace=namespace,
    )

    async def _setup():
        await store.ensure_collection(col, _EMBEDDING_DIM)
        await store.ingest_chunks(col, [chunk], namespace=namespace)
        await store.rebuild_fts_index(col)
        await store.update_collection_meta(meta)

    # Safe in Python 3.12+: TestClient drives the ASGI lifespan from a background
    # thread; the main test thread has no running event loop. asyncio.Lock is not
    # loop-bound in Python 3.12 (per project learnings). Would break under async
    # fixtures or anyio-based test runners — do not add @pytest.mark.asyncio.
    asyncio.run(_setup())


# ---------------------------------------------------------------------------
# T-3: bidirectional namespace data isolation
# ---------------------------------------------------------------------------


def _assert_namespace_stored(client, col: str, expected_namespace: str, other_namespace: str) -> None:
    """Assert that the collection's stored namespace matches expected_namespace.

    Setup validation helper (not an isolation proof itself — the MCP Proofs 1-4 are
    the load-bearing assertions). Makes three independent checks:

    1. get_collection_meta(col, expected_namespace) → non-None: collection exists under correct ns.
    2. get_collection_meta(col, other_namespace) → None: collection NOT visible under the other
       real namespace used in this test. Proves cross-namespace metadata gating between two
       real namespaces, not just against a non-existent sentinel.
    3. get_collection_meta(col, "wrong-sentinel-xyz") → None: collection NOT visible under an
       entirely non-existent namespace (defense-in-depth).
    """
    store = client.app.state.search_store

    async def _read_all():
        correct = await store.get_collection_meta(col, expected_namespace)
        other = await store.get_collection_meta(col, other_namespace)
        sentinel = await store.get_collection_meta(col, "wrong-sentinel-xyz")
        return correct, other, sentinel

    # Single asyncio.run() call for all three reads — avoids repeated event-loop
    # creation against the same LanceDB AsyncConnection (safer per project learnings).
    meta, other_meta, sentinel_meta = asyncio.run(_read_all())
    assert meta is not None, (
        f"Collection '{col}' has no metadata after injection — _inject_namespace_data failed silently."
    )
    assert other_meta is None, (
        f"Collection '{col}' is visible under OTHER namespace {other_namespace!r} — "
        f"namespace scoping is broken at the store layer. get_collection_meta must return None "
        f"when queried with a namespace that does not own the collection."
    )
    assert sentinel_meta is None, (
        f"Collection '{col}' is visible under non-existent sentinel namespace 'wrong-sentinel-xyz' — "
        f"get_collection_meta is not filtering by namespace at all."
    )


def _assert_cross_namespace_blocked(result: dict, requester_label: str, target_col: str) -> None:
    """Assert that a cross-namespace MCP search result correctly blocks access.

    Accepts either:
    - A graceful not_found error with code == "not_found" (the expected namespace-gate behavior)
    - An empty results list (acceptable fallback)

    Rejects:
    - A server crash (isError=True without code == "not_found")
    - Non-empty results (data leaked across namespace boundary)
    - Unexpected payload shapes
    """
    rpc_result = result.get("result", {})
    content = rpc_result.get("content", [])

    if rpc_result.get("isError", False):
        # Envelope-level error: must be a graceful not_found, not a server crash.
        assert content, (
            f"{requester_label} got isError=True on {target_col!r} with no content — "
            f"likely a server crash, not a namespace gate. Full result: {result!r}"
        )
        text = content[0].get("text", "")
        try:
            parsed = json.loads(text) if text else {}
        except (json.JSONDecodeError, ValueError):
            pytest.fail(
                f"Cross-namespace search isError=True but content is non-JSON — looks like a crash. "
                f"text={text!r}"
            )
        assert isinstance(parsed, dict) and parsed.get("code") == "not_found", (
            f"{requester_label} got isError=True on {target_col!r} but error code is not 'not_found' — "
            f"this may be a server crash, not a namespace gate. parsed={parsed!r}"
        )
    else:
        # No envelope error: content must parse to empty results or a not_found code.
        assert content, (
            f"{requester_label} searching {target_col!r} returned no error and no content — unexpected. "
            f"Full result: {result!r}"
        )
        text = content[0].get("text", "")
        try:
            parsed = json.loads(text) if text else {}
        except (json.JSONDecodeError, ValueError):
            pytest.fail(
                f"Cross-namespace search returned non-JSON content — cannot verify isolation. "
                f"text={text!r}"
            )
        assert isinstance(parsed, dict), (
            f"Cross-namespace search content is not a JSON object: {parsed!r}"
        )
        if "code" in parsed:
            assert parsed["code"] == "not_found", (
                f"{requester_label} got error code {parsed['code']!r} on {target_col!r} — "
                f"expected 'not_found'. Namespace data isolation broken or wrong error surfaced. "
                f"parsed={parsed!r}"
            )
        elif "results" in parsed:
            assert isinstance(parsed["results"], list) and len(parsed["results"]) == 0, (
                f"{requester_label} MUST NOT see {target_col!r} (different namespace). "
                f"Got non-empty results: {parsed['results']!r}. Namespace data isolation broken."
            )
        else:
            pytest.fail(
                f"Cross-namespace search returned a JSON object with neither 'code' nor 'results' — "
                f"unexpected payload format. parsed={parsed!r}. Full result: {result!r}"
            )


def _assert_own_namespace_accessible(result: dict, requester_label: str, own_col: str, phrase: str) -> None:
    """Assert that a same-namespace MCP search result correctly finds the expected document.

    Verifies:
    - No isError at envelope level (namespace gate must not block own-namespace access)
    - Content is non-empty
    - Content parses to a results dict with at least one hit
    - The expected phrase appears in at least one result's text
    """
    rpc_result = result.get("result", {})
    assert not rpc_result.get("isError"), (
        f"{requester_label} searching {own_col!r} (own namespace) returned isError=True: {result!r}. "
        "A token must have access to collections in its own namespace."
    )
    content = rpc_result.get("content", [])
    assert content, (
        f"{requester_label} search on {own_col!r} returned empty content: {result!r}"
    )
    try:
        parsed = json.loads(content[0].get("text", "{}"))
    except (json.JSONDecodeError, ValueError):
        pytest.fail(
            f"{requester_label} search on {own_col!r} returned non-JSON content — cannot verify isolation. "
            f"text={content[0].get('text', '')!r}"
        )
    assert isinstance(parsed, dict) and "results" in parsed, (
        f"Expected results dict from {own_col!r} search, got: {parsed!r}"
    )
    own_results = parsed["results"]
    assert isinstance(own_results, list) and len(own_results) > 0, (
        f"{requester_label} must find results in {own_col!r} (own namespace). "
        f"Got: {own_results!r}. This is the positive half of the isolation proof."
    )
    assert any(phrase in r.get("text", "") for r in own_results), (
        f"{requester_label} results for {own_col!r} must contain phrase={phrase!r} but none did. "
        f"Results: {[r.get('text', '')[:80] for r in own_results]!r}"
    )


def test_mcp_namespace_data_isolation(tmp_path, monkeypatch) -> None:
    """Bidirectional namespace data isolation via MCP search.

    Setup:
    - doc-A ingested into collection "t3-col-a" under namespace "t3-ns-a".
      doc-A contains the distinctive phrase "helical quantum flux vortex".
    - doc-B ingested into collection "t3-col-b" under namespace "t3-ns-b".
      doc-B contains the distinctive phrase "luminous stellar protoplanetary disk".
    - A managed key (ns-a-token) is scoped to t3-ns-a.
    - A managed key (ns-b-token) is scoped to t3-ns-b.

    Proof:
    1. ns-a-token + search("helical quantum flux vortex", collection=t3-col-a)
       → finds doc-A (ns-a has access to its own collection).
    2. ns-a-token + search("luminous stellar protoplanetary disk", collection=t3-col-b)
       → returns not_found error or empty results (ns-a cannot see t3-col-b in ns-b).
    3. ns-b-token + search("luminous stellar protoplanetary disk", collection=t3-col-b)
       → finds doc-B (ns-b has access to its own collection).
    4. ns-b-token + search("helical quantum flux vortex", collection=t3-col-a)
       → returns not_found error or empty results (ns-b cannot see t3-col-a in ns-a).

    Steps 1+2 together prove ns-a is properly scoped (not a vacuous check).
    Steps 3+4 together prove ns-b is properly scoped (bidirectional).

    Scenarios completed: S8 (namespace propagation — e2e isolation proof).
    """
    phrase_a = "helical quantum flux vortex"
    phrase_b = "luminous stellar protoplanetary disk"
    col_a = "t3-col-a"
    col_b = "t3-col-b"
    ns_a = "t3-ns-a"
    ns_b = "t3-ns-b"

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        # --- Inject test data into both namespaces via the store layer directly ---
        _inject_namespace_data(client, col_a, ns_a, f"{phrase_a} document in namespace a")
        _inject_namespace_data(client, col_b, ns_b, f"{phrase_b} document in namespace b")
        # Setup validation: each collection visible under its own ns, invisible under the other.
        _assert_namespace_stored(client, col_a, ns_a, other_namespace=ns_b)
        _assert_namespace_stored(client, col_b, ns_b, other_namespace=ns_a)

        # --- Create managed keys scoped to each namespace via REST admin API ---
        resp = client.post(
            "/keys",
            json={"namespace": ns_a, "label": "t3-ns-a-key"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201, (
            f"POST /keys for ns-a failed: {resp.status_code} {resp.json()}"
        )
        ns_a_token = resp.json()["token"]

        resp = client.post(
            "/keys",
            json={"namespace": ns_b, "label": "t3-ns-b-key"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201, (
            f"POST /keys for ns-b failed: {resp.status_code} {resp.json()}"
        )
        ns_b_token = resp.json()["token"]

        # -----------------------------------------------------------------------
        # Proof 1: ns-a-token sees its own collection (col-a, ns-a)
        # -----------------------------------------------------------------------
        session_a = _mcp_initialize(client, ns_a_token)

        result = _mcp_call_tool(client, ns_a_token, session_a, "search", {
            "query": phrase_a,
            "collection": col_a,
        }, request_id=10)
        _assert_own_namespace_accessible(
            result, requester_label="ns-a-token", own_col=col_a, phrase=phrase_a
        )

        # -----------------------------------------------------------------------
        # Proof 2: ns-a-token CANNOT see col-b (ns-b namespace)
        # -----------------------------------------------------------------------
        result = _mcp_call_tool(client, ns_a_token, session_a, "search", {
            "query": phrase_b,
            "collection": col_b,
        }, request_id=11)
        _assert_cross_namespace_blocked(result, requester_label="ns-a-token", target_col=col_b)

        # -----------------------------------------------------------------------
        # Proof 3: ns-b-token sees its own collection (col-b, ns-b)
        # -----------------------------------------------------------------------
        session_b = _mcp_initialize(client, ns_b_token)

        result = _mcp_call_tool(client, ns_b_token, session_b, "search", {
            "query": phrase_b,
            "collection": col_b,
        }, request_id=20)
        _assert_own_namespace_accessible(
            result, requester_label="ns-b-token", own_col=col_b, phrase=phrase_b
        )

        # -----------------------------------------------------------------------
        # Proof 4: ns-b-token CANNOT see col-a (ns-a namespace)
        # -----------------------------------------------------------------------
        result = _mcp_call_tool(client, ns_b_token, session_b, "search", {
            "query": phrase_a,
            "collection": col_a,
        }, request_id=21)
        _assert_cross_namespace_blocked(result, requester_label="ns-b-token", target_col=col_a)
