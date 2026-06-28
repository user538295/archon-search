"""tests/integration/test_e0e_t2_mcp_filter_e2e.py

T-2: Integration e2e — MCP search tool with filters + collections via JSON-RPC TestClient.

Plan task: T-2 — Integration e2e: MCP search tool with filters + collections via JSON-RPC
TestClient #tester-role

Covers scenarios: S8 (language filter + multi-collection), S9 (file_type filter +
multi-collection).

Run with:
    uv run pytest tests/integration/test_e0e_t2_mcp_filter_e2e.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

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
                "clientInfo": {"name": "t2-e2e-mcp-filter-test", "version": "1.0"},
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
    """Call an MCP tool; return the parsed SSE result payload."""
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


def _extract_tool_payload(result: dict, tool_name: str) -> dict:
    """Extract and parse the JSON text from an MCP tool response."""
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
# T-2 / test 1 — language filter + multi-collection (S8)
# ---------------------------------------------------------------------------


def test_e2e_mcp_search_multi_collection_language_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP search tool with collections + language: 'fr' returns non-error results (S8).

    Previously (pre-BE-4) this call returned code='validation_error' because the MCP
    search tool blocked the language parameter when collections was provided. After BE-4
    the restriction is lifted: the tool forwards the language filter to search_many() and
    returns a valid search response.

    The language detector is stubbed in the test environment (all chunks get language=''),
    so language='fr' returns empty results — this is correct, expected behaviour. The key
    assertion is that the tool returns 'results' (valid search response shape), NOT
    code='validation_error'.

    Scenario: S8.

    NOTE: This test proves parameter acceptance (the key regression from pre-BE-4, when
    collections + language returned validation_error), NOT that language filtering correctly
    selects documents. Filter-forwarding behavior is verified at the unit level in
    test_e0e_be4_mcp_filters.py::test_mcp_search_tool_multi_collection_with_language_filter.
    The equivalent REST e2e test (T-1) uses the same empty-result smoke-test pattern for
    the language filter.
    """
    col_a = "t2-lang-col-a"
    col_b = "t2-lang-col-b"

    # Two French-language documents ingested into separate collections
    doc_a = tmp_path / "docs_fr_a.txt"
    doc_a.write_text(
        (
            "Ce document traite du langage de programmation Python.\n"
            "Fonctions, classes et modules sont des concepts clés.\n"
            "Programmation orientée objet en Python.\n"
        ) * 4
    )
    doc_b = tmp_path / "docs_fr_b.txt"
    doc_b.write_text(
        (
            "TypeScript est un langage à typage fort.\n"
            "Les interfaces, génériques et décorateurs sont importants.\n"
            "Développement JavaScript moderne avec TypeScript.\n"
        ) * 4
    )

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        ingest_file_via_path(client, col_a, str(doc_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc_b), api_key=api_key)

        session_id = _mcp_initialize(client, api_key)

        raw = _mcp_call_tool(
            client, api_key, session_id, "search",
            {
                "query": "programmation langage",
                "collections": [col_a, col_b],
                "language": "fr",
            },
        )

        payload = _extract_tool_payload(raw, "search")

        # Must NOT be a validation_error — language filter must be accepted
        # in multi-collection MCP search (core assertion for S8)
        assert payload.get("code") != "validation_error", (
            f"Expected language filter to be accepted in multi-collection MCP search, "
            f"but got code='validation_error': {payload!r}"
        )

        # Response must have the valid search response shape (results key present)
        assert "results" in payload, (
            f"Expected 'results' in payload (valid search response), got: {payload!r}"
        )
        assert isinstance(payload["results"], list), (
            f"Expected results to be a list, got: {type(payload['results'])!r}"
        )

        # The language detector stub assigns language='' to all chunks in the test
        # environment, so language='fr' matches nothing — empty results is correct.
        # This verifies the language filter path runs without error (not silently ignored).
        assert payload["results"] == [], (
            f"Expected empty results: stub language detector assigns language='', "
            f"so language='fr' filter matches nothing; got: {payload['results']}"
        )

        # excluded_collections must be present and empty — zero-result legs are silent
        excluded = payload.get("excluded_collections", [])
        assert excluded == [], (
            f"excluded_collections must be [] for zero-result filter legs; got: {excluded}"
        )


