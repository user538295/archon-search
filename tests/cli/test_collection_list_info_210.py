"""Tests for list_cmd and info HTTP proxy behaviour (brief 210 → DCS proxy conversion).

DCS converted list_cmd from a direct-store path to an HTTP proxy (GET /collections/),
matching the existing info proxy.  These tests cover list_cmd's proxy contract and
error paths not already exercised in test_cli_collection.py.

Tests:
- test_list_cmd_shows_collections: httpx.get returns list → names/counts printed, exit 0
- test_list_cmd_empty: httpx.get returns [] → "No collections found", exit 0
- test_list_cmd_server_not_running: ConnectError → exit 1, "not running" message
- test_list_cmd_url_contains_collections_path: structural guard — URL ends with /collections/
- test_list_cmd_non_200_exits_1: server returns 500 → exit 1
- test_list_cmd_no_make_store_in_module: structural guard — _make_store removed from module
- test_info_proxies_to_server: mocked httpx.get → formatted output, exit 0  (brief 350)
- test_info_server_not_running: ConnectError → exit 1  (brief 350)
- test_info_shows_source_and_status / test_info_omits_*: description, source and status lines (S40)
- test_info_strips_control_characters_*: terminal controls never reach stdout (S40)
- test_info_omits_whitespace_only_source_and_status: control-only values print no blank line (S40)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
from _cli_server_stub import server_reported_stopped
from click.testing import CliRunner

import archon_search.cli.collection as collection_mod
from archon_search.cli.collection import collection

# ---------------------------------------------------------------------------
# list_cmd — HTTP proxy (DCS)
# ---------------------------------------------------------------------------


def _mock_list_resp(collections: list) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = collections
    return resp


def test_list_cmd_shows_collections() -> None:
    data = [
        {"name": "col-a", "doc_count": 3, "chunk_count": 12},
        {"name": "col-b", "doc_count": 0, "chunk_count": 0},
    ]
    with patch("archon_search.cli.collection.httpx.get", return_value=_mock_list_resp(data)):
        result = CliRunner().invoke(collection, ["list", "--api-key", "testkey"])

    assert result.exit_code == 0, result.output
    assert "col-a" in result.output
    assert "col-b" in result.output
    assert "docs=3" in result.output
    assert "chunks=12" in result.output


def test_list_cmd_empty() -> None:
    with patch("archon_search.cli.collection.httpx.get", return_value=_mock_list_resp([])):
        result = CliRunner().invoke(collection, ["list", "--api-key", "testkey"])

    assert result.exit_code == 0, result.output
    assert "No collections found" in result.output


def test_list_cmd_server_not_running() -> None:
    # Isolate the managed-service fallback: on the default local URL a failed /ready
    # probe consults _get_service().status(); server_reported_stopped() pins both the
    # probe and the service not-running so the assertion is hermetic regardless of host state.
    with (
        server_reported_stopped(),
        patch("archon_search.cli.collection.httpx.get", side_effect=httpx.ConnectError("refused")),
    ):
        result = CliRunner().invoke(collection, ["list", "--api-key", "testkey"])

    assert result.exit_code == 1
    assert "not running" in result.output.lower() or "not running" in getattr(result, "stderr", "").lower()


def test_list_cmd_url_contains_collections_path() -> None:
    """Structural guard: list_cmd calls GET /collections/ (trailing slash)."""
    with patch("archon_search.cli.collection.httpx.get", return_value=_mock_list_resp([])) as mock_get:
        CliRunner().invoke(collection, ["list", "--api-key", "testkey"])

    called_url = mock_get.call_args[0][0]
    assert called_url.endswith("/collections/"), f"Expected URL ending /collections/, got {called_url}"


def test_list_cmd_non_200_exits_1() -> None:
    resp = MagicMock()
    resp.status_code = 500
    resp.text = "internal error"
    with patch("archon_search.cli.collection.httpx.get", return_value=resp):
        result = CliRunner().invoke(collection, ["list", "--api-key", "testkey"])

    assert result.exit_code == 1


def test_list_cmd_no_make_store_in_module() -> None:
    """Structural guard: _make_store was removed from collection.py by the DCS proxy conversion."""
    assert not hasattr(collection_mod, "_make_store"), (
        "_make_store still present in collection.py — DCS proxy conversion incomplete"
    )


# ---------------------------------------------------------------------------
# info — HTTP proxy (brief 350)
# ---------------------------------------------------------------------------


def test_info_proxies_to_server() -> None:
    """brief 350: info proxies GET /collections/{name} and prints formatted key-value output."""
    detail = {
        "name": "my-col", "description": "test desc", "namespace": "default",
        "doc_count": 5, "chunk_count": 20, "active_embedding_model": "BAAI/bge-small-en-v1.5",
        "pending_embedding_model": None, "needs_reindex": False, "reindex_job_id": None,
        "last_indexed": None, "default_ttl_seconds": None, "schema_version": 0,
        "centroid_present": False, "path": "/data/my-col", "status": "ready",
        "acl_protected_count": 0, "acl_open_count": 0,
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = detail

    with patch("archon_search.cli.collection.httpx.get", return_value=mock_resp):
        result = CliRunner().invoke(collection, ["info", "my-col", "--api-key", "testkey"])

    assert result.exit_code == 0, result.output
    assert "my-col" in result.output
    assert "doc_count: 5" in result.output
    assert "description: test desc" in result.output


def _info_output(**overrides: object) -> str:
    detail = {
        "name": "my-col", "description": "test desc", "namespace": "default",
        "doc_count": 5, "chunk_count": 20, "active_embedding_model": "BAAI/bge-small-en-v1.5",
        "pending_embedding_model": None, "needs_reindex": False, "reindex_job_id": None,
        "last_indexed": None, "default_ttl_seconds": None, "schema_version": 0,
        "centroid_present": False,
    }
    detail.update(overrides)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = detail
    with patch("archon_search.cli.collection.httpx.get", return_value=mock_resp):
        result = CliRunner().invoke(collection, ["info", "my-col", "--api-key", "testkey"])
    assert result.exit_code == 0, result.output
    return result.output


def test_info_shows_source_and_status() -> None:
    """S40: description_source and description_status print as key: value lines."""
    output = _info_output(description_source="generated", description_status="ok")
    assert "description_source: generated" in output
    assert "description_status: ok" in output


def test_info_omits_source_and_status_when_null() -> None:
    """S40: an empty description and null source/status print no such lines."""
    output = _info_output(description="", description_source=None, description_status=None)
    assert "description" not in output
    absent = _info_output(description=None, description_source=None, description_status=None)
    assert "description" not in absent


def test_info_omits_whitespace_only_description_after_cleaning() -> None:
    """A description of only line breaks/control characters prints no blank line."""
    output = _info_output(description="\n\x1b\r", description_source=None, description_status=None)
    assert "description" not in output


def test_info_omits_whitespace_only_source_and_status() -> None:
    """Control-only source/status values print no blank line."""
    output = _info_output(description_source="\n", description_status="\x1b\t")
    assert "description_source" not in output
    assert "description_status" not in output


def test_info_keeps_status_when_description_is_empty() -> None:
    """First-ingest/failed state: no description yet, but the status line still prints."""
    output = _info_output(description="", description_source=None, description_status="pending")
    assert "description:" not in output
    assert "description_source" not in output
    assert "description_status: pending" in output


def test_info_keeps_description_when_source_and_status_null() -> None:
    """A legacy server omits source/status: the description still prints alone."""
    output = _info_output(description_source=None, description_status=None)
    assert "description: test desc" in output
    assert "description_source" not in output
    assert "description_status" not in output


def test_info_strips_control_characters_from_other_fields() -> None:
    """Every printed value is cleaned, not just the description trio."""
    output = _info_output(name="my\x1b[2Jcol", namespace="ns\nforged: 1")
    assert "\x1b" not in output
    assert "name: my [2Jcol" in output
    assert "namespace: ns forged: 1" in output


def test_info_strips_control_characters_from_untrusted_text() -> None:
    """Description, source and status come from documents/the server: no escape or newline reaches the terminal."""
    output = _info_output(
        description="line1\nfake: injected\x1b[31m",
        description_source="gen\x07erated",
        description_status="o\rk",
    )
    assert "\x1b" not in output and "\x07" not in output and "\r" not in output
    assert "description: line1 fake: injected [31m" in output
    assert "description_source: gen erated" in output
    assert "description_status: o k" in output


def test_info_server_not_running() -> None:
    """brief 350: info exits 1 with 'not running' when server is unreachable."""
    with (
        server_reported_stopped(),
        patch("archon_search.cli.collection.httpx.get", side_effect=httpx.ConnectError("refused")),
    ):
        result = CliRunner().invoke(collection, ["info", "my-col"])

    assert result.exit_code == 1
    assert "not running" in result.output.lower()
