"""D9 / T-1 — 17 MCP tool smoke e2e tests (one per tool; shape-valid response).

Each test calls exactly one MCP tool via full JSON-RPC transport and asserts:
- The transport returns HTTP 200 with a data: SSE line.
- The response content is non-empty (tool executed, not a silent 500).
- The response is either a schema-valid success dict OR a graceful error dict —
  never an AttributeError / NoneType crash.

Destructive tools (delete_document, revoke_key) use a dedicated smoke namespace
``mcp-smoke-{uuid}`` so they cannot corrupt data used by T-2 / T-3 / T-4.

Scenario completed: S3 (each of the 17 tools responds with a non-empty, schema-valid result).
"""
from __future__ import annotations

import json
import textwrap
import uuid
from typing import Any

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers (shared across all smoke tests in this module)
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
                "clientInfo": {"name": "smoke-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, (
        f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    )
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize must return mcp-session-id header"
    # Send notifications/initialized (fire-and-forget; 200/202/204 all valid)
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
    for line in resp.text.split("\n"):
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(
        f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    )


def _assert_tool_response_valid(result: dict, tool_name: str) -> Any | None:
    """Assert the tool response is non-empty and has no AttributeError crash.

    A graceful error dict (code, error fields) is valid — only AttributeError
    and NoneType crashes indicate a missing writer=None guard or programming bug.

    Checks both the JSON-RPC envelope-level ``isError`` flag (set by FastMCP when
    a tool raises an unhandled exception) and the content-level crash strings.

    Returns the parsed JSON payload (dict or list) or None for non-JSON text.
    """
    assert result, f"Tool '{tool_name}' returned empty result dict"
    # The result must have a 'result' key from the JSON-RPC envelope
    rpc_result = result.get("result")
    assert rpc_result is not None, (
        f"Tool '{tool_name}' RPC result missing 'result' key: {result!r}"
    )
    # Envelope-level isError: set by FastMCP when a tool raises an unhandled exception.
    # This is distinct from the content-level error dict returned by graceful tool error paths.
    assert not rpc_result.get("isError"), (
        f"Tool '{tool_name}' returned envelope-level isError=True (unhandled exception): {rpc_result!r}"
    )
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content list: {rpc_result!r}"
    first_item = content[0]
    text = first_item.get("text", "")
    assert text, f"Tool '{tool_name}' returned empty text in first content item: {content!r}"

    # Parse and check for AttributeError / NoneType crashes in the tool's text response
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError:
        # Non-JSON text is acceptable (some tools return plain text)
        return None

    if isinstance(parsed, dict):
        # Check for crash strings serialized into the tool's error response.
        # Use str() to handle both None and actual error strings (job_to_dict sets error=None).
        err_text = str(parsed.get("error") or "")
        assert "AttributeError" not in err_text, (
            f"Tool '{tool_name}' raised AttributeError: {err_text!r}"
        )
        assert "NoneType" not in err_text, (
            f"Tool '{tool_name}' raised NoneType error: {err_text!r}"
        )

    return parsed


# ---------------------------------------------------------------------------
# Fixture: real app with MCP + a pre-ingested collection for read-heavy tools
# ---------------------------------------------------------------------------

