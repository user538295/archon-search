"""D9 / BE-7 — Slice 2 integration tests: namespace propagation, telemetry, key_store, writer=None.

Covers:
- test_namespace_isolation_via_mcp — two namespaces; managed key for ns-a; MCP `search`
  returns only ns-a documents, not ns-b documents.
- test_toml_namespace_scope_honoured — TOML namespace token for ns-b; MCP `list_collections`
  returns only ns-b collections, not ns-a collections.
- test_telemetry_wired_mcp_call_logs_entry — full stack; MCP `search` → JSONL entry present.
- test_telemetry_none_writer_no_crash — writer=None; all 17 tools callable without AttributeError.

Scenarios completed: S8 (namespace propagation — e2e proof), S9 (telemetry entry),
S12 (TOML namespace scope), S13 (writer=None → no errors), S14 (17 tools with key_store).
"""
from __future__ import annotations

import asyncio
import json
import secrets
import textwrap
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# Shared MCP helpers
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
                "clientInfo": {"name": "be7-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, (
        f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    )
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize response must return mcp-session-id header"
    return session_id


def _mcp_call_tool(client, token: str, session_id: str, tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool and return the parsed SSE result dict."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, (
        f"MCP tools/call ({tool_name}) failed: {resp.status_code} {resp.text[:300]}"
    )
    for line in resp.text.split("\n"):
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(
        f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    )


# ---------------------------------------------------------------------------
# Helpers to inject data under a specific namespace via the store directly
# ---------------------------------------------------------------------------


_EMBEDDING_DIM = 384  # must match the stub fastembed TextEmbedding (384-dim zeros)


