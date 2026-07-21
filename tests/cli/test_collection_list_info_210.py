"""Tests for brief 210: collection list uses SearchStore directly, not create_pipeline.

brief 350: info was converted to an HTTP proxy (see test_cli_collection.py for new tests).

Tests:
- test_list_cmd_shows_collections: mocked store → names/counts printed, exit 0
- test_list_cmd_empty: mocked store returns [] → "No collections found", exit 0
- test_list_cmd_config_error_exits_1: load_config raises → exit 1
- test_list_cmd_uses_store_with_correct_db_path: structural guard — _make_store called with cfg
- test_list_cmd_disconnect_called_on_store_error: finally disconnect even when list_collections raises
- test_info_proxies_to_server: mocked httpx.get → formatted output, exit 0  (brief 350)
- test_info_server_not_running: ConnectError → exit 1  (brief 350)
"""
from __future__ import annotations

import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from archon_search._types import CollectionInfo
from archon_search.cli.collection import collection
from archon_search.collection_meta import CollectionMeta


def _make_cfg(db_path: str = "/tmp/test-db") -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = db_path
    return cfg


def _make_store_mock(collections: list | None = None, meta: CollectionMeta | None = None) -> MagicMock:
    store = MagicMock()
    store.connect = AsyncMock()
    store.disconnect = AsyncMock()
    store.list_collections = AsyncMock(return_value=collections or [])
    store.get_collection_meta = AsyncMock(return_value=meta)
    return store


# ---------------------------------------------------------------------------
# list_cmd
# ---------------------------------------------------------------------------


def test_list_cmd_shows_collections() -> None:
    cfg = _make_cfg()
    info1 = CollectionInfo(name="col-a", doc_count=3, chunk_count=12)
    info2 = CollectionInfo(name="col-b", doc_count=0, chunk_count=0)
    store = _make_store_mock(collections=[info1, info2])

    with (
        patch("archon_search.cli.collection.load_config", return_value=cfg),
        patch("archon_search.cli.collection._make_store", return_value=store),
    ):
        result = CliRunner().invoke(collection, ["list"])

    assert result.exit_code == 0, result.output
    assert "col-a" in result.output
    assert "col-b" in result.output
    assert "docs=3" in result.output
    assert "chunks=12" in result.output
    store.connect.assert_awaited_once()
    store.disconnect.assert_awaited_once()
    store.list_collections.assert_awaited_once()


def test_list_cmd_empty() -> None:
    cfg = _make_cfg()
    store = _make_store_mock(collections=[])

    with (
        patch("archon_search.cli.collection.load_config", return_value=cfg),
        patch("archon_search.cli.collection._make_store", return_value=store),
    ):
        result = CliRunner().invoke(collection, ["list"])

    assert result.exit_code == 0, result.output
    assert "No collections found" in result.output


def test_list_cmd_config_error_exits_1() -> None:
    with patch("archon_search.cli.collection.load_config", side_effect=RuntimeError("bad config")):
        result = CliRunner().invoke(collection, ["list"])

    assert result.exit_code == 1
    assert "bad config" in result.output


def test_list_cmd_uses_store_with_correct_db_path() -> None:
    """Structural guard: list constructs SearchStore with cfg.db_path, not create_pipeline."""
    cfg = _make_cfg(db_path="/tmp/guard-db")
    store = _make_store_mock(collections=[])

    with (
        patch("archon_search.cli.collection.load_config", return_value=cfg),
        patch("archon_search.cli.collection._make_store", return_value=store) as mock_make_store,
    ):
        result = CliRunner().invoke(collection, ["list"])

    assert result.exit_code == 0
    mock_make_store.assert_called_once_with(cfg)


def test_list_cmd_disconnect_called_on_store_error() -> None:
    """finally: disconnect is awaited even when list_collections raises."""
    cfg = _make_cfg()
    store = _make_store_mock(collections=[])
    store.list_collections = AsyncMock(side_effect=RuntimeError("db error"))

    with (
        patch("archon_search.cli.collection.load_config", return_value=cfg),
        patch("archon_search.cli.collection._make_store", return_value=store),
    ):
        result = CliRunner().invoke(collection, ["list"])

    assert result.exit_code == 1
    store.disconnect.assert_awaited_once()


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


def test_info_server_not_running() -> None:
    """brief 350: info exits 1 with 'not running' when server is unreachable."""
    with patch("archon_search.cli.collection.httpx.get", side_effect=httpx.ConnectError("refused")):
        result = CliRunner().invoke(collection, ["info", "my-col"])

    assert result.exit_code == 1
    assert "not running" in result.output.lower()


def test_make_store_constructs_search_store_with_db_path() -> None:
    """Body guard: _make_store must call SearchStore(cfg.db_path), not a different attribute."""
    cfg = _make_cfg(db_path="/tmp/body-guard")
    with patch("archon_search.store.SearchStore") as MockStore:
        from archon_search.cli.collection import _make_store  # noqa: PLC0415
        _make_store(cfg)
    MockStore.assert_called_once_with("/tmp/body-guard")
