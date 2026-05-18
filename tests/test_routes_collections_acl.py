"""Tests for ACL stats in GET /collections/{name} (Task 4.2 — FEAT-044)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.sync import path_to_collection_name


def _make_client(
    tmp_path: Path,
    tmp_store: JobStore,
    mock_store: MagicMock,
    cfg: SearchConfig | None = None,
) -> tuple[TestClient, str]:
    """Helper: wire app with mock_store; return (client, collection_name)."""
    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    if cfg is None:
        cfg = SearchConfig()
        cfg.db_path = str(tmp_path / "search")
        cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return client, path_to_collection_name(str(src))


@pytest.fixture
def tmp_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def test_get_collection_info_includes_acl_stats(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} includes acl_protected_count and acl_open_count fields."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")

    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=5)
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    # 3 protected, 2 open
    mock_store.get_acl_stats = AsyncMock(return_value=(3, 2))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    client, _ = _make_client(tmp_path, tmp_store, mock_store, cfg)

    response = client.get(f"/collections/{name}")
    assert response.status_code == 200
    data = response.json()

    assert "acl_protected_count" in data
    assert "acl_open_count" in data
    assert data["acl_protected_count"] == 3
    assert data["acl_open_count"] == 2


def test_acl_stats_sum_to_total(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """acl_protected_count + acl_open_count equals the total chunk count."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    name = path_to_collection_name(str(src))
    total_chunks = 10
    protected = 7
    open_count = total_chunks - protected  # 3

    meta = CollectionMeta(name=name, namespace="default")

    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=total_chunks)
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.get_acl_stats = AsyncMock(return_value=(protected, open_count))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    client, _ = _make_client(tmp_path, tmp_store, mock_store, cfg)

    response = client.get(f"/collections/{name}")
    assert response.status_code == 200
    data = response.json()

    assert data["acl_protected_count"] + data["acl_open_count"] == total_chunks