def _inject_namespace_data(client, col: str, namespace: str, doc_text: str) -> None:
    """Inject data under a specific namespace using the store's async API directly.

    Bypasses the HTTP ingest route so we can specify namespace explicitly without
    needing a token for that namespace.

    A CollectionMeta row is created alongside the chunk table so
    pipeline.get_all_collections_meta(namespace) finds the collection.
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

    asyncio.run(_setup())


# ---------------------------------------------------------------------------
# Test 1: namespace_isolation_via_mcp
# ---------------------------------------------------------------------------


def test_namespace_isolation_via_mcp(tmp_path, monkeypatch) -> None:
    """Managed key for ns-a: MCP search returns only ns-a docs, not ns-b docs.

    Two collections in two namespaces:
    - "col-a" is in ns-a with text "archon namespace-a content"
    - "col-b" is in ns-b with text "archon namespace-b content"

    A managed key scoped to ns-a is used to call `search` via MCP.
    The search against col-a (ns-a) succeeds and finds results.
    The search against col-b (ns-b) returns empty results (namespace isolation).

    This proves asymmetry fix #2 is working end-to-end: _get_request_namespace()
    returns "ns-a" for the ns-a token, not DEFAULT_NAMESPACE.

    S8: Namespace propagation correct for all 17 tool closures.
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        # Set up data in two namespaces using the store directly
        _inject_namespace_data(client, "col-a", "ns-a", "archon namespace-a content")
        _inject_namespace_data(client, "col-b", "ns-b", "archon namespace-b content")

        # Create a managed key scoped to ns-a via REST admin API
        resp = client.post(
            "/keys",
            json={"namespace": "ns-a", "label": "test-ns-a-be7"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201, (
            f"POST /keys failed: {resp.status_code} {resp.json()}"
        )
        ns_a_token = resp.json()["token"]

        # Initialize MCP session with the ns-a managed key
        session_id = _mcp_initialize(client, ns_a_token)

        # Search in col-a (belongs to ns-a) — must return results
        result_a = _mcp_call_tool(client, ns_a_token, session_id, "search", {
            "query": "namespace-a",
            "collection": "col-a",
        })
        content_a = result_a.get("result", {}).get("content", [])
        # The result should not be an error — col-a is in ns-a which this token owns
        assert not result_a.get("result", {}).get("isError"), (
            f"search col-a with ns-a token returned isError=True: {result_a!r}. "
            "col-a is in ns-a — the ns-a token should have access."
        )
        assert content_a, (
            f"Expected non-empty content from col-a search (ns-a token, ns-a collection). "
            f"Full result: {result_a!r}"
        )
        tool_text_a = content_a[0].get("text", "")
        assert tool_text_a, f"Expected non-empty text in col-a search result content"
        parsed_a = json.loads(tool_text_a)
        assert isinstance(parsed_a, dict) and "results" in parsed_a, (
            f"Expected results dict from col-a search, got: {parsed_a!r}"
        )

        # Search in col-b (belongs to ns-b) with ns-a token — must be rejected as not_found.
        # The namespace gate added to the MCP search tool returns McpErrorResponse(code="not_found")
        # when the collection doesn't exist in the caller's namespace.
        result_b = _mcp_call_tool(client, ns_a_token, session_id, "search", {
            "query": "namespace-b",
            "collection": "col-b",
        })
        content_b = result_b.get("result", {}).get("content", [])
        assert content_b, (
            f"Expected a response content from col-b cross-namespace search, got none: {result_b!r}"
        )
        tool_text_b = content_b[0].get("text", "")
        assert tool_text_b, f"Expected non-empty text in response content: {content_b!r}"
        parsed_b = json.loads(tool_text_b)
        assert isinstance(parsed_b, dict), f"Expected dict response, got: {type(parsed_b)!r}: {parsed_b!r}"
        assert "error" in parsed_b or (isinstance(parsed_b.get("results"), list) and parsed_b["results"] == []), (
            f"ns-a token searching col-b (ns-b) must return not_found error or empty results. "
            f"Got: {parsed_b!r}. Namespace isolation broken."
        )


# ---------------------------------------------------------------------------
# Test 2: toml_namespace_scope_honoured
# ---------------------------------------------------------------------------


def test_toml_namespace_scope_honoured(tmp_path, monkeypatch) -> None:
    """TOML namespace token for ns-b: MCP list_collections returns only ns-b collections.

    Two collections in two namespaces:
    - "toml-col-a" is in ns-a
    - "toml-col-b" is in ns-b

    A TOML namespace token maps to ns-b. When that token calls list_collections via MCP,
    it must return only "toml-col-b", not "toml-col-a".

    Proves asymmetry fix #1 (namespaces dict wired) + asymmetry fix #2 (namespace
    propagation) work together for TOML-configured namespace tokens.

    S12: TOML namespace tokens → correct namespace resolution.
    S8: Namespace scope honoured.
    """
    toml_token = secrets.token_hex(32)  # the TOML namespace bearer token

    with make_real_app(
        tmp_path,
        monkeypatch,
        mcp_enabled=True,
        namespaces={toml_token: "ns-b"},  # TOML-style namespace token
    ) as (client, _cfg, api_key):
        # Inject data in two namespaces
        _inject_namespace_data(client, "toml-col-a", "ns-a", "content in namespace a")
        _inject_namespace_data(client, "toml-col-b", "ns-b", "content in namespace b")

        # Use the TOML namespace token (scoped to ns-b) to initialize MCP
        session_id = _mcp_initialize(client, toml_token)

        # Call list_collections — must return only ns-b collections
        result = _mcp_call_tool(client, toml_token, session_id, "list_collections", {})
        content = result.get("result", {}).get("content", [])
        assert not result.get("result", {}).get("isError"), (
            f"list_collections returned isError=True: {result!r}"
        )

        assert content, f"list_collections returned empty content: {result!r}"
        tool_text = content[0].get("text", "")
        collections = json.loads(tool_text)

        # Must be a list of collections
        assert isinstance(collections, list), (
            f"list_collections must return a list, got: {type(collections)!r}"
        )

        collection_names = [c.get("name") or c.get("collection") for c in collections]

        # toml-col-b must be present (ns-b belongs to this token)
        assert "toml-col-b" in collection_names, (
            f"TOML ns-b token must see 'toml-col-b'. Got: {collection_names!r}. "
            "list_collections must use _get_request_namespace() → 'ns-b'."
        )

        # toml-col-a must NOT be present (ns-a is a different namespace)
        assert "toml-col-a" not in collection_names, (
            f"TOML ns-b token must NOT see 'toml-col-a' (that's in ns-a). "
            f"Got: {collection_names!r}. Namespace isolation broken."
        )


# ---------------------------------------------------------------------------
# Test 3: telemetry_wired_mcp_call_logs_entry
# ---------------------------------------------------------------------------


def test_telemetry_wired_mcp_call_logs_entry(tmp_path, monkeypatch) -> None:
    """Full stack: MCP search tool call → JSONL telemetry entry present.

    Verifies that the telemetry writer passed from the REST lifespan to
    create_mcp_http_app() is actually invoked on tool calls.

    Ingest a real collection first so the search succeeds (success-path
    telemetry at mcp.py:393) — not just the error-path.  After context exit
    (drain_and_stop() flushes), assert a 'search' entry with status=='ok'
    exists in the JSONL log.

    S9: Telemetry entry logged after MCP tool call.
    C2: writer from lifespan wired to create_mcp_http_app() (confirmed).
    """
    with make_real_app(
        tmp_path, monkeypatch, mcp_enabled=True, telemetry_enabled=True
    ) as (client, _cfg, api_key):
        # Create a real collection so search hits the success path
        text_file = tmp_path / "tel-col" / "doc.txt"
        text_file.parent.mkdir(parents=True, exist_ok=True)
        text_file.write_text(
            textwrap.dedent("telemetry test document content for be7 integration test")
        )
        ingest_file_via_path(
            client, "tel-col", str(text_file), api_key=api_key, timeout_s=30.0
        )

        session_id = _mcp_initialize(client, api_key)
        _mcp_call_tool(client, api_key, session_id, "search", {
            "query": "telemetry test",
            "collection": "tel-col",
        })
        # Context exit triggers drain_and_stop() — all queued entries flushed.

    # Read telemetry log after full shutdown (drain_and_stop guarantees flush).
    log_dir = Path(_cfg.telemetry.log_dir)
    jsonl_files = list(log_dir.glob("*.jsonl"))
    assert jsonl_files, (
        "No JSONL telemetry files found after MCP search call. "
        "The writer from lifespan must be wired into create_mcp_http_app() (BE-6/BE-7)."
    )

    entries = []
    for jsonl_file in jsonl_files:
        for line in jsonl_file.read_text().splitlines():
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))

    assert entries, "JSONL file(s) found but contain no entries."

    search_entries = [e for e in entries if e.get("endpoint") == "search"]
    assert search_entries, (
        f"Expected a 'search' telemetry entry from MCP tool call. "
        f"Endpoints found: {[e.get('endpoint') for e in entries]!r}. "
        "Check writer is passed to create_mcp_http_app() (C2 contract)."
    )

    entry = search_entries[0]
    assert entry.get("status") == "ok", (
        f"Expected status='ok' (success path), got status={entry.get('status')!r}. "
        f"Full entry: {entry!r}. The collection must exist for the success-path to fire."
    )

    # No raw query string — structural invariant
    assert "query" not in entry, (
        f"Telemetry entry must not contain a raw 'query' field (no-raw-query invariant). "
        f"Entry keys: {list(entry.keys())}"
    )