@pytest.fixture()
def smoke_env(tmp_path, monkeypatch):
    """Yield (client, cfg, api_key, session_id, smoke_col) with one ingested collection.

    The ingested collection ``smoke_col`` is in the default namespace and has
    one real document, so search/search_with_context/explain/list_documents/
    get_collection_meta all hit the success path (not the not_found path).
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, cfg, api_key):
        smoke_col = "mcp-smoke-base"
        doc_file = tmp_path / "smoke_docs" / "doc.txt"
        doc_file.parent.mkdir(parents=True, exist_ok=True)
        doc_file.write_text(
            textwrap.dedent("""\
                Archon Search MCP smoke test document.
                This document is used to verify that MCP tools work correctly.
                Retrieval augmented generation system powered by LanceDB.
            """)
        )
        ingest_file_via_path(client, smoke_col, str(doc_file), api_key=api_key, timeout_s=30.0)
        session_id = _mcp_initialize(client, api_key)
        yield client, cfg, api_key, session_id, smoke_col


# ---------------------------------------------------------------------------
# Smoke test 1: search
# ---------------------------------------------------------------------------


def test_mcp_smoke_search(smoke_env) -> None:
    """calls search, response non-empty, schema-valid (S3)."""
    client, _cfg, api_key, session_id, smoke_col = smoke_env
    result = _mcp_call_tool(client, api_key, session_id, "search", {
        "query": "retrieval augmented generation",
        "collection": smoke_col,
    })
    parsed = _assert_tool_response_valid(result, "search")
    # smoke_env pre-ingests a collection so search must always succeed
    assert "results" in parsed, (
        f"search response must have 'results' key: {parsed!r}"
    )
    assert isinstance(parsed["results"], list), (
        f"search 'results' must be a list, got: {type(parsed['results'])!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test 2: search_with_context
# ---------------------------------------------------------------------------


def test_mcp_smoke_search_with_context(smoke_env) -> None:
    """calls search_with_context, response non-empty, schema-valid (S3)."""
    client, _cfg, api_key, session_id, smoke_col = smoke_env
    result = _mcp_call_tool(client, api_key, session_id, "search_with_context", {
        "query": "retrieval augmented generation",
        "collection": smoke_col,
    })
    parsed = _assert_tool_response_valid(result, "search_with_context")
    # smoke_env collection exists — schema-valid response has 'results' key
    assert isinstance(parsed, dict), (
        f"search_with_context must return a dict, got: {type(parsed)!r}"
    )
    assert "results" in parsed, (
        f"search_with_context response must have 'results' key: {parsed!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test 3: explain
# ---------------------------------------------------------------------------


def test_mcp_smoke_explain(smoke_env) -> None:
    """calls explain, response non-empty, schema-valid (S3)."""
    client, _cfg, api_key, session_id, smoke_col = smoke_env
    result = _mcp_call_tool(client, api_key, session_id, "explain", {
        "query": "retrieval augmented generation",
        "collection": smoke_col,
    })
    parsed = _assert_tool_response_valid(result, "explain")
    # schema-valid explain response has 'results' key (ExplainResponse shape)
    assert isinstance(parsed, dict), (
        f"explain must return a dict, got: {type(parsed)!r}"
    )
    assert "results" in parsed, (
        f"explain response must have 'results' key: {parsed!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test 4: ingest_file
# ---------------------------------------------------------------------------


def test_mcp_smoke_ingest_file(tmp_path, monkeypatch) -> None:
    """calls ingest_file, response non-empty, schema-valid (S3)."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        session_id = _mcp_initialize(client, api_key)
        # Create a real file to ingest
        doc_file = tmp_path / "ingest_smoke" / "file.txt"
        doc_file.parent.mkdir(parents=True, exist_ok=True)
        doc_file.write_text("ingest_file smoke test content")
        result = _mcp_call_tool(client, api_key, session_id, "ingest_file", {
            "collection": "mcp-smoke-ingest-file",
            "path": str(doc_file),
        })
        parsed = _assert_tool_response_valid(result, "ingest_file")
        # Schema-valid response has 'status' and 'chunks_written' OR error with code
        assert isinstance(parsed, dict), f"ingest_file must return a dict: {parsed!r}"
        assert "status" in parsed or "error" in parsed, (
            f"ingest_file response must have 'status' or 'error' key: {parsed!r}"
        )


# ---------------------------------------------------------------------------
# Smoke test 5: ingest_directory
# ---------------------------------------------------------------------------