# ---------------------------------------------------------------------------
# T-2 / test 2 — file_type filter + multi-collection (S9)
# ---------------------------------------------------------------------------


def test_e2e_mcp_search_multi_collection_file_type_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP search tool with collections + file_type: '.py' returns only .py results (S9).

    Both collections contain .py files and .md files. A file_type='.py' filter applied
    via the MCP search tool must forward the filter to search_many() (via SearchFilters),
    returning results only from .py files across both legs.

    Scenario: S9.
    """
    col_a = "t2-ft-col-a"
    col_b = "t2-ft-col-b"

    # col_a: one .py file; col_b: one .py file + one .md file
    py_file_a = tmp_path / "module_a.py"
    py_file_a.write_text(
        (
            "# Python module alpha\n"
            "def compute_alpha():\n"
            "    '''Compute alpha function result.'''\n"
            "    return 42\n"
        ) * 5
    )
    py_file_b = tmp_path / "module_b.py"
    py_file_b.write_text(
        (
            "# Python module beta\n"
            "def compute_beta():\n"
            "    '''Compute beta function result.'''\n"
            "    return 100\n"
        ) * 5
    )
    md_file_b = tmp_path / "readme_b.md"
    md_file_b.write_text(
        (
            "# Readme Beta\n\n"
            "Documentation and reference for the beta compute module.\n"
        ) * 5
    )

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        ingest_file_via_path(client, col_a, str(py_file_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(py_file_b), api_key=api_key)
        ingest_file_via_path(client, col_b, str(md_file_b), api_key=api_key)

        session_id = _mcp_initialize(client, api_key)

        raw = _mcp_call_tool(
            client, api_key, session_id, "search",
            {
                "query": "compute function module",
                "collections": [col_a, col_b],
                "file_type": ".py",  # leading dot — must be normalised
            },
        )

        payload = _extract_tool_payload(raw, "search")

        # Must NOT be a validation_error — file_type filter must be accepted (S9)
        assert payload.get("code") != "validation_error", (
            f"Expected file_type filter to be accepted in multi-collection MCP search, "
            f"but got code='validation_error': {payload!r}"
        )

        # Response must have the valid search response shape
        assert "results" in payload, (
            f"Expected 'results' in payload (valid search response), got: {payload!r}"
        )
        assert isinstance(payload["results"], list), (
            f"Expected results to be a list, got: {type(payload['results'])!r}"
        )

        # Results must be non-empty — both collections have .py files
        results = payload["results"]
        assert results, (
            f"Expected non-empty results from .py filter across both collections; "
            f"got empty results. Both collections have .py files ingested."
        )

        # All results must have file_type == 'py' — .md file from col_b must be excluded
        for r in results:
            assert r.get("file_type") == "py", (
                f"Non-.py result slipped through file_type filter: "
                f"file_type={r.get('file_type')!r}, source_path={r.get('source_path')!r}"
            )

        # Results must span both collections — multi-collection fan-out must have searched
        # both legs (col_a and col_b each have .py files, both should contribute)
        collections_in_results = {r.get("collection") for r in results}
        assert col_a in collections_in_results, (
            f"Expected results from {col_a!r} in multi-collection .py filter; "
            f"seen collections: {collections_in_results}"
        )
        assert col_b in collections_in_results, (
            f"Expected results from {col_b!r} in multi-collection .py filter; "
            f"seen collections: {collections_in_results}"
        )

        # excluded_collections must be empty — filter legs are silent (S9)
        excluded = payload.get("excluded_collections", [])
        assert excluded == [], (
            f"excluded_collections must be [] for filter legs; got: {excluded}"
        )