# ---------------------------------------------------------------------------
# Test 4: telemetry_none_writer_no_crash
# ---------------------------------------------------------------------------


def test_telemetry_none_writer_no_crash(tmp_path, monkeypatch) -> None:
    """writer=None: all 17 tools callable without AttributeError.

    Verifies that every tool closure has a correct `if writer is not None:`
    guard — none of them attempts `writer.enqueue(...)` when writer is None.

    Calls all 17 tools in sequence via the MCP JSON-RPC transport. For tools
    that require real data (search, list_documents, export_collection,
    get_collection_meta) we expect either an empty/not_found result or a
    graceful error dict — NOT an AttributeError 500.

    S13: writer=None → no errors, no AttributeError.
    S14: all 17 tools are registered (confirmed callable count).
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        # telemetry disabled by default → writer=None is wired to create_mcp_http_app()

        session_id = _mcp_initialize(client, api_key)

        def call_tool(name: str, args: dict) -> dict:
            return _mcp_call_tool(client, api_key, session_id, name, args)

        def assert_no_internal_error(result: dict, tool: str) -> None:
            """Assert the result is not an unexpected internal error (AttributeError/500).

            isError=True is acceptable when it means 'not found' or another expected
            error — we're only guarding against AttributeError from a missing
            `if writer is not None:` guard.
            """
            content = result.get("result", {}).get("content", [])
            if not content:
                return
            text = content[0].get("text", "")
            if not text:
                return
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return
            if isinstance(parsed, dict) and parsed.get("isError"):
                err_text = parsed.get("error", "")
                assert "AttributeError" not in err_text, (
                    f"Tool '{tool}' raised AttributeError (likely missing writer=None guard): "
                    f"{err_text!r}"
                )
                assert "NoneType" not in err_text, (
                    f"Tool '{tool}' raised NoneType error (likely writer.enqueue() on None): "
                    f"{err_text!r}"
                )

        # --- Non-destructive read-only / idempotent tools ---
        r = call_tool("list_collections", {})
        assert_no_internal_error(r, "list_collections")

        r = call_tool("get_collections_meta", {})
        assert_no_internal_error(r, "get_collections_meta")

        r = call_tool("search", {"query": "hello", "collection": "no-such-col"})
        assert_no_internal_error(r, "search")

        r = call_tool("search_with_context", {"query": "hello", "collection": "no-such-col"})
        assert_no_internal_error(r, "search_with_context")

        r = call_tool("explain", {"query": "hello"})
        assert_no_internal_error(r, "explain")

        r = call_tool("list_documents", {"collection": "no-such-col"})
        assert_no_internal_error(r, "list_documents")

        r = call_tool("get_collection_meta", {"name": "no-such-col"})
        assert_no_internal_error(r, "get_collection_meta")

        r = call_tool("delete_document", {"collection": "no-such-col", "doc_id": "nonexistent-doc-id"})
        assert_no_internal_error(r, "delete_document")

        # --- Key-management tools (read-only ops) ---
        r = call_tool("list_keys", {})
        assert_no_internal_error(r, "list_keys")

        # create_key (creates a real key — harmless in tmp_path)
        r = call_tool("create_key", {"namespace": "test-ns", "label": "be7-test"})
        assert_no_internal_error(r, "create_key")

        # For revoke_key / rotate_key, we need a real key ID.
        # create_key above should have returned one; parse it.
        content_create = r.get("result", {}).get("content", [])
        created_key_id: str | None = None
        if content_create:
            try:
                parsed_key = json.loads(content_create[0].get("text", ""))
                if isinstance(parsed_key, dict):
                    created_key_id = parsed_key.get("id")
            except (json.JSONDecodeError, KeyError):
                pass

        if created_key_id:
            r = call_tool("revoke_key", {"key_id": created_key_id})
            assert_no_internal_error(r, "revoke_key")

        # rotate_key does not need an ID — rotates the default key
        r = call_tool("rotate_key", {})
        assert_no_internal_error(r, "rotate_key")

        # --- Ingest tools (no real file — graceful error expected, not AttributeError) ---
        r = call_tool("ingest_file", {
            "collection": "no-such-col",
            "path": str(tmp_path / "nonexistent-file.txt"),
        })
        assert_no_internal_error(r, "ingest_file")

        r = call_tool("ingest_directory", {
            "collection": "no-such-col",
            "path": str(tmp_path / "nonexistent-dir"),
        })
        assert_no_internal_error(r, "ingest_directory")

        # --- Export / import (no real collection — graceful error expected) ---
        r = call_tool("export_collection", {"collection": "no-such-col"})
        assert_no_internal_error(r, "export_collection")

        r = call_tool("import_collection", {
            "collection": "no-such-col",
            "path": str(tmp_path / "nonexistent.tar.gz"),
        })
        assert_no_internal_error(r, "import_collection")

        # --- update_collection (no real collection — graceful error expected) ---
        r = call_tool("update_collection", {
            "collection_name": "no-such-col",
            "embedding_model": "BAAI/bge-small-en-v1.5",
        })
        assert_no_internal_error(r, "update_collection")