def test_mcp_smoke_ingest_directory(tmp_path, monkeypatch) -> None:
    """calls ingest_directory, response non-empty, schema-valid (S3)."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        session_id = _mcp_initialize(client, api_key)
        # Create a real directory with a file
        dir_path = tmp_path / "ingest_dir_smoke"
        dir_path.mkdir(parents=True, exist_ok=True)
        (dir_path / "file1.txt").write_text("ingest_directory smoke test content one")
        result = _mcp_call_tool(client, api_key, session_id, "ingest_directory", {
            "collection": "mcp-smoke-ingest-dir",
            "path": str(dir_path),
        })
        _assert_tool_response_valid(result, "ingest_directory")


# ---------------------------------------------------------------------------
# Smoke test 6: list_collections
# ---------------------------------------------------------------------------


def test_mcp_smoke_list_collections(smoke_env) -> None:
    """calls list_collections, response is list (may be empty) (S3)."""
    client, _cfg, api_key, session_id, smoke_col = smoke_env
    result = _mcp_call_tool(client, api_key, session_id, "list_collections", {})
    parsed = _assert_tool_response_valid(result, "list_collections")
    # smoke_env collection exists so list_collections must return a list
    assert isinstance(parsed, list), (
        f"list_collections must return a list, got: {type(parsed)!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test 7: get_collections_meta
# ---------------------------------------------------------------------------


def test_mcp_smoke_get_collections_meta(smoke_env) -> None:
    """calls get_collections_meta, response schema-valid (S3)."""
    client, _cfg, api_key, session_id, smoke_col = smoke_env
    result = _mcp_call_tool(client, api_key, session_id, "get_collections_meta", {})
    parsed = _assert_tool_response_valid(result, "get_collections_meta")
    # smoke_env collection exists so get_collections_meta must return a list
    assert isinstance(parsed, list), (
        f"get_collections_meta must return a list, got: {type(parsed)!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test 8: get_collection_meta
# ---------------------------------------------------------------------------


def test_mcp_smoke_get_collection_meta(smoke_env) -> None:
    """calls get_collection_meta, response schema-valid or 404-equivalent (S3)."""
    client, _cfg, api_key, session_id, smoke_col = smoke_env
    result = _mcp_call_tool(client, api_key, session_id, "get_collection_meta", {
        "name": smoke_col,
    })
    parsed = _assert_tool_response_valid(result, "get_collection_meta")
    # smoke_col was pre-ingested so get_collection_meta must succeed
    assert isinstance(parsed, dict), (
        f"get_collection_meta must return a dict, got: {type(parsed)!r}"
    )
    assert "name" in parsed, (
        f"get_collection_meta must have 'name' key (success path): {parsed!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test 9: list_documents
# ---------------------------------------------------------------------------


def test_mcp_smoke_list_documents(smoke_env) -> None:
    """calls list_documents, response is list (S3)."""
    client, _cfg, api_key, session_id, smoke_col = smoke_env
    result = _mcp_call_tool(client, api_key, session_id, "list_documents", {
        "collection": smoke_col,
    })
    parsed = _assert_tool_response_valid(result, "list_documents")
    # smoke_col has at least 1 document so list_documents must return a list
    assert isinstance(parsed, list), (
        f"list_documents must return a list, got: {type(parsed)!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test 10: delete_document — destructive, uses throwaway namespace
# ---------------------------------------------------------------------------


def test_mcp_smoke_delete_document(tmp_path, monkeypatch) -> None:
    """calls delete_document, response non-empty (S3).

    Uses a dedicated throwaway namespace so deletion cannot corrupt shared data.
    Creates a collection + document first, then deletes it.
    """
    smoke_ns_col = f"mcp-smoke-del-{uuid.uuid4().hex[:8]}"
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        # Ingest a real document so delete_document has something to operate on
        doc_file = tmp_path / "del_smoke" / "doc.txt"
        doc_file.parent.mkdir(parents=True, exist_ok=True)
        doc_file.write_text("delete_document smoke test content")
        ingest_file_via_path(client, smoke_ns_col, str(doc_file), api_key=api_key, timeout_s=30.0)

        # Find the doc_id via list_documents
        session_id = _mcp_initialize(client, api_key)
        ld_result = _mcp_call_tool(client, api_key, session_id, "list_documents", {
            "collection": smoke_ns_col,
        })
        docs = _assert_tool_response_valid(ld_result, "list_documents (setup for delete_document)")
        # docs is a list of document dicts with 'doc_id' field
        doc_id = docs[0]["doc_id"] if docs and isinstance(docs, list) else "nonexistent-doc-id"

        result = _mcp_call_tool(client, api_key, session_id, "delete_document", {
            "collection": smoke_ns_col,
            "doc_id": doc_id,
        })
        _assert_tool_response_valid(result, "delete_document")


# ---------------------------------------------------------------------------
# Smoke test 11: update_collection
# ---------------------------------------------------------------------------


def test_mcp_smoke_update_collection(smoke_env) -> None:
    """calls update_collection, response non-empty (S3).

    update_collection checks config.collections first. Because SearchConfig() defaults
    to collections={} (empty dict), the collection will not be found and the tool returns
    a graceful not_found error dict — this is the expected response for this smoke test.
    """
    client, cfg, api_key, session_id, smoke_col = smoke_env
    result = _mcp_call_tool(client, api_key, session_id, "update_collection", {
        "collection_name": smoke_col,
        "embedding_model": cfg.embedding_model,
    })
    parsed = _assert_tool_response_valid(result, "update_collection")
    assert isinstance(parsed, dict), (
        f"update_collection must return a dict, got: {type(parsed)!r}"
    )
    # With empty config.collections the tool always returns not_found — verify the shape
    if "error" in parsed:
        assert parsed.get("code") == "not_found", (
            f"update_collection error must have code='not_found', got: {parsed!r}"
        )


# ---------------------------------------------------------------------------
# Smoke test 12: export_collection
# ---------------------------------------------------------------------------


def test_mcp_smoke_export_collection(smoke_env) -> None:
    """calls export_collection, response non-empty (S3).

    export_collection enqueues a job and returns immediately with a QUEUED job dict.
    """
    client, _cfg, api_key, session_id, smoke_col = smoke_env
    result = _mcp_call_tool(client, api_key, session_id, "export_collection", {
        "collection": smoke_col,
    })
    parsed = _assert_tool_response_valid(result, "export_collection")
    # smoke_col has real data so export must succeed and return a job dict
    assert "job_id" in parsed, (
        f"export_collection must return a job dict with 'job_id': {parsed!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test 13: import_collection
# ---------------------------------------------------------------------------


def test_mcp_smoke_import_collection(tmp_path, monkeypatch) -> None:
    """calls import_collection, response non-empty (S3).

    Uses a nonexistent archive path — expects a graceful not_found error, not a crash.
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        session_id = _mcp_initialize(client, api_key)
        result = _mcp_call_tool(client, api_key, session_id, "import_collection", {
            "collection": "mcp-smoke-import",
            "path": str(tmp_path / "nonexistent.tar.gz"),
        })
        parsed = _assert_tool_response_valid(result, "import_collection")
        # Nonexistent archive → graceful not_found or path_unsafe error dict
        assert isinstance(parsed, dict), (
            f"import_collection must return a dict, got: {type(parsed)!r}"
        )
        assert "error" in parsed or "job_id" in parsed or "id" in parsed, (
            f"import_collection must return error or job dict: {parsed!r}"
        )


# ---------------------------------------------------------------------------
# Smoke test 14: create_key
# ---------------------------------------------------------------------------


def test_mcp_smoke_create_key(smoke_env) -> None:
    """calls create_key, response contains key id (S3)."""
    client, _cfg, api_key, session_id, _smoke_col = smoke_env
    result = _mcp_call_tool(client, api_key, session_id, "create_key", {
        "namespace": "mcp-smoke-create-key-ns",
        "label": "smoke-test-key",
    })
    parsed = _assert_tool_response_valid(result, "create_key")
    assert isinstance(parsed, dict), (
        f"create_key must return a dict, got: {type(parsed)!r}"
    )
    # smoke_env context — create_key must succeed on the success path
    assert "id" in parsed, (
        f"create_key response must have 'id' (success path): {parsed!r}"
    )
    assert "token" in parsed, (
        f"create_key success response must include 'token' (raw bearer token): {parsed!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test 15: list_keys
# ---------------------------------------------------------------------------


def test_mcp_smoke_list_keys(smoke_env) -> None:
    """calls list_keys, response is list (S3)."""
    client, _cfg, api_key, session_id, _smoke_col = smoke_env
    result = _mcp_call_tool(client, api_key, session_id, "list_keys", {})
    parsed = _assert_tool_response_valid(result, "list_keys")
    # list_keys returns {"keys": [...], "hidden_revoked_count": N} or an error dict
    assert isinstance(parsed, dict), (
        f"list_keys must return a dict, got: {type(parsed)!r}"
    )
    assert "keys" in parsed or "error" in parsed, (
        f"list_keys must have 'keys' or 'error' key: {parsed!r}"
    )
    if "keys" in parsed:
        assert isinstance(parsed["keys"], list), (
            f"list_keys 'keys' field must be a list, got: {type(parsed['keys'])!r}"
        )


# ---------------------------------------------------------------------------
# Smoke test 16: revoke_key — destructive, uses a freshly-created throwaway key
# ---------------------------------------------------------------------------


def test_mcp_smoke_revoke_key(smoke_env) -> None:
    """calls revoke_key, response non-empty (S3).

    Creates a throwaway key first, then revokes it so existing keys are not destroyed.
    """
    client, _cfg, api_key, session_id, _smoke_col = smoke_env

    # Create a throwaway key to revoke
    create_result = _mcp_call_tool(client, api_key, session_id, "create_key", {
        "namespace": "mcp-smoke-revoke-ns",
        "label": "smoke-revoke-throwaway",
    })
    create_parsed = _assert_tool_response_valid(create_result, "create_key (setup for revoke_key)")
    throwaway_id = create_parsed.get("id") if isinstance(create_parsed, dict) else None

    if throwaway_id is None:
        # create_key returned an error — skip the revoke (still validates no crash)
        pytest.fail(f"create_key did not return an id (got: {create_parsed!r}); cannot run revoke smoke")

    result = _mcp_call_tool(client, api_key, session_id, "revoke_key", {
        "key_id": throwaway_id,
    })
    parsed = _assert_tool_response_valid(result, "revoke_key")
    assert isinstance(parsed, dict), (
        f"revoke_key must return a dict, got: {type(parsed)!r}"
    )


# ---------------------------------------------------------------------------
# Smoke test 17: rotate_key
# ---------------------------------------------------------------------------


def test_mcp_smoke_rotate_key(tmp_path, monkeypatch) -> None:
    """calls rotate_key, response non-empty and schema-valid (S3).

    make_real_app always sets ARCHON_SEARCH_API_KEY via monkeypatch.setenv, so
    rotate_key returns a conflict error (env var override protection). This smoke
    test verifies the conflict guard responds with a non-empty, schema-valid error
    dict — not that rotation succeeds.

    Uses its own app instance so key rotation does not affect the smoke_env fixture
    shared by other tests.
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        session_id = _mcp_initialize(client, api_key)
        result = _mcp_call_tool(client, api_key, session_id, "rotate_key", {})
        parsed = _assert_tool_response_valid(result, "rotate_key")
        assert isinstance(parsed, dict), (
            f"rotate_key must return a dict, got: {type(parsed)!r}"
        )
        # ARCHON_SEARCH_API_KEY env var is always set by make_real_app, so the
        # conflict guard fires — verify error dict shape
        if "error" in parsed:
            assert parsed.get("code") == "conflict", (
                f"rotate_key conflict error must have code='conflict', got: {parsed!r}"
            )
        else:
            # If somehow rotation succeeded, verify expected success fields
            assert "new_key_id" in parsed or "id" in parsed, (
                f"rotate_key success response must have 'new_key_id' or 'id': {parsed!r}"
            )
