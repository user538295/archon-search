"""Tests for GET/POST/DELETE /collections/* endpoints ."""
from __future__ import annotations

import asyncio
import dataclasses
import math
import os
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from pathlib import Path
from typing import Any, NamedTuple
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import INGEST_LOCK_TIMEOUT_S
from archon_search.jobs.model import JobStatus
from archon_search.jobs.store import JobStore
from archon_search.server._ingest_lock import STORE_BUSY_CODE
from archon_search.server.app import create_app
from archon_search.server.routes_collections import COLLECTION_JOB_IN_PROGRESS_DETAIL, COLLECTION_JOB_IN_PROGRESS_DETAIL_TEMPLATE
from archon_search.store import CollectionLockNotHeldError, StoreBusyError
from archon_search.sync import path_to_collection_name
from archon_search.types import CommunityRebuildJob, DeleteJob, ExportJob, ImportJob, IngestJob, IngestOptions, MetadataReindexJob, MigrationJob, MigrationKind, ReindexJob, SyncJob
from tests._parked_lock import parked_waiters, reaping, until_parked


@pytest.fixture
def tmp_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


@pytest.fixture
def config(tmp_path: Path) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    return cfg


@pytest.fixture
def client(config: SearchConfig, tmp_store: JobStore, auth_headers: dict[str, str]) -> TestClient:
    app = create_app(config, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store
    return TestClient(app, headers=auth_headers)


# ---------------------------------------------------------------------------
# GET /collections/
# ---------------------------------------------------------------------------


def test_list_collections_empty(client: TestClient) -> None:
    """Empty config returns empty list."""
    response = client.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert data == []


def test_list_collections_shows_configured(tmp_path: Path, tmp_store: JobStore) -> None:
    """Collections in config appear in the list with correct name."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    # No meta rows — collection with no meta row is included for DEFAULT_NAMESPACE
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    entry = data[0]
    assert "name" in entry
    assert "path" in entry
    assert "description" in entry
    assert "doc_count" in entry
    assert "chunk_count" in entry
    assert "status" in entry

    # Fix 8: assert name matches path_to_collection_name
    expected_name = path_to_collection_name(str(src))
    assert entry["name"] == expected_name


def test_list_collections_returns_real_chunk_count(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections reports the live count_chunks value, not a hardcoded 0 (bug-080)."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name=name, namespace=DEFAULT_NAMESPACE)]
    )
    mock_store.count_chunks = AsyncMock(return_value=42)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["chunk_count"] == 42
    # ns is forwarded for API symmetry; count_chunks is table-wide (isolation comes
    # from one-collection-one-namespace, not from this argument).
    mock_store.count_chunks.assert_awaited_once_with(name, namespace=DEFAULT_NAMESPACE)


def test_list_collections_counts_each_collection_independently(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """Each collection carries its own live count, not a shared/conflated value (C1-I-03)."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    a = tmp_path / "cola"
    a.mkdir()
    b = tmp_path / "colb"
    b.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(a), str(b)]
    app = create_app(cfg, tmp_store)

    name_a = path_to_collection_name(str(a))
    name_b = path_to_collection_name(str(b))
    counts = {name_a: 7, name_b: 11}

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(
        return_value=[
            CollectionMeta(name=name_a, namespace=DEFAULT_NAMESPACE),
            CollectionMeta(name=name_b, namespace=DEFAULT_NAMESPACE),
        ]
    )
    mock_store.count_chunks = AsyncMock(side_effect=lambda n, namespace: counts[n])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    data = c.get("/collections/").json()
    by_name = {e["name"]: e["chunk_count"] for e in data}
    assert by_name[name_a] == 7
    assert by_name[name_b] == 11


def test_list_collections_forwards_nondefault_namespace(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """count_chunks receives the caller's real namespace, not a hardcoded default (C1-I-02)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    key_a = "a" * 64
    cfg.namespaces = {key_a: "tenantA"}
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name=name, namespace="tenantA")]
    )
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    c = TestClient(app, headers={"Authorization": f"Bearer {key_a}"})
    response = c.get("/collections/")
    assert response.status_code == 200
    assert response.json()[0]["chunk_count"] == 5
    mock_store.count_chunks.assert_awaited_once_with(name, namespace="tenantA")


def test_list_collections_metaless_skips_count_chunks(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """A configured-but-not-indexed collection (no meta row) reports 0 WITHOUT calling
    count_chunks — proving the col_meta guard skips the store, so an orphaned table's
    rows can never leak into the listing (C2-I-1). Fails if the guard is removed."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])  # no meta row
    # If the guard were removed, this non-zero count would surface for a metaless entry.
    mock_store.count_chunks = AsyncMock(return_value=999)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["chunk_count"] == 0
    mock_store.count_chunks.assert_not_awaited()


def test_list_collections_chunk_count_zero_on_store_error(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections degrades to chunk_count=0 when count_chunks raises (never 500s the list)."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name=name, namespace=DEFAULT_NAMESPACE)]
    )
    mock_store.count_chunks = AsyncMock(side_effect=RuntimeError("db error"))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/collections/")
    assert response.status_code == 200
    assert response.json()[0]["chunk_count"] == 0
    # Prove the 0 came from the except branch, not from count_chunks being skipped.
    mock_store.count_chunks.assert_awaited_once()


# ---------------------------------------------------------------------------
# POST /collections/
# ---------------------------------------------------------------------------


def test_add_collection_persists_and_starts_ingest(
    tmp_path: Path, config: SearchConfig, tmp_store: JobStore
) -> None:
    """POST /collections/ persists the path and returns an IngestJob (202)."""
    import asyncio as _asyncio
    src = tmp_path / "myproject"
    src.mkdir()
    app = create_app(config, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())
    app.state.search_store = mock_store
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.PENDING.value
    # FE-4/C1-I-1: 202 must carry the server-derived collection name (not empty string)
    assert data["collection"] == path_to_collection_name(str(src.resolve()))

    # Config was updated
    updated_config: SearchConfig = app.state.config
    assert str(src.resolve()) in updated_config.collections

    # Fix 10: job exists in store
    assert tmp_store.get(data["job_id"]) is not None


def test_add_duplicate_collection_returns_409(
    tmp_path: Path, config: SearchConfig, tmp_store: JobStore
) -> None:
    """POST /collections/ twice with same path returns 409 on second call."""
    import asyncio as _asyncio
    src = tmp_path / "myproject"
    src.mkdir()
    app = create_app(config, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())
    app.state.search_store = mock_store
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        first = c.post("/collections/", json={"path": str(src)})
        second = c.post("/collections/", json={"path": str(src)})

    assert first.status_code == 202
    assert second.status_code == 409
    assert "already registered" in second.json()["detail"]


def test_add_collection_missing_path_returns_422(client: TestClient) -> None:
    """POST /collections/ without path is a validation error."""
    response = client.post("/collections/", json={})
    assert response.status_code == 422


def test_add_collection_already_pinned_returns_409(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ with a path already in pinned_collections returns 409."""
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.pinned_collections = [str(pinned)]
    app = create_app(cfg, tmp_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": str(pinned)})
    assert response.status_code == 409
    assert "already registered" in response.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE /collections/{name}
# ---------------------------------------------------------------------------


def test_remove_collection_deletes_config_and_data(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """DELETE /collections/{name} removes path from config and drops LanceDB data."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    # Fix 9: wire a mock search_store and assert drop_collection was called
    import asyncio as _asyncio
    from archon_search.collection_meta import CollectionMeta as _CM
    _meta = _CM(name=path_to_collection_name(str(src)), namespace="default")
    mock_search_store = MagicMock()
    mock_search_store.get_collection_meta = AsyncMock(return_value=_meta)
    mock_search_store.drop_collection = AsyncMock()
    mock_search_store.delete_collection_meta = AsyncMock()
    mock_search_store.lock_for = MagicMock(return_value=_asyncio.Lock())
    app.state.search_store = mock_search_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    name = path_to_collection_name(str(src))

    response = c.delete(f"/collections/{name}")

    assert response.status_code == 200
    # Path removed from config
    updated_config: SearchConfig = app.state.config
    assert str(src) not in updated_config.collections

    # LanceDB drop was called
    mock_search_store.drop_collection.assert_called_once_with(name, _locked_by_caller=True)


def test_remove_unknown_collection_returns_404(client: TestClient) -> None:
    """DELETE /collections/{name} for unknown name returns 404."""
    response = client.delete("/collections/nonexistent-collection")
    assert response.status_code == 404


def test_remove_pinned_only_collection_rejected(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """DELETE rejects a path that is in pinned_collections but NOT in collections."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "pinned-only"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.pinned_collections = [str(src)]
    # NOT in cfg.collections
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")
    mock_search_store = MagicMock()
    mock_search_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_search_store.migrate_namespace = AsyncMock()
    mock_search_store.connect = AsyncMock()
    mock_search_store.disconnect = AsyncMock()
    app.state.search_store = mock_search_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.delete(f"/collections/{name}")
    assert response.status_code in (400, 409)
    data = response.json()
    assert "detail" in data


# ---------------------------------------------------------------------------
# GET /collections/{name}
# ---------------------------------------------------------------------------


def test_get_collection_info(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns CollectionDetail fields."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model=cfg.embedding_model)
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == name
    assert "path" in data
    assert "active_embedding_model" in data
    assert "centroid_present" in data
    assert "last_indexed" in data

    # active_embedding_model comes from meta, which was set to cfg.embedding_model
    assert data["active_embedding_model"] == cfg.embedding_model


def test_get_collection_info_unknown_returns_404(client: TestClient) -> None:
    """GET /collections/{name} for unknown name returns 404."""
    response = client.get("/collections/nonexistent-collection")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /collections/{name}/reindex
# ---------------------------------------------------------------------------


def test_reindex_returns_ingest_job(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/reindex starts a job and returns IngestJob (202)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")
    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=asyncio.Lock())
    app.state.search_store = mock_store
    app.state.embedder_cache = MagicMock()
    app.state.pipeline = MagicMock()

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.PENDING.value
    # The job names its collection, so DELETE /collections/{name} finds it in the job store.
    assert data["collection"] == name
    assert tmp_store.get(data["job_id"]).collection == name


def test_reindex_unknown_collection_returns_404(client: TestClient) -> None:
    """POST /collections/{name}/reindex for unknown collection returns 404 (once the service is ready: readiness is
    checked first)."""
    client.app.state.embedder_cache = MagicMock()
    client.app.state.pipeline = MagicMock()
    response = client.post("/collections/nonexistent-collection/reindex")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /collections/{name} — real doc_count and centroid_present 
# ---------------------------------------------------------------------------


def test_collection_info_doc_count_real(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns real doc_count from SearchStore."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=3)
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    assert response.json()["doc_count"] == 3


def test_collection_info_doc_count_zero_on_store_error(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns doc_count=0 when SearchStore raises."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(side_effect=RuntimeError("db error"))
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    assert response.json()["doc_count"] == 0


def test_collection_info_chunk_count_real(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns real chunk_count from SearchStore (bug-080)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=3)
    mock_store.count_chunks = AsyncMock(return_value=17)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    assert response.json()["chunk_count"] == 17
    mock_store.count_chunks.assert_awaited_once_with(name, namespace="default")


def test_collection_info_chunk_count_zero_on_store_error(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns chunk_count=0 when count_chunks raises."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=3)
    mock_store.count_chunks = AsyncMock(side_effect=RuntimeError("db error"))
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    assert response.json()["chunk_count"] == 0
    # Prove the 0 came from the except branch, not from count_chunks being skipped.
    mock_store.count_chunks.assert_awaited_once()


def test_collection_info_centroid_present_true(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns centroid_present=true when centroid is set."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    meta = CollectionMeta(name=path_to_collection_name(str(src)), centroid=[0.1, 0.2])
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    name = path_to_collection_name(str(src))

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    assert response.json()["centroid_present"] is True


def test_collection_info_centroid_present_false(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns centroid_present=false when centroid is None."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    meta = CollectionMeta(name=path_to_collection_name(str(src)), centroid=None)
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    name = path_to_collection_name(str(src))

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    assert response.json()["centroid_present"] is False


# ---------------------------------------------------------------------------
# GET /collections/ namespace filter 
# ---------------------------------------------------------------------------


def test_list_collections_filters_by_namespace(tmp_path: Path, tmp_store: JobStore) -> None:
    """GET /collections/ returns only collections whose meta row matches the caller's namespace."""
    from archon_search.collection_meta import CollectionMeta

    src_a = tmp_path / "colA"
    src_a.mkdir()
    src_b = tmp_path / "colB"
    src_b.mkdir()

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src_a), str(src_b)]

    key_a = "a" * 64
    key_b = "b" * 64
    cfg.namespaces = {key_a: "tenantA", key_b: "tenantB"}

    app = create_app(cfg, tmp_store)

    name_a = path_to_collection_name(str(src_a))
    name_b = path_to_collection_name(str(src_b))

    meta_a = CollectionMeta(name=name_a, namespace="tenantA", description="Tenant A docs")
    meta_b = CollectionMeta(name=name_b, namespace="tenantB", description="Tenant B secret")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[meta_a, meta_b])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    c = TestClient(app, headers={"Authorization": f"Bearer {key_a}"})
    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    names = [e["name"] for e in data]
    assert name_a in names
    assert name_b not in names
    # bug-090: the ns-filtered listing surfaces the caller-namespace collection's
    # own stored description (not the old hardcoded "").
    assert [e["description"] for e in data if e["name"] == name_a] == ["Tenant A docs"]


def test_list_collections_no_meta_default_ns(tmp_path: Path, tmp_store: JobStore) -> None:
    """Collection with no meta row: included for DEFAULT_NAMESPACE, excluded for other namespaces."""
    src = tmp_path / "colX"
    src.mkdir()

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    key_a = "a" * 64
    cfg.namespaces = {key_a: "tenantA"}

    app = create_app(cfg, tmp_store)

    # No meta rows at all
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    default_key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    # DEFAULT_NAMESPACE caller → collection with no meta row is included
    c_default = TestClient(app, headers={"Authorization": f"Bearer {default_key}"})
    resp_default = c_default.get("/collections/")
    assert resp_default.status_code == 200
    assert len(resp_default.json()) == 1

    # tenantA caller → collection with no meta row is excluded
    c_a = TestClient(app, headers={"Authorization": f"Bearer {key_a}"})
    resp_a = c_a.get("/collections/")
    assert resp_a.status_code == 200
    assert resp_a.json() == []


def test_list_collections_single_key_backward_compat(tmp_path: Path, tmp_store: JobStore) -> None:
    """Single-key deployment (no namespaces config): DEFAULT_NAMESPACE sees all collections."""
    from archon_search.collection_meta import CollectionMeta

    src1 = tmp_path / "col1"
    src1.mkdir()
    src2 = tmp_path / "col2"
    src2.mkdir()

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src1), str(src2)]
    # no namespaces configured — single-key fallback to DEFAULT_NAMESPACE

    app = create_app(cfg, tmp_store)

    name1 = path_to_collection_name(str(src1))
    name2 = path_to_collection_name(str(src2))

    meta1 = CollectionMeta(name=name1, namespace="default")
    meta2 = CollectionMeta(name=name2, namespace="default")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[meta1, meta2])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    default_key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {default_key}"})
    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    names = [e["name"] for e in data]
    assert name1 in names
    assert name2 in names


# ---------------------------------------------------------------------------
# namespace field in GET /collections/ 
# ---------------------------------------------------------------------------


def test_routes_list_collections_namespace(tmp_path: Path, tmp_store: JobStore) -> None:
    """GET /collections/ response entries include "namespace": "default"."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[meta])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    for entry in data:
        assert "namespace" in entry
        assert entry["namespace"] == "default"


# ---------------------------------------------------------------------------
# namespace field in GET /collections/{name} 
# ---------------------------------------------------------------------------


def test_routes_get_collection_namespace(tmp_path: Path, tmp_store: JobStore) -> None:
    """GET /collections/{name} response includes "namespace": "default"."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    meta = CollectionMeta(name=path_to_collection_name(str(src)))
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    name = path_to_collection_name(str(src))

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    data = response.json()
    assert "namespace" in data
    assert data["namespace"] == "default"


# ---------------------------------------------------------------------------
# POST /collections/ — namespace enforcement 
# ---------------------------------------------------------------------------


def _make_app_with_mock_store(
    cfg: "SearchConfig",
    tmp_store: "JobStore",
    mock_store: "MagicMock",
) -> "TestClient":
    """Helper: create app with a pre-wired mock search_store."""
    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store
    return app


def test_add_collection_global_uniqueness_409(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ returns 409 when the collection name is already in meta (any namespace)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    name = path_to_collection_name(str(src))
    existing_meta = CollectionMeta(name=name, namespace="other-ns")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[existing_meta])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": str(src)})
    assert response.status_code == 409
    assert "already registered" in response.json()["detail"]


def test_add_collection_unknown_embedding_model_returns_422(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ with an unknown embedding_model returns 422, not 500 (S221).

    An unknown model makes the underlying embedder raise a plain ValueError
    (real fastembed does this). That must surface as 422 before any job is
    created — not escape as an unhandled 500.
    """
    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    # Mimic real fastembed: an unknown model raises ValueError when probed.
    bad_embedder = MagicMock()
    bad_embedder.embed = AsyncMock(
        side_effect=ValueError("Model unknown-model-xyz-404 is not supported")
    )

    with patch("archon_search.model_validation.make_embedder", return_value=bad_embedder):
        response = c.post(
            "/collections/",
            json={"path": str(src), "embedding_model": "unknown-model-xyz-404"},
        )

    assert response.status_code == 422
    # Validation must fail before any job/meta is created.
    mock_store.update_collection_meta.assert_not_called()


def test_add_collection_writes_stub_meta(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """Successful POST /collections/ writes a stub meta row before ingest completes."""
    import asyncio as _asyncio
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 202
    # Verify update_collection_meta was called with a CollectionMeta stub
    mock_store.update_collection_meta.assert_called_once()
    call_arg = mock_store.update_collection_meta.call_args[0][0]
    assert isinstance(call_arg, CollectionMeta)
    expected_name = path_to_collection_name(str(src.resolve()))
    assert call_arg.name == expected_name


def test_add_collection_rollback_on_meta_failure(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ returns 500 and reverts config when update_collection_meta raises non-ValueError."""
    import asyncio as _asyncio

    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock(side_effect=RuntimeError("disk error"))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    # Provide a real lock so the pre-acquire (moved before state mutation) can succeed.
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 500
    # Config must be reverted — the path should not be in collections
    updated_config: SearchConfig = app.state.config
    assert str(src.resolve()) not in updated_config.collections


def test_add_collection_cross_namespace_race_returns_409(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ returns 409 (not 500) when update_collection_meta raises ValueError (TOCTOU race)."""
    import asyncio as _asyncio

    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock(
        side_effect=ValueError("Collection belongs to other namespace")
    )
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    # Provide a real lock so the pre-acquire (moved before state mutation) can succeed.
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 409
    # Config must be reverted
    updated_config: SearchConfig = app.state.config
    assert str(src.resolve()) not in updated_config.collections


def test_add_collection_job_has_correct_namespace(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """Created job's namespace matches the caller's namespace."""
    import asyncio as _asyncio
    src = tmp_path / "myproject"
    src.mkdir()

    caller_ns = "tenantX"
    caller_key = "x" * 64
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.namespaces = {caller_key: caller_ns}

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 202
    data = response.json()
    assert data["namespace"] == caller_ns

    # Also verify the stub meta was written with the correct namespace
    call_arg = mock_store.update_collection_meta.call_args[0][0]
    assert call_arg.namespace == caller_ns


# ---------------------------------------------------------------------------
# DELETE /collections/{name} — namespace enforcement 
# ---------------------------------------------------------------------------


def test_remove_collection_cross_namespace_404(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """DELETE returns 404 when meta row exists for a different namespace; config is unchanged."""
    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    caller_key = "c" * 64
    cfg.namespaces = {caller_key: "tenantC"}

    name = path_to_collection_name(str(src))
    # get_collection_meta filters by namespace in the real store.
    # Since the meta belongs to "tenantOther", a query for "tenantC" returns None.
    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})
    response = c.delete(f"/collections/{name}")

    assert response.status_code == 404
    assert name in response.json()["detail"]
    # Config must not be mutated
    assert str(src) in cfg.collections


def test_remove_collection_deletes_meta_row(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """Successful DELETE calls delete_collection_meta with the correct name + namespace."""
    import asyncio as _asyncio
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    caller_key = "d" * 64
    caller_ns = "tenantD"
    cfg.namespaces = {caller_key: caller_ns}

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace=caller_ns)

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.drop_collection = AsyncMock()
    mock_store.delete_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})
    response = c.delete(f"/collections/{name}")

    assert response.status_code == 200
    mock_store.delete_collection_meta.assert_called_once_with(name, caller_ns)


def test_remove_collection_success_drops_table_and_meta(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """Successful DELETE calls both drop_collection AND delete_collection_meta; meta absent after."""
    import asyncio as _asyncio
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    caller_key = "e" * 64
    caller_ns = "tenantE"
    cfg.namespaces = {caller_key: caller_ns}

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace=caller_ns)

    # Simulate meta row is present before delete, absent after
    deleted = {"done": False}

    async def _get_meta(n: str, namespace: str = "default") -> CollectionMeta | None:
        return None if deleted["done"] else meta

    async def _delete_meta(n: str, ns: str) -> None:
        deleted["done"] = True

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(side_effect=_get_meta)
    mock_store.drop_collection = AsyncMock()
    mock_store.delete_collection_meta = AsyncMock(side_effect=_delete_meta)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})
    response = c.delete(f"/collections/{name}")

    assert response.status_code == 200
    mock_store.drop_collection.assert_called_once_with(name, _locked_by_caller=True)
    mock_store.delete_collection_meta.assert_called_once_with(name, caller_ns)
    assert deleted["done"] is True


def test_delete_collection_503_when_lock_held(
    tmp_path: Path, tmp_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE /collections/{name} returns 503 when the per-collection lock is already held."""
    # Shrink the real lock-acquisition timeout so the 503 path fires in ~0.1s instead of the
    # production 30s (which was ~30s of pure serial dead-time in the suite). The route helper
    # _ingest_lock.acquire_collection_lock_or_503 reads _constants.INGEST_LOCK_TIMEOUT_S live,
    # so patching the constants module takes effect without touching production defaults.
    lock_timeout_s = 0.1
    monkeypatch.setattr("archon_search.constants.INGEST_LOCK_TIMEOUT_S", lock_timeout_s)
    import asyncio as _asyncio
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "locked_col"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    caller_key = "f" * 64
    caller_ns = "tenantF"
    cfg.namespaces = {caller_key: caller_ns}

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace=caller_ns)

    # Pre-acquire the lock so acquire_collection_lock_or_503 times out.
    held_lock = _asyncio.Lock()

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.drop_collection = AsyncMock()
    mock_store.delete_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=held_lock)

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    # Acquire the lock before the request so it times out inside the handler.
    import threading

    ready = threading.Event()
    done = threading.Event()

    def _hold_lock() -> None:
        import asyncio

        async def _run() -> None:
            await held_lock.acquire()
            ready.set()
            done.wait()

        asyncio.run(_run())

    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()
    ready.wait()

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})
    response = c.delete(f"/collections/{name}")

    done.set()
    holder.join()

    assert response.status_code == 503
    assert response.json() == {"detail": "reindex in progress; retry after Retry-After seconds", "code": STORE_BUSY_CODE}
    assert response.headers.get("Retry-After") == str(math.ceil(lock_timeout_s))
    # drop_collection must NOT have been called
    mock_store.drop_collection.assert_not_called()


def test_delete_acquires_lock_before_drop(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """DELETE /collections/{name} calls acquire_collection_lock_or_503 before drop_collection."""
    import asyncio as _asyncio
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "ordered_col"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")

    call_order: list[str] = []

    acquired_lock = _asyncio.Lock()

    async def _mock_acquire_lock(store, collection_name: str):
        call_order.append("acquire")
        await acquired_lock.acquire()
        return acquired_lock

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)

    async def _mock_drop(n: str, **_kwargs) -> None:
        call_order.append("drop")

    mock_store.drop_collection = AsyncMock(side_effect=_mock_drop)
    mock_store.delete_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.acquire_collection_lock_or_503",
        side_effect=_mock_acquire_lock,
    ):
        response = c.delete(f"/collections/{name}")

    assert response.status_code == 200
    assert call_order == ["acquire", "drop"], (
        f"Expected ['acquire', 'drop'], got {call_order!r}"
    )
    assert not acquired_lock.locked(), "Lock must be released after successful DELETE"


def test_delete_collection_lock_released_on_exception(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """Lock is released even when delete_collection_meta raises."""
    import asyncio as _asyncio
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "exc_col"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")

    real_lock = _asyncio.Lock()

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.drop_collection = AsyncMock()
    mock_store.delete_collection_meta = AsyncMock(side_effect=RuntimeError("db failure"))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=real_lock)

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {key}"})
    response = c.delete(f"/collections/{name}")

    # The store failure maps to the sanitized collection_drop_failed 500
    assert response.status_code == 500
    assert response.json()["code"] == "collection_drop_failed"
    # Lock MUST be released despite the exception
    assert not real_lock.locked(), "Lock must be released even when delete_collection_meta raises"


# ---------------------------------------------------------------------------
# S399/S428/S430: DELETE /collections/{name} must return 503 when an ingest job is active
# ---------------------------------------------------------------------------


def _make_mock_search_store_for_delete(collection_name: str, ns: str = "default") -> MagicMock:
    """Return a mock SearchStore that reports `collection_name` as existing (for DELETE tests)."""
    meta = CollectionMeta(name=collection_name, namespace=ns)
    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.drop_collection = AsyncMock()
    mock_store.delete_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=asyncio.Lock())  # free lock
    return mock_store


_INGEST_JOB_TIMESTAMP = "2026-10-21T00:00:00Z"
#: Bound on each awaited step of the concurrent-request tests below: a hang fails the test instead of the run.
_ASYNC_STEP_TIMEOUT_S = 5.0
_TENANT_X_KEY = "x" * 64


def _ingest_job(job_id: str, status: JobStatus, collection: str, namespace: str = "default") -> IngestJob:
    """Base IngestJob on *collection* with fixed timestamps, for the DELETE active-ingest guard tests."""
    return IngestJob(
        job_id=job_id, status=status, created_at=_INGEST_JOB_TIMESTAMP, updated_at=_INGEST_JOB_TIMESTAMP,
        namespace=namespace, collection=collection,
    )


def _ingest_guard_app(
    tmp_path: Path, tmp_store: JobStore, caller_ns: str = "default"
) -> tuple[object, TestClient, MagicMock, str, Path]:
    """App + authed client for DELETE of one registered collection whose meta row is in *caller_ns*."""
    src = tmp_path / "ingest_guard_col"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    if caller_ns != "default":
        key = _TENANT_X_KEY
        cfg.namespaces = {key: caller_ns}
    name = path_to_collection_name(str(src))
    mock_search_store = _make_mock_search_store_for_delete(name, caller_ns)
    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_search_store
    return app, TestClient(app, headers={"Authorization": f"Bearer {key}"}), mock_search_store, name, src


@pytest.mark.parametrize("active_status", [JobStatus.RUNNING, JobStatus.CANCELLING])
def test_delete_collection_503_when_ingest_active(
    tmp_path: Path, tmp_store: JobStore, active_status: JobStatus
) -> None:
    """DELETE returns 503 when a base IngestJob is RUNNING or CANCELLING for this collection.

    Regression test for S399/S428/S430:
    - RUNNING: the collection lock is released early in _default_ingest_task_with_lock
      (before writes complete), so a free lock does not mean the collection is safe to delete.
    - CANCELLING: DELETE /jobs/{id} transitions RUNNING→CANCELLING but does NOT cancel the
      asyncio task; the ingest continues writing until it finishes.
    The route must check the job store directly.
    """
    app, client, mock_search_store, name, src = _ingest_guard_app(tmp_path, tmp_store)
    tmp_store.create_job(_ingest_job("s399-active-job-id", active_status, name))

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 503, (
        f"Expected 503 when ingest job is {active_status.value}, "
        f"got {response.status_code}; body={response.json()}"
    )
    assert response.json() == {
        "detail": "job s399-active-job-id is in progress; retry after it completes",
        "code": STORE_BUSY_CODE,
    }
    assert response.headers.get("Retry-After") == str(math.ceil(INGEST_LOCK_TIMEOUT_S))
    # The collection must NOT have been mutated in any way, and the lock is released but kept
    mock_search_store.drop_collection.assert_not_called()
    mock_search_store.delete_collection_meta.assert_not_called()
    mock_search_store.discard_lock.assert_not_called()
    assert not mock_search_store.lock_for.return_value.locked()
    assert app.state.config.collections == [str(src)]


def _job_on(job_type: type[IngestJob], job_id: str, status: JobStatus, **fields: Any) -> Callable[[str], IngestJob]:
    """A *job_type* job with fixed timestamps; the param factories below bind its collection to the test's."""
    return lambda name: job_type(
        job_id=job_id, status=status, created_at=_INGEST_JOB_TIMESTAMP, updated_at=_INGEST_JOB_TIMESTAMP,
        collection=name, **fields,
    )


#: (job kind, extra fields) of each kind whose job blocks DELETE only while it may still write the collection.
_COLLECTION_WRITING_KINDS = [
    pytest.param(ReindexJob, {}, id="reindex"),
    pytest.param(ImportJob, {}, id="import"),
    pytest.param(MigrationJob, {"kind": MigrationKind.REWRITE}, id="migration"),
    pytest.param(MetadataReindexJob, {}, id="metadata-reindex"),
    pytest.param(CommunityRebuildJob, {}, id="community-rebuild"),
]


@pytest.mark.parametrize(
    "make_job",
    [
        # Read-only: the export task only reads the collection's rows and meta row.
        pytest.param(_job_on(ExportJob, "running-export", JobStatus.RUNNING), id="export-running"),
        # The kinds in NEVER_BLOCKING_JOB_KINDS never block, even when bound to the collection: a real SyncJob is
        # global (its collection is always ""), the legacy DeleteJob has no task, and an export only reads.
        pytest.param(_job_on(SyncJob, "running-sync", JobStatus.RUNNING), id="sync-running"),
        pytest.param(_job_on(DeleteJob, "running-delete", JobStatus.RUNNING), id="legacy-delete-running"),
        # A reindex job saved by an older server names no collection (""), so it is never matched to this one.
        pytest.param(lambda name: ReindexJob(
            job_id="legacy-reindex", status=JobStatus.RUNNING, created_at=_INGEST_JOB_TIMESTAMP,
            updated_at=_INGEST_JOB_TIMESTAMP,
        ), id="legacy-reindex-without-collection"),
        # QUEUED bulk jobs have no task yet. The scheduler's later run of a migration opens the dropped table under
        # the collection lock and fails cleanly; an import creates its target collection by design.
        pytest.param(
            _job_on(MigrationJob, "queued-migration", JobStatus.QUEUED, kind=MigrationKind.REWRITE),
            id="migration-queued",
        ),
        pytest.param(_job_on(ImportJob, "queued-import", JobStatus.QUEUED), id="import-queued"),
    ],
)
def test_delete_collection_200_when_job_on_collection_cannot_write_it(
    tmp_path: Path, tmp_store: JobStore, make_job: Callable[[str], IngestJob]
) -> None:
    """DELETE is not blocked by a job that cannot write the collection's table or meta row (yet): a never-blocking
    kind (export, sync, legacy delete), a legacy reindex naming no collection, or a QUEUED bulk job."""
    app, client, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    tmp_store.create_job(make_job(name))

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 200, f"Expected 200, got {response.status_code}; body={response.text}"
    assert response.json() == {"name": name, "deleted": True}
    mock_search_store.drop_collection.assert_awaited_once()
    assert app.state.config.collections == []


@pytest.mark.parametrize("terminal_status", [JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED])
@pytest.mark.parametrize(("job_type", "fields"), _COLLECTION_WRITING_KINDS)
def test_delete_collection_200_when_collection_writing_job_is_terminal(
    tmp_path: Path, tmp_store: JobStore, job_type: type[IngestJob], fields: dict[str, Any], terminal_status: JobStatus
) -> None:
    """A terminal job of a kind that blocks DELETE while active writes nothing more, so DELETE drops the collection."""
    app, client, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    tmp_store.create_job(_job_on(job_type, "terminal-job", terminal_status, **fields)(name))

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 200, f"Expected 200, got {response.status_code}; body={response.text}"
    assert response.json() == {"name": name, "deleted": True}
    mock_search_store.drop_collection.assert_awaited_once()
    assert app.state.config.collections == []


def _reindex_job_as_route_creates(store: JobStore, name: str, ns: str) -> IngestJob:
    """A ReindexJob as POST /collections/{name}/reindex creates it: PENDING, on *name*."""
    return store.create_reindex(name, namespace=ns)


def _metadata_reindex_job_as_route_creates(store: JobStore, name: str, ns: str) -> IngestJob:
    """A MetadataReindexJob as POST /collections/{name}/reindex-metadata creates it (RUNNING)."""
    return store.create_metadata_reindex(collection=name, namespace=ns)


def _migration_job_as_route_creates(store: JobStore, name: str, ns: str) -> IngestJob:
    """A REWRITE MigrationJob as POST /collections/{name}/migrate creates it (RUNNING)."""
    return store.create_migration(
        collection=name, kind=MigrationKind.REWRITE, backup_confirmed=True, namespace=ns, status=JobStatus.RUNNING,
    )


def _import_job_as_route_creates(store: JobStore, name: str, ns: str) -> IngestJob:
    """An ImportJob as POST /collections/{name}/import creates it (QUEUED, until the scheduler claims it RUNNING)."""
    return store.create_import(
        collection=name, archive_path="/archives/col.tar.gz", force_overwrite=False, ignore_schema_version=False,
        on_error="fail", namespace=ns,
    )


def _community_rebuild_job_as_route_creates(store: JobStore, name: str, ns: str) -> IngestJob:
    """A CommunityRebuildJob as POST /graph/{collection}/rebuild-communities creates it (RUNNING)."""
    return store.create_community_rebuild(collection=name, namespace=ns)


@pytest.mark.parametrize(
    ("make_job", "active_status"),
    [
        # PENDING: _reindex_task loads the embedder before its RUNNING claim, holding no collection lock.
        pytest.param(_reindex_job_as_route_creates, JobStatus.PENDING, id="reindex-pending"),
        pytest.param(_reindex_job_as_route_creates, JobStatus.RUNNING, id="reindex-running"),
        # CANCELLING: DELETE /jobs/{id} does not stop the task; ingest_directory keeps writing.
        pytest.param(_reindex_job_as_route_creates, JobStatus.CANCELLING, id="reindex-cancelling"),
        pytest.param(_metadata_reindex_job_as_route_creates, JobStatus.RUNNING, id="metadata-reindex-running"),
        pytest.param(_metadata_reindex_job_as_route_creates, JobStatus.CANCELLING, id="metadata-reindex-cancelling"),
        pytest.param(_migration_job_as_route_creates, JobStatus.RUNNING, id="migration-running"),
        pytest.param(_migration_job_as_route_creates, JobStatus.CANCELLING, id="migration-cancelling"),
        # The import task writes through ensure_collection / ingest_chunks, taking the lock per write only.
        pytest.param(_import_job_as_route_creates, JobStatus.RUNNING, id="import-running"),
        # CANCELLING: the import task checks for it between batches only, and keeps writing until then.
        pytest.param(_import_job_as_route_creates, JobStatus.CANCELLING, id="import-cancelling"),
        # The rebuild writes the collection's graph tables, then clears meta.community_rebuild_job_id by an unlocked
        # meta read + upsert, which re-creates the meta row of a collection dropped in between.
        pytest.param(_community_rebuild_job_as_route_creates, JobStatus.RUNNING, id="community-rebuild-running"),
        pytest.param(_community_rebuild_job_as_route_creates, JobStatus.CANCELLING, id="community-rebuild-cancelling"),
    ],
)
def test_delete_collection_503_when_collection_mutating_job_active(
    tmp_path: Path,
    tmp_store: JobStore,
    caplog: pytest.LogCaptureFixture,
    make_job: Callable[[JobStore, str, str], IngestJob],
    active_status: JobStatus,
) -> None:
    """DELETE refuses with the store-busy 503 while a job that writes the collection is active, and logs a WARNING
    naming the job's kind and id.

    None of these tasks holds ``lock_for(name)`` for the job's whole life: ``_reindex_task`` writes through
    ``pipeline.ingest_directory`` (lock per ``ingest_chunks`` call, ``ensure_collection`` lock-free — a dropped
    table and meta row are re-created), the import task likewise locks per write, and the metadata-reindex /
    migration / community-rebuild tasks take the lock only inside their store call (or not at all) and then
    upsert the meta row unlocked, so a free lock does not mean the collection is safe to drop. The job store is
    the signal.
    """
    app, client, mock_search_store, name, src = _ingest_guard_app(tmp_path, tmp_store)
    job = make_job(tmp_store, name, "default")
    tmp_store.update(job.job_id, status=active_status)

    with caplog.at_level("WARNING", logger="archon_search.server.routes_collections"):
        response = client.delete(f"/collections/{name}")

    assert response.status_code == 503, (
        f"Expected 503 while a {type(job).__name__} is {active_status.value}, "
        f"got {response.status_code}; body={response.json()}"
    )
    body = response.json()
    assert set(body) == {"detail", "code"}
    assert body["code"] == STORE_BUSY_CODE
    assert job.job_id in body["detail"], "the caller's own job id is shown, as for an active ingest"
    assert response.headers.get("Retry-After") == str(math.ceil(INGEST_LOCK_TIMEOUT_S))
    [record] = [r for r in caplog.records if r.getMessage().startswith(f"DELETE /collections/{name} refused")]
    assert record.levelname == "WARNING"
    assert f": {type(job).__name__} {job.job_id} is {active_status.value} " in record.getMessage()
    mock_search_store.drop_collection.assert_not_called()
    mock_search_store.delete_collection_meta.assert_not_called()
    mock_search_store.discard_lock.assert_not_called()
    assert not mock_search_store.lock_for.return_value.locked()
    assert app.state.config.collections == [str(src)]


@pytest.mark.parametrize("active_status", [JobStatus.RUNNING, JobStatus.CANCELLING], ids=lambda s: s.value)
@pytest.mark.parametrize(
    ("dry_run", "expected_status"),
    [
        # A dry run only reads the table (SearchStore.reindex_metadata) and records no meta pointer, so it never
        # writes the collection and DELETE need not refuse for it.
        pytest.param(True, 200, id="dry-run"),
        pytest.param(False, 503, id="not-dry-run"),
    ],
)
def test_delete_collection_refuses_an_active_metadata_reindex_only_when_it_writes(
    tmp_path: Path, tmp_store: JobStore, active_status: JobStatus, dry_run: bool, expected_status: int
) -> None:
    """DELETE answers 503 while a non-dry-run metadata reindex is active but drops the collection while a dry run is."""
    app, client, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    job = tmp_store.create_metadata_reindex(collection=name, namespace="default", dry_run=dry_run)
    tmp_store.update(job.job_id, status=active_status)

    response = client.delete(f"/collections/{name}")

    assert response.status_code == expected_status, response.text
    assert (mock_search_store.drop_collection.await_count == 1) is (expected_status == 200)
    assert (app.state.config.collections == []) is (expected_status == 200)


@dataclasses.dataclass
class _UnclassifiedJob(IngestJob):
    """A job kind in neither COLLECTION_WRITING_JOB_STATUSES nor NEVER_BLOCKING_JOB_KINDS."""


def test_delete_collection_503_while_an_unclassified_job_kind_is_not_terminal(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A job of a kind nobody classified fails DELETE closed with the documented store-busy 503 — not a bare 500 —
    and the gap is logged at ERROR."""
    app, client, mock_search_store, name, src = _ingest_guard_app(tmp_path, tmp_store)
    job = tmp_store.create_job(_job_on(_UnclassifiedJob, "unclassified-job", JobStatus.QUEUED)(name))

    with caplog.at_level("ERROR", logger="archon_search.jobs.store"):
        response = client.delete(f"/collections/{name}")

    assert response.status_code == 503, response.text
    assert response.json() == {
        "detail": COLLECTION_JOB_IN_PROGRESS_DETAIL_TEMPLATE.format(job_id=job.job_id), "code": STORE_BUSY_CODE,
    }
    assert any(
        r.levelname == "ERROR" and "unclassified kind _UnclassifiedJob" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
    mock_search_store.drop_collection.assert_not_called()
    assert app.state.config.collections == [str(src)]


@pytest.mark.parametrize(
    ("caller_ns", "job_ns", "job_id_shown"),
    [
        pytest.param("default", "other-team", False, id="default-caller-foreign-job"),
        pytest.param("tenantX", "default", False, id="tenant-caller-default-job"),
        pytest.param("tenantX", "tenantX", True, id="tenant-caller-own-job"),
    ],
)
def test_delete_collection_503_shows_ingest_job_id_only_to_its_namespace(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture, caller_ns: str, job_ns: str, job_id_shown: bool
) -> None:
    """A RUNNING base IngestJob from ANY namespace blocks DELETE; its id reaches only its own namespace.

    One LanceDB table per collection name and a name-keyed collection lock mean an ingest from another
    namespace writes into the very table DELETE drops. A foreign job id must not leak on the wire.
    """
    app, client, mock_search_store, name, src = _ingest_guard_app(tmp_path, tmp_store, caller_ns)
    job_id = "namespaced-ingest-job-id"
    tmp_store.create_job(_ingest_job(job_id, JobStatus.RUNNING, name, namespace=job_ns))

    with caplog.at_level("WARNING", logger="archon_search.server.routes_collections"):
        response = client.delete(f"/collections/{name}")

    assert response.status_code == 503, f"DELETE dropped a collection an ingest writes to: {response.text}"
    expected_detail = (
        COLLECTION_JOB_IN_PROGRESS_DETAIL_TEMPLATE.format(job_id=job_id) if job_id_shown else COLLECTION_JOB_IN_PROGRESS_DETAIL
    )
    assert response.json() == {"detail": expected_detail, "code": STORE_BUSY_CODE}
    assert (job_id in response.text) is job_id_shown
    assert response.headers.get("Retry-After") == str(math.ceil(INGEST_LOCK_TIMEOUT_S))
    # The server log names the job even when the wire hides it from a foreign namespace.
    [record] = [r for r in caplog.records if r.getMessage().startswith(f"DELETE /collections/{name} refused")]
    assert record.levelname == "WARNING"
    assert job_id in record.getMessage()
    mock_search_store.drop_collection.assert_not_called()
    mock_search_store.delete_collection_meta.assert_not_called()
    mock_search_store.discard_lock.assert_not_called()
    assert not mock_search_store.lock_for.return_value.locked()
    assert app.state.config.collections == [str(src)]


def test_delete_collection_503_hides_a_foreign_non_ingest_job_id(tmp_path: Path, tmp_store: JobStore) -> None:
    """Another namespace's reindex on the collection blocks DELETE like its ingest does, with the generic detail:
    the foreign job id never reaches the caller."""
    _, client, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    job = tmp_store.create_reindex(name, namespace="other-team")
    tmp_store.update(job.job_id, status=JobStatus.RUNNING)

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": COLLECTION_JOB_IN_PROGRESS_DETAIL, "code": STORE_BUSY_CODE}
    assert job.job_id not in response.text
    mock_search_store.drop_collection.assert_not_called()


@pytest.mark.parametrize("with_store", [True, False], ids=["store", "no-store"])
def test_delete_config_only_collection_503_while_a_job_writes_it(
    tmp_path: Path, tmp_store: JobStore, with_store: bool
) -> None:
    """A configured collection with no meta row yet (its first ingest is RUNNING) is refused like any other: that
    ingest creates the table and meta row, which would outlive the DELETE. The config entry stays, the collection's
    failed ingest stays a retry candidate (only a DELETE that succeeds cancels it), and the lock is released and
    kept."""
    app, client, mock_search_store, name, src = _ingest_guard_app(tmp_path, tmp_store)
    mock_search_store.get_collection_meta = AsyncMock(return_value=None)
    mock_search_store.get_all_collections_meta = AsyncMock(return_value=[])
    if not with_store:
        app.state.search_store = None
    tmp_store.create_job(_ingest_job("first-ingest", JobStatus.RUNNING, name))
    tmp_store.create_job(_ingest_job("failed-ingest", JobStatus.FAILED, name))

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 503, response.text
    assert response.json() == {
        "detail": COLLECTION_JOB_IN_PROGRESS_DETAIL_TEMPLATE.format(job_id="first-ingest"), "code": STORE_BUSY_CODE,
    }
    assert app.state.config.collections == [str(src)]
    assert _statuses(tmp_store, ["failed-ingest"]) == {"failed-ingest": JobStatus.FAILED}
    if with_store:
        mock_search_store.lock_for.assert_called_once_with(name)
        assert not mock_search_store.lock_for.return_value.locked()
        mock_search_store.discard_lock.assert_not_called()


def test_delete_config_only_collection_200_without_an_active_job(tmp_path: Path, tmp_store: JobStore) -> None:
    """With no job writing it, a config-only collection is removed from the config without touching its data, and
    the lock entry DELETE created for it is discarded — only after the release, as for a dropped collection — so
    no entry is left behind per deleted config-only name."""
    app, client, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    mock_search_store.get_collection_meta = AsyncMock(return_value=None)
    mock_search_store.get_all_collections_meta = AsyncMock(return_value=[])
    lock = mock_search_store.lock_for.return_value
    held_at_discard: list[bool] = []
    mock_search_store.discard_lock.side_effect = lambda collection: held_at_discard.append(lock.locked())

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 200, response.text
    assert response.json() == {"name": name, "deleted": True}
    assert app.state.config.collections == []
    mock_search_store.drop_collection.assert_not_called()
    mock_search_store.delete_collection_meta.assert_not_called()
    mock_search_store.discard_lock.assert_called_once_with(name)
    assert held_at_discard == [False]
    assert not lock.locked()


async def test_delete_config_only_collection_503_while_another_write_holds_the_lock(
    tmp_path: Path, tmp_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE of a config-only collection waits for the collection lock like any DELETE: while another write holds
    it past the wait bound, the answer is the lock helper's 503 ``store_busy`` and the config entry stays."""
    lock_timeout_s = 0.05
    monkeypatch.setattr("archon_search.constants.INGEST_LOCK_TIMEOUT_S", lock_timeout_s)
    app, _, mock_search_store, name, src = _ingest_guard_app(tmp_path, tmp_store)
    mock_search_store.get_collection_meta = AsyncMock(return_value=None)
    mock_search_store.get_all_collections_meta = AsyncMock(return_value=[])
    lock = mock_search_store.lock_for.return_value

    await lock.acquire()
    try:
        async with _asgi_client(app) as client:
            response = await asyncio.wait_for(client.delete(f"/collections/{name}"), timeout=_ASYNC_STEP_TIMEOUT_S)
    finally:
        lock.release()

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "reindex in progress; retry after Retry-After seconds", "code": STORE_BUSY_CODE}
    assert response.headers.get("Retry-After") == str(math.ceil(lock_timeout_s))
    assert app.state.config.collections == [str(src)]
    mock_search_store.drop_collection.assert_not_called()


def test_delete_config_only_collection_drops_a_meta_row_created_while_it_waited_for_the_lock(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """The meta row is read again under the collection lock: a first ingest that held the lock while this DELETE
    waited created the table and meta row, so DELETE drops them like any collection's — a config-only 200 would leave
    them behind."""
    app, client, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    created_meanwhile = CollectionMeta(name=name, namespace="default")
    lock = mock_search_store.lock_for.return_value
    held_at_read: list[bool] = []

    async def get_collection_meta(*_args: Any, **_kwargs: Any) -> CollectionMeta | None:
        held_at_read.append(lock.locked())
        return None if len(held_at_read) == 1 else created_meanwhile

    mock_search_store.get_collection_meta = AsyncMock(side_effect=get_collection_meta)
    mock_search_store.get_all_collections_meta = AsyncMock(return_value=[])

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 200, response.text
    assert response.json() == {"name": name, "deleted": True}
    assert held_at_read == [False, True], "the second meta read must run under the collection lock"
    mock_search_store.drop_collection.assert_awaited_once_with(name, _locked_by_caller=True)
    mock_search_store.delete_collection_meta.assert_awaited_once_with(name, "default")
    mock_search_store.discard_lock.assert_called_once_with(name)
    assert app.state.config.collections == []


def test_delete_config_only_collection_404_once_another_namespace_created_it(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """A config-only collection is visible only while no namespace has a meta row for it: when another namespace's
    first ingest created one while this DELETE waited for the lock, the re-check under the lock answers 404 — the
    config entry and the other namespace's table are left alone, and the lock is released."""
    app, client, mock_search_store, name, src = _ingest_guard_app(tmp_path, tmp_store)
    mock_search_store.get_collection_meta = AsyncMock(return_value=None)
    lock = mock_search_store.lock_for.return_value
    held_at_read: list[bool] = []

    async def get_all_collections_meta() -> list[CollectionMeta]:
        held_at_read.append(lock.locked())
        return [] if len(held_at_read) == 1 else [CollectionMeta(name=name, namespace="other-team")]

    mock_search_store.get_all_collections_meta = AsyncMock(side_effect=get_all_collections_meta)

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 404, response.text
    assert held_at_read == [False, True], "the visibility re-check must run under the collection lock"
    assert response.json() == {"detail": f"Collection {name!r} not found"}
    assert app.state.config.collections == [str(src)]
    mock_search_store.drop_collection.assert_not_called()
    mock_search_store.delete_collection_meta.assert_not_called()
    assert not mock_search_store.lock_for.return_value.locked()


def test_delete_config_only_collection_cancels_its_ingest_retries(tmp_path: Path, tmp_store: JobStore) -> None:
    """A config-only collection's FAILED path ingest is a MaintenanceLoop retry candidate, whose retry would create
    the table and meta row of the deleted collection: DELETE cancels it, as it does for a collection it drops."""
    app, client, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    mock_search_store.get_collection_meta = AsyncMock(return_value=None)
    mock_search_store.get_all_collections_meta = AsyncMock(return_value=[])
    tmp_store.create_job(_ingest_job("failed-first-ingest", JobStatus.FAILED, name))

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 200, response.text
    assert app.state.config.collections == []
    cancelled = tmp_store.get("failed-first-ingest")
    assert cancelled is not None and cancelled.status == JobStatus.CANCELLED
    mock_search_store.drop_collection.assert_not_called()


@pytest.mark.parametrize("foreign_first", [True, False], ids=["foreign-job-first", "own-job-first"])
def test_delete_collection_503_prefers_callers_own_active_ingest_job_id(
    tmp_path: Path, tmp_store: JobStore, foreign_first: bool
) -> None:
    """With a foreign AND an own active ingest, the 503 names the caller's own job, whatever the job order."""
    _, client, _, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    foreign = _ingest_job("foreign-ingest-job-id", JobStatus.RUNNING, name, namespace="other-team")
    own = _ingest_job("own-ingest-job-id", JobStatus.RUNNING, name)
    for job in (foreign, own) if foreign_first else (own, foreign):
        tmp_store.create_job(job)

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 503
    assert response.json()["detail"] == COLLECTION_JOB_IN_PROGRESS_DETAIL_TEMPLATE.format(job_id="own-ingest-job-id")
    assert "foreign-ingest-job-id" not in response.text


@pytest.mark.parametrize(
    ("status", "same_collection"),
    [
        *[
            pytest.param(status, True, id=f"{status.value}-same-collection")
            for status in (
                JobStatus.PENDING, JobStatus.QUEUED, JobStatus.DONE,
                JobStatus.FAILED, JobStatus.FAILED_EXPIRED, JobStatus.CANCELLED,
            )
        ],
        pytest.param(JobStatus.RUNNING, False, id="RUNNING-other-collection"),
    ],
)
def test_delete_collection_200_when_no_active_ingest_on_collection(
    tmp_path: Path, tmp_store: JobStore, status: JobStatus, same_collection: bool
) -> None:
    """DELETE drops the collection when no base IngestJob is RUNNING/CANCELLING on it.

    PENDING is excluded (a REST ingest and a maintenance retry both hold or wait for the lock while
    PENDING), terminal and queued jobs write nothing, and a RUNNING ingest on another collection is
    irrelevant.
    """
    app, client, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    job_collection = name if same_collection else f"{name}-other"
    tmp_store.create_job(_ingest_job(f"{status.value.lower()}-ingest", status, job_collection))

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 200, f"Expected 200, got {response.status_code}; body={response.text}"
    assert response.json() == {"name": name, "deleted": True}
    mock_search_store.drop_collection.assert_awaited_once()
    mock_search_store.delete_collection_meta.assert_awaited_once_with(name, "default")
    assert app.state.config.collections == []


def _seed_retry_candidates(tmp_store: JobStore, name: str) -> dict[str, IngestJob]:
    """Base ingest jobs a DELETE of *name* (namespace ``default``) may or may not end, keyed by job id."""
    jobs = {
        "failed": _ingest_job("failed", JobStatus.FAILED, name),
        "pending": _ingest_job("pending", JobStatus.PENDING, name),
        "done": _ingest_job("done", JobStatus.DONE, name),
        "other-collection": _ingest_job("other-collection", JobStatus.FAILED, f"{name}-other"),
        "other-namespace": _ingest_job("other-namespace", JobStatus.FAILED, name, namespace="other-team"),
        "reindex": ReindexJob(
            job_id="reindex", status=JobStatus.FAILED, created_at=_INGEST_JOB_TIMESTAMP,
            updated_at=_INGEST_JOB_TIMESTAMP, collection=name,
        ),
        "export": ExportJob(
            job_id="export", status=JobStatus.PENDING, created_at=_INGEST_JOB_TIMESTAMP,
            updated_at=_INGEST_JOB_TIMESTAMP, collection=name,
        ),
    }
    for job in jobs.values():
        tmp_store.create_job(job)
    return jobs


def _statuses(tmp_store: JobStore, job_ids: Iterable[str]) -> dict[str, JobStatus]:
    statuses: dict[str, JobStatus] = {}
    for job_id in job_ids:
        job = tmp_store.get(job_id)
        assert job is not None
        statuses[job_id] = job.status
    return statuses


def test_delete_collection_ends_its_maintenance_retries(tmp_path: Path, tmp_store: JobStore) -> None:
    """A DELETE that drops the collection stops every maintenance retry from re-creating it: its FAILED base ingest
    jobs (never retried) and PENDING ones (a retry waiting for the collection lock DELETE holds, whose claim is then
    refused) end CANCELLED — in every namespace, as the dropped table is shared by name — not FAILED_EXPIRED, which
    would count as a failed ingest to re-submit. Other statuses, collections and subclass jobs are left as they are."""
    _, client, _, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    jobs = _seed_retry_candidates(tmp_store, name)

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 200, response.text
    assert _statuses(tmp_store, jobs) == {
        "failed": JobStatus.CANCELLED,
        "pending": JobStatus.CANCELLED,
        "done": JobStatus.DONE,
        "other-collection": JobStatus.FAILED,
        "other-namespace": JobStatus.CANCELLED,
        "reindex": JobStatus.FAILED,
        "export": JobStatus.PENDING,
    }


def test_delete_collection_keeps_retries_when_the_drop_fails(tmp_path: Path, tmp_store: JobStore) -> None:
    """A failed table drop leaves the collection in place, so its retries are left as they are."""
    _, client, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    mock_search_store.drop_collection = AsyncMock(side_effect=RuntimeError("drop failed"))
    jobs = _seed_retry_candidates(tmp_store, name)

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 500
    assert _statuses(tmp_store, jobs) == {job_id: job.status for job_id, job in jobs.items()}


def test_delete_collection_succeeds_when_ending_retries_fails(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Ending the retries is auxiliary: a failed job-store write is logged at WARNING, the jobs keep their statuses
    and the DELETE still succeeds."""
    from tests._job_store_faults import failing_job_writes

    _, client, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    jobs = _seed_retry_candidates(tmp_store, name)

    with failing_job_writes(), caplog.at_level("WARNING", logger="archon_search.jobs.store"):
        response = client.delete(f"/collections/{name}")

    assert response.status_code == 200, response.text
    mock_search_store.delete_collection_meta.assert_awaited_once_with(name, "default")
    assert _statuses(tmp_store, jobs) == {job_id: job.status for job_id, job in jobs.items()}
    assert any(
        f"could not cancel the ingest jobs of dropped collection {name}" in r.getMessage() for r in caplog.records
    )


async def test_delete_collection_cancels_a_retry_parked_on_its_lock(tmp_path: Path, tmp_store: JobStore) -> None:
    """A real DELETE racing a maintenance retry over ONE shared collection lock: the DELETE holds the lock while it
    drops the table, the retry dispatched meanwhile parks on that lock still PENDING, the DELETE cancels it, and once
    the lock frees the retry's claim is refused — it ends CANCELLED and never ingests into the dropped collection."""
    import httpx

    from archon_search.server.routes_jobs import make_retry_dispatcher

    app, _, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    lock = asyncio.Lock()
    mock_search_store.lock_for = MagicMock(return_value=lock)
    dropping, finish_drop = asyncio.Event(), asyncio.Event()

    async def drop_collection(*_args: Any, **_kwargs: Any) -> None:
        dropping.set()
        await finish_drop.wait()

    mock_search_store.drop_collection = AsyncMock(side_effect=drop_collection)
    pipeline_fn = AsyncMock()
    app.state.ingest_pipeline = pipeline_fn
    app.state._background_tasks = set()
    retry = tmp_store.create(
        source="maintenance", path="/data/doc.md", collection=name,
        ingest_options=IngestOptions(submitted_path="/data/doc.md", ingested_by="http"),
    )

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {key}"}
    ) as client:
        delete_task = asyncio.create_task(client.delete(f"/collections/{name}"))
        async with reaping({delete_task}), reaping(app.state._background_tasks):
            await asyncio.wait_for(dropping.wait(), timeout=_ASYNC_STEP_TIMEOUT_S)
            assert lock.locked(), "precondition: DELETE holds the collection lock while it drops the table"
            make_retry_dispatcher(app.state)(retry)
            (retry_task,) = app.state._background_tasks
            await until_parked(lock)  # the retry parks on the lock DELETE holds
            finish_drop.set()
            response = await asyncio.wait_for(delete_task, timeout=_ASYNC_STEP_TIMEOUT_S)
            await asyncio.wait_for(retry_task, timeout=_ASYNC_STEP_TIMEOUT_S)

    assert response.status_code == 200, response.text
    final = tmp_store.get(retry.job_id)
    assert final is not None
    assert final.status == JobStatus.CANCELLED
    pipeline_fn.assert_not_awaited()
    assert not lock.locked()
    assert parked_waiters(lock) == 0


class _LockedJobRoute(NamedTuple):
    """A route that creates a job writing its collection, and records it on the meta row, under the collection lock
    (``create_collection_job``)."""

    path: str  # template with {name}
    body: dict[str, Any] | None
    not_found_detail: str  # template with {name}
    conflict_detail: str
    pointer_field: str
    create_method: str  # the JobStore method creating the route's job
    log_context: str
    job_kind: type[IngestJob]  # the kind of the route's job


_LOCKED_JOB_ROUTES = [
    pytest.param(
        _LockedJobRoute(
            "/collections/{name}/reindex", None, "Collection {name!r} not found", "reindex already in progress",
            "reindex_job_id", "create_reindex", "reindex_collection", ReindexJob,
        ),
        id="reindex",
    ),
    pytest.param(
        _LockedJobRoute(
            "/collections/{name}/reindex-metadata", {}, "Collection {name!r} not found",
            "metadata reindex already in progress for this collection", "metadata_reindex_job_id",
            "create_metadata_reindex", "reindex_metadata", MetadataReindexJob,
        ),
        id="reindex-metadata",
    ),
    pytest.param(
        _LockedJobRoute(
            "/graph/{name}/rebuild-communities", None, "collection not found",
            "community rebuild already in progress for this collection", "community_rebuild_job_id",
            "create_community_rebuild", "rebuild_communities", CommunityRebuildJob,
        ),
        id="rebuild-communities",
    ),
]


def _locked_job_routes_app(
    tmp_path: Path, tmp_store: JobStore
) -> tuple[Any, MagicMock, dict[str, CollectionMeta], asyncio.Lock, str]:
    """App wired for every ``_LOCKED_JOB_ROUTES`` route on one collection: its meta row lives in a dict and each read
    returns a copy (only a real meta write changes what a later read sees), and its collection lock is a real one,
    which a meta write takes unless its caller holds it (as ``SearchStore.update_collection_meta`` does).

    Returns (app, search store, meta rows by name, collection lock, collection name).
    """
    app, _, _, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    mock_store, meta_rows = _stateful_meta_store([CollectionMeta(name=name, namespace="default")])
    lock = asyncio.Lock()
    mock_store.lock_for = MagicMock(return_value=lock)
    read_row = mock_store.get_collection_meta.side_effect
    write_row = mock_store.update_collection_meta.side_effect

    async def read_copy(collection: str, namespace: str = "default") -> CollectionMeta | None:
        meta = await read_row(collection, namespace)
        return None if meta is None else dataclasses.replace(meta)

    async def locked_write(meta: CollectionMeta, *, _locked_by_caller: bool = False) -> None:
        if _locked_by_caller:
            if not lock.locked():  # as SearchStore.update_collection_meta refuses it
                raise CollectionLockNotHeldError(f"lock_for({meta.name!r}) is not held")
            await write_row(meta)
            return
        async with lock:
            await write_row(meta)

    mock_store.get_collection_meta = AsyncMock(side_effect=read_copy)
    mock_store.update_collection_meta = AsyncMock(side_effect=locked_write)
    mock_store.reindex_metadata = AsyncMock()
    app.state.search_store = mock_store
    app.state.embedder_cache = MagicMock()
    app.state.pipeline = MagicMock()
    app.state.config.graph.enabled = True
    app.state.graph_store = MagicMock()
    app.state.enrichment_client = None
    app.state._background_tasks = set()
    return app, mock_store, meta_rows, lock, name


def _asgi_client(app: Any) -> Any:
    """An authed httpx client calling *app* in this event loop, so a test can interleave concurrent requests."""
    import httpx

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {key}"}
    )


async def _never_finishing_job_task(*_args: Any, **_kwargs: Any) -> None:
    """Stands in for a job route's background task: it never ends the job, so the job stays active until reaped."""
    await asyncio.Event().wait()


def _park_job_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the background task of every ``_LOCKED_JOB_ROUTES`` route by ``_never_finishing_job_task``."""
    from archon_search.server import routes_collections, routes_graph

    monkeypatch.setattr(routes_collections, "_reindex_task", _never_finishing_job_task)
    monkeypatch.setattr(routes_collections, "_reindex_metadata_task", _never_finishing_job_task)
    monkeypatch.setattr(routes_graph, "_community_rebuild_task", _never_finishing_job_task)


@pytest.mark.parametrize("route", _LOCKED_JOB_ROUTES)
async def test_job_route_racing_delete_creates_no_job_and_no_meta_row(
    tmp_path: Path, tmp_store: JobStore, route: _LockedJobRoute
) -> None:
    """A job route whose collection a DELETE is dropping must not create its job or re-insert the meta row.

    The route passes its guards (config entry, meta row) before DELETE starts, and goes on while DELETE, holding the
    collection lock, drops the table. Unless the route creates its job under that lock after re-reading the row, it
    creates a job DELETE never saw and upserts its meta pointer, re-inserting the row of the dropped collection.
    The route releases the lock on its 404 too, and then discards the lock entry: DELETE's own discard kept it, as
    the route was waiting on it (``SearchStore.discard_lock``), so without the route's the entry would leak.
    """
    app, mock_store, meta_rows, lock, name = _locked_job_routes_app(tmp_path, tmp_store)
    dropping, finish_drop = asyncio.Event(), asyncio.Event()
    route_read, resume_route_read = asyncio.Event(), asyncio.Event()
    read_meta = mock_store.get_collection_meta.side_effect

    async def get_collection_meta(collection: str, namespace: str = "default") -> CollectionMeta | None:
        meta = await read_meta(collection, namespace)
        if not route_read.is_set():  # the route's guard read (the first read): it then waits for DELETE to start
            route_read.set()
            await resume_route_read.wait()
        return meta

    async def drop_collection(*_args: Any, **_kwargs: Any) -> None:
        dropping.set()
        await finish_drop.wait()

    mock_store.get_collection_meta = AsyncMock(side_effect=get_collection_meta)
    mock_store.drop_collection = AsyncMock(side_effect=drop_collection)
    discards: list[tuple[str, bool]] = []  # (collection, lock still held) per discard_lock call
    mock_store.discard_lock = MagicMock(side_effect=lambda collection: discards.append((collection, lock.locked())))

    async with _asgi_client(app) as client:
        route_task = asyncio.create_task(client.post(route.path.format(name=name), json=route.body))
        tasks = {route_task}
        async with reaping(tasks), reaping(app.state._background_tasks):
            await asyncio.wait_for(route_read.wait(), timeout=_ASYNC_STEP_TIMEOUT_S)
            delete_task = asyncio.create_task(client.delete(f"/collections/{name}"))
            tasks.add(delete_task)
            await asyncio.wait_for(dropping.wait(), timeout=_ASYNC_STEP_TIMEOUT_S)
            resume_route_read.set()
            # The route got the still-present row, and must now park on the lock DELETE holds.
            await until_parked(lock)
            assert not route_task.done(), "the route answered while DELETE held the collection lock"
            finish_drop.set()
            delete_response = await asyncio.wait_for(delete_task, timeout=_ASYNC_STEP_TIMEOUT_S)
            response = await asyncio.wait_for(route_task, timeout=_ASYNC_STEP_TIMEOUT_S)

    assert delete_response.status_code == 200, delete_response.text
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": route.not_found_detail.format(name=name)}
    assert tmp_store.list() == [], "the route created a job on the dropped collection"
    assert name not in meta_rows, "the dropped collection's meta row was re-inserted"
    mock_store.update_collection_meta.assert_not_awaited()
    assert not lock.locked(), "the route kept the collection lock after its 404"
    assert discards == [(name, False), (name, False)], "DELETE's then the route's discard, each after its release"


@pytest.mark.parametrize("route", _LOCKED_JOB_ROUTES)
async def test_job_route_concurrent_duplicates_create_one_job(
    tmp_path: Path, tmp_store: JobStore, monkeypatch: pytest.MonkeyPatch, route: _LockedJobRoute
) -> None:
    """Of two concurrent requests whose unlocked reads both saw no active job, exactly one creates its job.

    Both queue on the collection lock past their unlocked guards. The duplicate guard runs again under the lock, on
    a meta row re-read there — and the first request records its job's pointer before it releases the lock — so the
    second request answers 409 instead of creating a second job.
    """
    app, _, meta_rows, lock, name = _locked_job_routes_app(tmp_path, tmp_store)
    _park_job_tasks(monkeypatch)
    path = route.path.format(name=name)

    async with _asgi_client(app) as client, reaping(app.state._background_tasks):
        await lock.acquire()  # held by "another write" until both requests wait for it
        requests = [asyncio.create_task(client.post(path, json=route.body)) for _ in range(2)]
        async with reaping(set(requests)):
            await until_parked(lock, waiters=len(requests))
            lock.release()
            responses = await asyncio.wait_for(asyncio.gather(*requests), timeout=_ASYNC_STEP_TIMEOUT_S)

    assert sorted(r.status_code for r in responses) == [202, 409], [r.text for r in responses]
    [conflict] = [r for r in responses if r.status_code == 409]
    assert conflict.json() == {"detail": route.conflict_detail}
    [job] = tmp_store.list()
    assert getattr(meta_rows[name], route.pointer_field) == job.job_id
    assert not lock.locked()


@pytest.mark.parametrize("route", _LOCKED_JOB_ROUTES)
async def test_job_route_lock_timeout_answers_store_busy_and_creates_nothing(
    tmp_path: Path, tmp_store: JobStore, monkeypatch: pytest.MonkeyPatch, route: _LockedJobRoute
) -> None:
    """While another write holds the collection lock past the wait bound, the route answers the lock helper's 503
    ``store_busy`` with ``Retry-After``, and neither creates a job nor writes a meta pointer."""
    lock_timeout_s = 0.05
    monkeypatch.setattr("archon_search.constants.INGEST_LOCK_TIMEOUT_S", lock_timeout_s)
    app, mock_store, meta_rows, lock, name = _locked_job_routes_app(tmp_path, tmp_store)

    await lock.acquire()
    try:
        async with _asgi_client(app) as client:
            response = await asyncio.wait_for(
                client.post(route.path.format(name=name), json=route.body), timeout=_ASYNC_STEP_TIMEOUT_S
            )
    finally:
        lock.release()

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "reindex in progress; retry after Retry-After seconds", "code": STORE_BUSY_CODE}
    assert response.headers.get("Retry-After") == str(math.ceil(lock_timeout_s))
    assert tmp_store.list() == []
    mock_store.update_collection_meta.assert_not_awaited()
    assert getattr(meta_rows[name], route.pointer_field) is None


@pytest.mark.parametrize("error_type", [OSError, TypeError], ids=["oserror", "non-oserror"])
@pytest.mark.parametrize("route", _LOCKED_JOB_ROUTES)
async def test_job_route_create_failure_answers_sanitized_500_and_releases_the_lock(
    tmp_path: Path,
    tmp_store: JobStore,
    caplog: pytest.LogCaptureFixture,
    route: _LockedJobRoute,
    error_type: type[Exception],
) -> None:
    """Job creation raising anything is logged, answered with the sanitized 500 (no exception text), leaves no job
    and no meta pointer, and releases the collection lock."""
    app, mock_store, meta_rows, lock, name = _locked_job_routes_app(tmp_path, tmp_store)
    create_error = error_type(_SECRET_EXC_TEXT)

    with (
        patch.object(tmp_store, route.create_method, side_effect=create_error),
        caplog.at_level("ERROR", logger="archon_search.server._collection_job"),
    ):
        async with _asgi_client(app) as client:
            response = await asyncio.wait_for(
                client.post(route.path.format(name=name), json=route.body), timeout=_ASYNC_STEP_TIMEOUT_S
            )

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert _SECRET_EXC_TEXT not in response.text
    [record] = [
        r for r in caplog.records
        if r.getMessage() == f"{route.log_context}: failed to create the job for collection {name!r}"
    ]
    assert record.exc_info is not None and record.exc_info[1] is create_error
    assert tmp_store.list() == []
    mock_store.update_collection_meta.assert_not_awaited()
    assert getattr(meta_rows[name], route.pointer_field) is None
    assert not lock.locked()


@pytest.mark.parametrize("route", _LOCKED_JOB_ROUTES)
async def test_job_route_duplicate_answers_409_at_once_while_another_write_holds_the_lock(
    tmp_path: Path, tmp_store: JobStore, route: _LockedJobRoute
) -> None:
    """A duplicate of an active job is refused on the route's first, unlocked meta read: an instant 409, even while
    another write holds the collection lock (``SearchStore.reindex_metadata`` holds it for its whole run) — the
    request never queues on the lock, to time out with 503 ``store_busy`` instead."""
    app, mock_store, meta_rows, lock, name = _locked_job_routes_app(tmp_path, tmp_store)
    active = tmp_store.create_job(_job_on(route.job_kind, "active-job", JobStatus.RUNNING)(name))
    meta_rows[name] = dataclasses.replace(meta_rows[name], **{route.pointer_field: active.job_id})

    await lock.acquire()  # held by another write past this test's step bound (INGEST_LOCK_TIMEOUT_S is 30 s)
    try:
        async with _asgi_client(app) as client:
            response = await asyncio.wait_for(
                client.post(route.path.format(name=name), json=route.body), timeout=_ASYNC_STEP_TIMEOUT_S
            )
        assert parked_waiters(lock) == 0, "the duplicate queued on the collection lock"
    finally:
        lock.release()

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": route.conflict_detail}
    assert tmp_store.list() == [active]
    mock_store.update_collection_meta.assert_not_awaited()


@pytest.mark.parametrize("route", [route for route in _LOCKED_JOB_ROUTES if route.id != "reindex"])
async def test_job_route_pointer_write_failure_is_best_effort(
    tmp_path: Path,
    tmp_store: JobStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    route: _LockedJobRoute,
) -> None:
    """POST /collections/{name}/reindex-metadata and /graph/{name}/rebuild-communities record their job's meta
    pointer best-effort: a failed write is logged at WARNING with its traceback (never on the wire), the job is run
    anyway — the route answers 202 with it RUNNING — and the collection lock is released."""
    app, mock_store, meta_rows, lock, name = _locked_job_routes_app(tmp_path, tmp_store)
    _park_job_tasks(monkeypatch)
    write_error = OSError(_SECRET_EXC_TEXT)
    mock_store.update_collection_meta = AsyncMock(side_effect=write_error)

    with caplog.at_level("WARNING", logger="archon_search.server._collection_job"):
        async with _asgi_client(app) as client, reaping(app.state._background_tasks):
            response = await asyncio.wait_for(
                client.post(route.path.format(name=name), json=route.body), timeout=_ASYNC_STEP_TIMEOUT_S
            )

    assert response.status_code == 202, response.text
    assert _SECRET_EXC_TEXT not in response.text
    [job] = tmp_store.list()
    assert (response.json()["job_id"], job.status) == (job.job_id, JobStatus.RUNNING)
    assert getattr(meta_rows[name], route.pointer_field) is None
    [record] = [
        r for r in caplog.records
        if r.getMessage() == f"{route.log_context}: failed to persist {route.pointer_field} for job {job.job_id}"
    ]
    assert record.levelname == "WARNING"
    assert record.exc_info is not None and record.exc_info[1] is write_error
    assert not lock.locked()


@pytest.mark.parametrize("route", _LOCKED_JOB_ROUTES)
async def test_job_route_pointer_write_without_the_lock_raises_instead_of_answering(
    tmp_path: Path, tmp_store: JobStore, monkeypatch: pytest.MonkeyPatch, route: _LockedJobRoute
) -> None:
    """``CollectionLockNotHeldError`` from the pointer write is a programming error (the route claims a lock it does
    not hold): it escapes the route instead of being logged as a failed write (and answered 202 or 500), the job it
    created — which no task will run — ends FAILED with the sanitized ``internal error`` (else it would block
    DELETE /collections/{name} until a restart), and the collection lock is still released."""
    app, mock_store, _, lock, name = _locked_job_routes_app(tmp_path, tmp_store)
    _park_job_tasks(monkeypatch)
    lock_error = CollectionLockNotHeldError(f"lock_for({name!r}) is not held")
    mock_store.update_collection_meta = AsyncMock(side_effect=lock_error)

    async with _asgi_client(app) as client, reaping(app.state._background_tasks):
        with pytest.raises(CollectionLockNotHeldError) as raised:
            await asyncio.wait_for(client.post(route.path.format(name=name), json=route.body), timeout=_ASYNC_STEP_TIMEOUT_S)

    assert raised.value is lock_error
    [job] = tmp_store.list()
    assert (job.status, job.error) == (JobStatus.FAILED, "internal error")
    assert tmp_store.find_active_collection_job(name, "default") is None
    assert not lock.locked()


@pytest.mark.parametrize("route", _LOCKED_JOB_ROUTES)
async def test_job_route_without_a_search_store_answers_503_service_not_ready(
    tmp_path: Path, tmp_store: JobStore, route: _LockedJobRoute
) -> None:
    """Without a search store the route cannot read the meta row, take the collection lock or run its job: it
    answers the readiness 503 (never an unhandled 500) and creates no job."""
    app, _, _, _, name = _locked_job_routes_app(tmp_path, tmp_store)
    app.state.search_store = None

    async with _asgi_client(app) as client:
        response = await asyncio.wait_for(
            client.post(route.path.format(name=name), json=route.body), timeout=_ASYNC_STEP_TIMEOUT_S
        )

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "service not ready"}
    assert tmp_store.list() == []


def test_reindex_metadata_without_a_search_store_still_404s_a_name_absent_from_the_config(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """The config check runs before the readiness check: a name absent from the config can never be
    metadata-reindexed, so it answers 404 — not a 503 inviting a retry that would 404 once the server is ready."""
    c, name, _, _ = _make_reindex_metadata_app(tmp_path, tmp_store, in_config=False)
    c.app.state.search_store = None

    response = c.post(f"/collections/{name}/reindex-metadata", json={})

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": f"Collection {name!r} not found"}
    assert tmp_store.list() == []


_GRAPH_DISABLED_DETAIL = "graph inspection requires [graph] enabled=true in server config"


@pytest.mark.parametrize(
    ("break_request", "query", "expected_detail"),
    [
        pytest.param(
            lambda app: setattr(app.state.config.graph, "enabled", False), "", _GRAPH_DISABLED_DETAIL,
            id="graph-disabled",
        ),
        pytest.param(lambda app: setattr(app.state, "graph_store", None), "", _GRAPH_DISABLED_DETAIL, id="no-graph-store"),
        pytest.param(
            lambda _app: None, "?namespace=other",
            "namespace mismatch: token authorises 'default', but ?namespace='other' was requested",
            id="namespace-mismatch",
        ),
    ],
)
async def test_rebuild_communities_without_a_search_store_still_answers_its_422(
    tmp_path: Path,
    tmp_store: JobStore,
    break_request: Callable[[Any], None],
    query: str,
    expected_detail: str,
) -> None:
    """The 422 guards (``?namespace=`` mismatch, graph disabled or not connected) run before the readiness check: a
    request refused for them is answered 422 — not a 503 inviting a retry that would get the same 422 once the
    server is ready."""
    app, _, _, _, name = _locked_job_routes_app(tmp_path, tmp_store)
    app.state.search_store = None
    break_request(app)

    async with _asgi_client(app) as client:
        response = await asyncio.wait_for(
            client.post(f"/graph/{name}/rebuild-communities{query}"), timeout=_ASYNC_STEP_TIMEOUT_S
        )

    assert response.status_code == 422, response.text
    assert response.json() == {"detail": expected_detail}
    assert tmp_store.list() == []


class _RestartEffect(NamedTuple):
    """What a request proceeding past a stranded reindex leaves behind (see the restart test below)."""

    job_store: JobStore
    meta_rows: dict[str, CollectionMeta]
    search_store: MagicMock
    response: Any
    name: str
    stranded_job_id: str


def _new_reindex_overwrites_the_stranded_pointer(effect: _RestartEffect) -> None:
    new_job = effect.job_store.get(effect.response.json()["job_id"])
    assert isinstance(new_job, ReindexJob) and new_job.status == JobStatus.PENDING
    assert effect.meta_rows[effect.name].reindex_job_id == new_job.job_id != effect.stranded_job_id


def _model_change_clears_the_stranded_pointer(effect: _RestartEffect) -> None:
    meta = effect.meta_rows[effect.name]
    assert (meta.pending_embedding_model, meta.needs_reindex, meta.reindex_job_id) == ("model-B", True, None)


def _rewrite_migration_job_is_created(effect: _RestartEffect) -> None:
    [migration] = [job for job in effect.job_store.list() if isinstance(job, MigrationJob)]
    assert (migration.collection, migration.status) == (effect.name, JobStatus.RUNNING)


def _delete_drops_the_collection(effect: _RestartEffect) -> None:
    effect.search_store.drop_collection.assert_awaited_once_with(effect.name, _locked_by_caller=True)
    assert effect.name not in effect.meta_rows


@pytest.mark.parametrize(
    ("method", "path", "body", "expected_status", "assert_effect"),
    [
        pytest.param(
            "POST", "/collections/{name}/reindex", None, 202, _new_reindex_overwrites_the_stranded_pointer, id="reindex"
        ),
        pytest.param(
            "PATCH", "/collections/{name}", {"embedding_model": "model-B"}, 200, _model_change_clears_the_stranded_pointer,
            id="patch-embedding-model",
        ),
        pytest.param(
            "POST", "/collections/{name}/migrate", {"backup_confirmed": True}, 202, _rewrite_migration_job_is_created,
            id="rewrite-migration",
        ),
        pytest.param("DELETE", "/collections/{name}", None, 200, _delete_drops_the_collection, id="delete"),
    ],
)
def test_reindex_left_pending_by_a_restart_blocks_nothing(
    tmp_path: Path,
    tmp_store: JobStore,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    expected_status: int,
    assert_effect: Callable[[_RestartEffect], None],
) -> None:
    """A reindex job whose server died before its task ran it is still PENDING in the jobs file, and the meta row
    still points at it. The next server fails it at load (never started), so neither its meta pointer nor the job
    itself refuses anything: a new reindex (its id overwrites the stale pointer), an embedding-model change, a rewrite
    migration and DELETE (the collection is dropped) all proceed and take effect."""
    from archon_search.jobs.store import NOT_STARTED_BEFORE_RESTART_DETAIL
    from archon_search.types import MigrationSpec

    src = tmp_path / "docs"
    src.mkdir()
    name = path_to_collection_name(str(src))
    stranded = tmp_store.create_reindex(name)  # the previous process: its task never ran the job
    job_store = JobStore(path=tmp_path / "jobs.json")  # this process loads the jobs file

    reloaded = job_store.get(stranded.job_id)
    assert reloaded is not None
    assert (reloaded.status, reloaded.error) == (JobStatus.FAILED, NOT_STARTED_BEFORE_RESTART_DETAIL)
    mock_store, meta_rows = _stateful_meta_store(
        [CollectionMeta(name=name, namespace="default", active_embedding_model="model-A", reindex_job_id=stranded.job_id)]
    )
    mock_store.count_chunks = AsyncMock(return_value=3)
    mock_store.count_documents = AsyncMock(return_value=1)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.pending_migrations = AsyncMock(
        return_value=[MigrationSpec(name="rewrite_chunks", kind=MigrationKind.REWRITE, description="d", introduced_at=1)]
    )
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, job_store)
    app.state.search_store = mock_store
    app.state.embedder_cache = MagicMock()
    app.state.pipeline = MagicMock()

    with (
        patch("archon_search.server.routes_collections.validate_embedding_model", new=AsyncMock()),
        patch("archon_search.server.routes_collections.asyncio.create_task",
              side_effect=lambda coro: (coro.close(), MagicMock())[1]),
    ):
        response = _authed_client(app).request(method, path.format(name=name), json=body)

    assert response.status_code == expected_status, response.text
    assert_effect(_RestartEffect(job_store, meta_rows, mock_store, response, name, stranded.job_id))


def test_delete_collection_200_without_job_store(tmp_path: Path, tmp_store: JobStore) -> None:
    """With no job store there is nothing to consult, so DELETE drops the collection."""
    app, client, mock_search_store, name, _ = _ingest_guard_app(tmp_path, tmp_store)
    app.state.job_store = None

    response = client.delete(f"/collections/{name}")

    assert response.status_code == 200, f"Expected 200, got {response.status_code}; body={response.text}"
    assert response.json() == {"name": name, "deleted": True}
    mock_search_store.drop_collection.assert_awaited_once()
    mock_search_store.delete_collection_meta.assert_awaited_once_with(name, "default")
    assert app.state.config.collections == []


class _WaiterSignallingLock(asyncio.Lock):
    """asyncio.Lock that sets ``waiting`` once a caller blocks on the already-held lock."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.waiting.set()
        return await super().acquire()


async def test_delete_collection_503_when_ingest_turns_running_while_delete_waits_for_lock(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """The active-ingest guard must run UNDER the collection lock, not before it (TOCTOU).

    POST /ingest pre-acquires the collection lock; the ingest task marks its job RUNNING and,
    for a path ingest, releases the lock early before dispatch. A DELETE that checked the job
    store before waiting for that lock sees no active job, then acquires the released lock and
    drops the table + config while the ingest is mid-write.
    """
    import httpx

    src = tmp_path / "toctou_col"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    name = path_to_collection_name(str(src))
    ns = "default"

    mock_store, meta_rows = _stateful_meta_store([CollectionMeta(name=name, namespace=ns)])
    lock = _WaiterSignallingLock()
    mock_store.lock_for = MagicMock(return_value=lock)
    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    # The in-flight ingest holds the collection lock and has created its PENDING job, as POST /ingest
    # pre-acquires the lock before creating the job.
    await lock.acquire()
    test_holds_lock = True
    lock.waiting.clear()
    job_id = "toctou-running-ingest"
    tmp_store.create_job(_ingest_job(job_id, JobStatus.PENDING, name, namespace=ns))

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {key}"},
    ) as client:
        delete_task = asyncio.create_task(client.delete(f"/collections/{name}"))
        waiting_task = asyncio.create_task(lock.waiting.wait())
        try:
            await asyncio.wait({delete_task, waiting_task}, timeout=_ASYNC_STEP_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
            if delete_task.done():
                early = delete_task.result()
                pytest.fail(f"DELETE finished before the ingest released the lock: {early.status_code} {early.text}")
            assert waiting_task.done(), "DELETE never blocked on the held collection lock"

            # While DELETE waits for the lock, the ingest task marks its job RUNNING and only then releases
            # the lock early before dispatching its writes (_default_ingest_task_with_lock).
            tmp_store.update(job_id, status=JobStatus.RUNNING)
            test_holds_lock = False
            lock.release()

            response = await asyncio.wait_for(delete_task, timeout=_ASYNC_STEP_TIMEOUT_S)
        finally:
            # On failure: stop both tasks before freeing the lock, so a still-waiting DELETE cannot run on.
            delete_task.cancel()
            waiting_task.cancel()
            if test_holds_lock:
                lock.release()
            await asyncio.gather(delete_task, waiting_task, return_exceptions=True)

    assert response.status_code == 503, f"DELETE dropped a collection mid-ingest: {response.status_code} {response.text}"
    assert response.json() == {
        "detail": COLLECTION_JOB_IN_PROGRESS_DETAIL_TEMPLATE.format(job_id=job_id),
        "code": STORE_BUSY_CODE,
    }
    assert response.headers.get("Retry-After") == str(math.ceil(INGEST_LOCK_TIMEOUT_S))
    # The collection survives: table, meta row and config entry all intact; the lock is released and kept.
    mock_store.drop_collection.assert_not_awaited()
    mock_store.delete_collection_meta.assert_not_awaited()
    mock_store.discard_lock.assert_not_called()
    assert name in meta_rows
    assert app.state.config.collections == [str(src)]
    assert not lock.locked()


# ---------------------------------------------------------------------------
# GET /collections/{name} — namespace enforcement
# ---------------------------------------------------------------------------


def test_get_collection_info_cross_namespace_404(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns 404 when meta row belongs to a different namespace."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    caller_key = "a" * 64
    cfg.namespaces = {caller_key: "tenantA"}

    app = create_app(cfg, tmp_store)
    # meta row belongs to tenantB — namespace filter returns None for tenantA caller
    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})
    name = path_to_collection_name(str(src))

    response = c.get(f"/collections/{name}")
    assert response.status_code == 404


def test_get_collection_info_namespace_in_response(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} response includes the actual namespace from meta (not hardcoded default)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    caller_key = "b" * 64
    caller_ns = "tenantB"
    cfg.namespaces = {caller_key: caller_ns}

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace=caller_ns)

    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})
    response = c.get(f"/collections/{name}")

    assert response.status_code == 200
    data = response.json()
    assert data["namespace"] == caller_ns


def test_get_collection_info_centroid_from_namespace_meta(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns centroid_present=True using the namespace-filtered meta (no second bare lookup)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    caller_key = "c" * 64
    caller_ns = "tenantC"
    cfg.namespaces = {caller_key: caller_ns}

    name = path_to_collection_name(str(src))
    # meta has a centroid set — a bare get_collection_meta(name) (no namespace)
    # would return None after (defaults to DEFAULT_NAMESPACE), so
    # centroid_present would be False if the handler did a second bare lookup.
    meta = CollectionMeta(name=name, namespace=caller_ns, centroid=[0.1, 0.2, 0.3])

    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=5)
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})
    response = c.get(f"/collections/{name}")

    assert response.status_code == 200
    data = response.json()
    assert data["centroid_present"] is True
    # get_collection_meta should have been called exactly once (namespace-gated call),
    # not twice (once for namespace check + once bare).
    mock_store.get_collection_meta.assert_called_once()


# ---------------------------------------------------------------------------
# POST /collections/{name}/reindex — namespace enforcement 
# ---------------------------------------------------------------------------


def test_reindex_cross_namespace_404(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/reindex returns 404 when meta row belongs to a different namespace."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    caller_key = "r" * 64
    cfg.namespaces = {caller_key: "tenantR"}

    # meta row belongs to tenantOther — namespace filter returns None for tenantR
    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store
    app.state.embedder_cache = MagicMock()  # a ready service: readiness is checked before the 404
    app.state.pipeline = MagicMock()

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})
    name = path_to_collection_name(str(src))

    response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 404


def test_reindex_same_namespace_succeeds(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/reindex returns 202 when meta row belongs to caller's namespace."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    caller_key = "s" * 64
    caller_ns = "tenantS"
    cfg.namespaces = {caller_key: caller_ns}

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace=caller_ns)

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=asyncio.Lock())

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store
    app.state.embedder_cache = MagicMock()
    app.state.pipeline = MagicMock()

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.PENDING.value


def test_reindex_job_namespace(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/reindex creates a job with the caller's namespace."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    caller_key = "t" * 64
    caller_ns = "tenantT"
    cfg.namespaces = {caller_key: caller_ns}

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace=caller_ns)

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=asyncio.Lock())

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store
    app.state.embedder_cache = MagicMock()
    app.state.pipeline = MagicMock()

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 202
    data = response.json()
    assert data["namespace"] == caller_ns

    # Also verify the job in the store has the correct namespace
    job = tmp_store.get(data["job_id"])
    assert job is not None
    assert job.namespace == caller_ns


def test_add_collection_rollback_save_failure(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ returns 500 when both update_collection_meta AND rollback _maybe_save_config raise."""
    import asyncio as _asyncio

    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    # Set a config_path so _maybe_save_config is invoked
    cfg_file = tmp_path / "search.toml"

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock(side_effect=RuntimeError("disk error"))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    # Provide a real lock so the pre-acquire (moved before state mutation) can succeed.
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    # Inject config_path so _maybe_save_config is called during rollback
    app.state.config_path = str(cfg_file)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    call_count = 0

    def failing_save(config: object, config_path: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise OSError("disk full")

    with patch("archon_search.server.routes_collections.save_config", side_effect=failing_save):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert app.state.config.collections == []


@pytest.mark.parametrize(
    "persist_error",
    [
        PermissionError(13, "Permission denied", "/very/secret/internal/path"),
        TypeError("unserialisable value"),
        ValueError("hand-corrupted TOML"),
    ],
    ids=["read-only-config", "type-error", "parse-error"],
)
def test_add_collection_persist_failure_rolls_back_with_sanitized_500(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture, persist_error: Exception
) -> None:
    """save_config raising (PermissionError from a read-only config, TypeError, or tomlkit ParseError,
    a ValueError) → sanitized 500, in-memory append reverted, no meta written or job started."""
    from archon_search.server.routes_collections import (
        CONFIG_PERSIST_FAILED_CODE,
        CONFIG_PERSIST_FAILED_DETAIL,
    )

    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    app.state.config_path = str(tmp_path / "archon-search.toml")
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"}, raise_server_exceptions=False)

    with (
        patch("archon_search.server.routes_collections.save_config", side_effect=persist_error),
        caplog.at_level("ERROR", logger="archon_search.server.routes_collections"),
    ):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 500
    assert response.json() == {"detail": CONFIG_PERSIST_FAILED_DETAIL, "code": CONFIG_PERSIST_FAILED_CODE}
    [record] = [r for r in caplog.records if r.getMessage().startswith("Failed to persist collection change")]
    assert record.levelname == "ERROR"
    assert record.exc_info is not None and record.exc_info[1] is persist_error
    assert app.state.config.collections == []
    mock_store.update_collection_meta.assert_not_awaited()
    assert tmp_store.list() == []


def _remove_app(
    tmp_path: Path, tmp_store: JobStore, *, with_meta: bool
) -> tuple[object, MagicMock, str, list[str], list[str]]:
    """App whose config registers one collection in both lists, with or without a store meta row."""
    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    cfg.pinned_collections = [str(src)]
    name = path_to_collection_name(str(src))

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name=name, namespace="default") if with_meta else None
    )
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.drop_collection = AsyncMock()
    mock_store.delete_collection_meta = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=asyncio.Lock())

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    app.state.config_path = str(tmp_path / "archon-search.toml")
    return app, mock_store, name, list(cfg.collections), list(cfg.pinned_collections)


@pytest.mark.parametrize("with_meta", [False, True], ids=["config-only", "with-meta"])
@pytest.mark.parametrize(
    "persist_error",
    [PermissionError(13, "Permission denied", "/very/secret/internal/path"), TypeError("bad value")],
    ids=["oserror", "type-error"],
)
def test_remove_collection_persist_failure_rolls_back_with_sanitized_500(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture, with_meta: bool, persist_error: Exception
) -> None:
    """DELETE whose config save fails → sanitized 500, both config lists restored, store data untouched."""
    from archon_search.server.routes_collections import (
        CONFIG_PERSIST_FAILED_CODE,
        CONFIG_PERSIST_FAILED_DETAIL,
    )

    app, mock_store, name, collections_before, pinned_before = _remove_app(
        tmp_path, tmp_store, with_meta=with_meta
    )
    tmp_store.create_job(_ingest_job("failed-ingest", JobStatus.FAILED, name))
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"}, raise_server_exceptions=False)

    with (
        patch("archon_search.server.routes_collections.save_config", side_effect=persist_error),
        caplog.at_level("ERROR", logger="archon_search.server.routes_collections"),
    ):
        response = c.delete(f"/collections/{name}")

    assert response.status_code == 500
    assert response.json() == {"detail": CONFIG_PERSIST_FAILED_DETAIL, "code": CONFIG_PERSIST_FAILED_CODE}
    [record] = [r for r in caplog.records if r.getMessage().startswith("Failed to persist collection change")]
    assert record.levelname == "ERROR"
    assert record.exc_info is not None and record.exc_info[1] is persist_error
    assert app.state.config.collections == collections_before
    assert app.state.config.pinned_collections == pinned_before
    mock_store.drop_collection.assert_not_awaited()
    mock_store.delete_collection_meta.assert_not_awaited()
    mock_store.discard_lock.assert_not_called()
    assert not mock_store.lock_for.return_value.locked()
    # The collection stays, so its failed ingest stays a retry candidate (a DELETE cancels it only once it succeeds).
    assert _statuses(tmp_store, ["failed-ingest"]) == {"failed-ingest": JobStatus.FAILED}


def test_remove_collection_rollback_keeps_config_change_made_while_waiting_for_lock(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """The DELETE rollback snapshot is taken under the collection lock: a path another
    request registers while DELETE waits for that lock survives a failed config save."""
    from archon_search.server import routes_collections

    app, _mock_store, name, collections_before, pinned_before = _remove_app(tmp_path, tmp_store, with_meta=True)
    added_while_waiting = str(tmp_path / "added-while-waiting")
    real_acquire = routes_collections.acquire_collection_lock_or_503

    async def _acquire_while_another_request_adds(search_store: object, collection: str) -> object:
        app.state.config.collections.append(added_while_waiting)
        return await real_acquire(search_store, collection)

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"}, raise_server_exceptions=False)
    with (
        patch.object(routes_collections, "acquire_collection_lock_or_503", new=_acquire_while_another_request_adds),
        patch.object(routes_collections, "save_config", side_effect=OSError("disk full")),
    ):
        response = c.delete(f"/collections/{name}")

    assert response.status_code == 500
    assert app.state.config.collections == [*collections_before, added_while_waiting]
    assert app.state.config.pinned_collections == pinned_before


_BUSY_BODY = {"detail": "store busy; retry in 30 seconds"}


@pytest.mark.parametrize(
    ("meta_error", "lock_busy", "expected_status", "expected_body"),
    [
        (StoreBusyError(timeout_s=30.0), False, 503, _BUSY_BODY),
        (ValueError("name taken"), False, 409, {"detail": "collection name already registered"}),
        (None, True, 503, _BUSY_BODY),
    ],
    ids=["store-busy-503", "name-race-409", "ingest-lock-503"],
)
def test_add_collection_error_status_preserved_when_rollback_save_fails(
    tmp_path: Path,
    tmp_store: JobStore,
    meta_error: Exception | None,
    lock_busy: bool,
    expected_status: int,
    expected_body: dict[str, str],
) -> None:
    """A StoreBusyError (503) or TOCTOU ValueError (409) on the stub meta write, or an ingest-lock timeout
    (503, stub meta deleted), keeps its status even when the rollback config save raises."""
    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock(side_effect=meta_error)
    mock_store.delete_collection_meta = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    app.state.config_path = str(tmp_path / "archon-search.toml")
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"}, raise_server_exceptions=False)

    calls = {"n": 0}

    def _save(config: object, config_path: object) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("disk full")

    busy_response = JSONResponse({"detail": "busy"}, status_code=503, headers={"Retry-After": "30"})
    acquire_patch = (
        patch(
            "archon_search.server.routes_collections.acquire_collection_lock_or_503",
            new_callable=AsyncMock,
            return_value=busy_response,
        )
        if lock_busy
        else nullcontext()
    )
    with acquire_patch, patch("archon_search.server.routes_collections.save_config", side_effect=_save):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == expected_status
    assert response.json() == expected_body
    if expected_status == 503:
        assert response.headers["Retry-After"] == "30"
    assert calls["n"] == 2
    assert app.state.config.collections == []
    assert mock_store.delete_collection_meta.await_count == (1 if lock_busy else 0)


_SECRET_EXC_TEXT = "/very/secret/internal/path/store.lance"


def _authed_client(app: object) -> TestClient:
    """TestClient sending the test API key; server exceptions surface as 500 responses."""
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"}, raise_server_exceptions=False)


def _stateful_meta_store(initial: list[CollectionMeta]) -> tuple[MagicMock, dict[str, CollectionMeta]]:
    """Mock search_store whose meta rows live in a dict, so a stub left behind stays observable."""
    rows = {m.name: m for m in initial}

    async def _update(meta: CollectionMeta, *, _locked_by_caller: bool = False) -> None:
        # As SearchStore.update_collection_meta, a caller claiming the lock must hold it (read at call time: a test
        # may replace lock_for; return_value records no lock_for call).
        if _locked_by_caller and not mock_store.lock_for.return_value.locked():
            raise CollectionLockNotHeldError(f"lock_for({meta.name!r}) is not held")
        rows[meta.name] = meta

    async def _delete(name: str, namespace: str) -> None:
        rows.pop(name, None)

    async def _get(name: str, namespace: str = "default") -> CollectionMeta | None:
        meta = rows.get(name)
        return meta if meta is not None and meta.namespace == namespace else None

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(side_effect=lambda: list(rows.values()))
    mock_store.get_collection_meta = AsyncMock(side_effect=_get)
    mock_store.update_collection_meta = AsyncMock(side_effect=_update)
    mock_store.delete_collection_meta = AsyncMock(side_effect=_delete)
    mock_store.drop_collection = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=asyncio.Lock())
    return mock_store, rows


# (failure patch, whether the real JobStore.create runs — only then can a ghost job be left behind)
_JOB_CREATE_FAILURES = [
    pytest.param(
        lambda store: patch.object(store, "create", side_effect=OSError(28, "No space left on device", _SECRET_EXC_TEXT)),
        False,
        id="create-oserror",
    ),
    pytest.param(
        lambda store: patch.object(store, "create", side_effect=TypeError(_SECRET_EXC_TEXT)),
        False,
        id="create-non-oserror",
    ),
    pytest.param(
        lambda store: patch(
            "archon_search.jobs.store.atomic_write_json",
            side_effect=OSError(28, "No space left on device", _SECRET_EXC_TEXT),
        ),
        True,
        id="real-jobstore-write-oserror",
    ),
]


@pytest.mark.parametrize(("job_create_failure", "real_create_runs"), _JOB_CREATE_FAILURES)
def test_add_collection_job_create_failure_rolls_back_config_and_meta(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture, job_create_failure, real_create_runs: bool
) -> None:
    """JobStore.create raising (OSError, any other exception, or a real JobStore persist failure) after
    the config persist and stub meta write → sanitized 500, config restored in memory AND on disk, stub
    meta row deleted, no ghost job, lock released, and a retry of the same path is accepted (not 409
    "collection already registered")."""
    from archon_search.config import load_config, save_config

    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg_file = tmp_path / "archon-search.toml"
    save_config(cfg, cfg_file)
    name = path_to_collection_name(str(src.resolve()))

    mock_store, meta_rows = _stateful_meta_store([])
    lock = mock_store.lock_for.return_value
    held_at_discard: list[bool] = []
    mock_store.discard_lock.side_effect = lambda collection: held_at_discard.append(lock.locked())
    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    app.state.config_path = str(cfg_file)
    c = _authed_client(app)

    with job_create_failure(tmp_store), caplog.at_level("ERROR", logger="archon_search.server.routes_collections"):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert _SECRET_EXC_TEXT not in response.text
    [record] = [r for r in caplog.records if r.getMessage() == f"Failed to create the ingest job for collection {name}"]
    assert record.exc_info is not None and _SECRET_EXC_TEXT in str(record.exc_info[1])
    assert app.state.config.collections == []
    assert load_config(cfg_file).collections == []
    assert name not in meta_rows
    if real_create_runs:
        assert tmp_store.list() == []
    assert not lock.locked()
    # The rolled-back collection's lock entry is discarded — only after the release, mirroring DELETE.
    mock_store.discard_lock.assert_called_once_with(name)
    assert held_at_discard == [False]

    with patch(
        "archon_search.server.routes_collections.asyncio.create_task",
        side_effect=lambda coro: (coro.close(), MagicMock())[1],
    ):
        retry = c.post("/collections/", json={"path": str(src)})

    assert retry.status_code == 202, retry.text


def test_add_collection_rollback_meta_delete_failure_still_returns_500_and_releases_lock(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A delete_collection_meta failure inside the job-creation rollback is logged, never raised:
    the 500 internal-error response survives, the config entry is still rolled back, the lock released."""
    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    mock_store, _meta_rows = _stateful_meta_store([])
    mock_store.delete_collection_meta.side_effect = OSError(5, "Input/output error", _SECRET_EXC_TEXT)
    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    c = _authed_client(app)

    with (
        patch.object(tmp_store, "create", side_effect=OSError("disk full")),
        caplog.at_level("ERROR", logger="archon_search.server.routes_collections"),
    ):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert app.state.config.collections == []
    mock_store.delete_collection_meta.assert_awaited_once()
    assert any(r.getMessage() == "Failed to rollback meta after job creation failure" for r in caplog.records)
    assert not mock_store.lock_for.return_value.locked()


def _raise_after_concurrent_delete(app: object, resolved: str, error: Exception):
    """Side effect simulating a concurrent DELETE that already removed *resolved* from the config."""

    def _side_effect(*args: object, **kwargs: object) -> None:
        app.state.config.collections.remove(resolved)
        raise error

    return _side_effect


@pytest.mark.parametrize(
    ("failure_point", "expected_status", "expected_body", "expected_meta_deletes"),
    [
        ("stub-meta-store-busy", 503, _BUSY_BODY, 0),
        ("stub-meta-name-race", 409, {"detail": "collection name already registered"}, 0),
        ("stub-meta-other", 500, {"detail": "internal error"}, 0),
        ("ingest-lock-busy", 503, _BUSY_BODY, 1),
        ("job-create", 500, {"detail": "internal error"}, 1),
    ],
)
def test_add_collection_rollback_tolerates_entry_already_removed_by_concurrent_delete(
    tmp_path: Path,
    tmp_store: JobStore,
    failure_point: str,
    expected_status: int,
    expected_body: dict[str, str],
    expected_meta_deletes: int,
) -> None:
    """When a concurrent DELETE already removed the config entry, the rollback must not raise
    ValueError: the intended status/body is returned and the meta rollback is still attempted."""
    src = tmp_path / "myproject"
    src.mkdir()
    resolved = str(src.resolve())
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    mock_store, _meta_rows = _stateful_meta_store([])
    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    c = _authed_client(app)

    stub_meta_errors = {
        "stub-meta-store-busy": StoreBusyError(timeout_s=30.0),
        "stub-meta-name-race": ValueError("name taken"),
        "stub-meta-other": RuntimeError("disk error"),
    }
    busy_response = JSONResponse({"detail": "busy"}, status_code=503, headers={"Retry-After": "30"})
    if failure_point in stub_meta_errors:
        mock_store.update_collection_meta.side_effect = _raise_after_concurrent_delete(
            app, resolved, stub_meta_errors[failure_point]
        )
        failure_patch = nullcontext()
    elif failure_point == "ingest-lock-busy":

        async def _busy_after_concurrent_delete(*args: object, **kwargs: object) -> JSONResponse:
            app.state.config.collections.remove(resolved)
            return busy_response

        failure_patch = patch(
            "archon_search.server.routes_collections.acquire_collection_lock_or_503",
            new=_busy_after_concurrent_delete,
        )
    else:
        failure_patch = patch.object(
            tmp_store, "create", side_effect=_raise_after_concurrent_delete(app, resolved, OSError("disk full"))
        )

    with failure_patch:
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == expected_status
    assert response.json() == expected_body
    assert app.state.config.collections == []
    assert mock_store.delete_collection_meta.await_count == expected_meta_deletes


def test_add_collection_stub_meta_failure_is_logged(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """An unexpected stub-meta write failure is logged with its traceback before the 500."""
    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    meta_error = RuntimeError("disk error")
    mock_store, _meta_rows = _stateful_meta_store([])
    mock_store.update_collection_meta.side_effect = meta_error
    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    c = _authed_client(app)

    with caplog.at_level("ERROR", logger="archon_search.server.routes_collections"):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 500
    [record] = [r for r in caplog.records if r.getMessage().startswith("Failed to write the stub meta row")]
    assert record.exc_info is not None and record.exc_info[1] is meta_error


@pytest.mark.parametrize(
    ("stored", "re_added"),
    [("absolute", "absolute"), ("absolute", "tilde"), ("tilde", "absolute")],
    ids=["same-spelling", "re-added-as-tilde", "stored-as-tilde"],
)
def test_drop_path_from_config_restore_skips_an_entry_already_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: str, re_added: str
) -> None:
    """Defensive unit test of the restore callback: through the API a re-add of the dropped path cannot
    happen before restore runs (the meta row still exists, so POST /collections/ answers 409), but if an
    entry resolving to the same path is already in the list, restore skips it rather than duplicating it —
    matching by resolved path like the drop does, so ``~/x`` and its absolute spelling count as one entry.
    Entries still missing are re-inserted at their original index."""
    from archon_search.server.routes_collections import _drop_path_from_config

    monkeypatch.setenv("HOME", str(tmp_path))
    dropped = str((tmp_path / "dropped").resolve())
    kept = str((tmp_path / "kept").resolve())
    spelling = {"absolute": dropped, "tilde": "~/dropped"}
    cfg = SearchConfig()
    cfg.collections = [spelling[stored], kept]
    cfg.pinned_collections = [spelling[stored]]

    restore = _drop_path_from_config(cfg, dropped)
    assert cfg.collections == [kept]
    assert cfg.pinned_collections == []
    cfg.collections.append(spelling[re_added])

    restore()

    assert cfg.collections == [kept, spelling[re_added]]
    assert cfg.pinned_collections == [spelling[stored]]


def _drop_failed_body() -> dict[str, str]:
    from archon_search.server.routes_collections import COLLECTION_DROP_FAILED_CODE, COLLECTION_DROP_FAILED_DETAIL

    return {"detail": COLLECTION_DROP_FAILED_DETAIL, "code": COLLECTION_DROP_FAILED_CODE}


def test_remove_collection_drop_failure_restores_config(tmp_path: Path, tmp_store: JobStore) -> None:
    """drop_collection raising after the config was persisted → the table data is intact, so the config
    removal is rolled back (in memory AND on disk, both lists), the meta row is left alone, and the
    sanitized collection_drop_failed 500 is returned without the exception text; lock released."""
    from archon_search.config import load_config

    app, mock_store, name, collections_before, pinned_before = _remove_app(tmp_path, tmp_store, with_meta=True)
    mock_store.drop_collection.side_effect = OSError(5, "Input/output error", _SECRET_EXC_TEXT)

    response = _authed_client(app).delete(f"/collections/{name}")

    assert response.status_code == 500
    assert response.json() == _drop_failed_body()
    assert _SECRET_EXC_TEXT not in response.text
    assert app.state.config.collections == collections_before
    assert app.state.config.pinned_collections == pinned_before
    on_disk = load_config(Path(app.state.config_path))
    assert on_disk.collections == collections_before
    assert on_disk.pinned_collections == pinned_before
    mock_store.delete_collection_meta.assert_not_awaited()
    mock_store.discard_lock.assert_not_called()
    assert not mock_store.lock_for.return_value.locked()


def test_remove_collection_meta_delete_failure_keeps_config_removal(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """delete_collection_meta raising after drop_collection succeeded → the table is gone, so the config
    removal stays (in memory AND on disk) instead of re-registering a path without data; the sanitized
    collection_drop_failed 500 is still returned and the failure logged; lock released, then its entry
    discarded (the table is gone)."""
    from archon_search.config import load_config

    app, mock_store, name, _collections_before, _pinned_before = _remove_app(tmp_path, tmp_store, with_meta=True)
    meta_error = OSError(5, "Input/output error", _SECRET_EXC_TEXT)
    mock_store.delete_collection_meta.side_effect = meta_error
    lock = mock_store.lock_for.return_value
    held_at_discard: list[bool] = []
    mock_store.discard_lock.side_effect = lambda collection: held_at_discard.append(lock.locked())

    with caplog.at_level("ERROR", logger="archon_search.server.routes_collections"):
        response = _authed_client(app).delete(f"/collections/{name}")

    [record] = [r for r in caplog.records if r.getMessage().startswith("Failed to delete the meta row")]
    assert record.exc_info is not None and record.exc_info[1] is meta_error
    mock_store.discard_lock.assert_called_once_with(name)
    assert held_at_discard == [False]
    assert response.status_code == 500
    assert response.json() == _drop_failed_body()
    assert _SECRET_EXC_TEXT not in response.text
    mock_store.drop_collection.assert_awaited_once_with(name, _locked_by_caller=True)
    assert app.state.config.collections == []
    assert app.state.config.pinned_collections == []
    on_disk = load_config(Path(app.state.config_path))
    assert on_disk.collections == []
    assert on_disk.pinned_collections == []
    assert not mock_store.lock_for.return_value.locked()


def test_remove_collection_drop_runtime_error_restores_config_and_keeps_meta(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """drop_collection raising RuntimeError (store not connected, or a LanceDB failure) is a failed drop,
    NOT "table missing": the config removal is rolled back, the meta row is kept (no orphaned table), the
    failure is logged, and the sanitized collection_drop_failed 500 is returned; lock entry not discarded."""
    from archon_search.config import load_config

    app, mock_store, name, collections_before, pinned_before = _remove_app(tmp_path, tmp_store, with_meta=True)
    drop_error = RuntimeError(f"lance error at {_SECRET_EXC_TEXT}")
    mock_store.drop_collection.side_effect = drop_error

    with caplog.at_level("ERROR", logger="archon_search.server.routes_collections"):
        response = _authed_client(app).delete(f"/collections/{name}")

    assert response.status_code == 500
    assert response.json() == _drop_failed_body()
    assert _SECRET_EXC_TEXT not in response.text
    [record] = [r for r in caplog.records if r.getMessage().startswith("Failed to drop collection")]
    assert record.exc_info is not None and record.exc_info[1] is drop_error
    assert app.state.config.collections == collections_before
    assert app.state.config.pinned_collections == pinned_before
    assert load_config(Path(app.state.config_path)).collections == collections_before
    mock_store.delete_collection_meta.assert_not_awaited()
    mock_store.discard_lock.assert_not_called()
    assert not mock_store.lock_for.return_value.locked()


def test_remove_collection_missing_table_still_deletes_meta_and_discards_lock(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """drop_collection raising KeyError (table already gone) is fine: the meta row is deleted, 200 returned,
    and the lock entry discarded after release."""
    app, mock_store, name, _collections_before, _pinned_before = _remove_app(tmp_path, tmp_store, with_meta=True)
    mock_store.drop_collection.side_effect = KeyError(name)

    response = _authed_client(app).delete(f"/collections/{name}")

    assert response.status_code == 200
    assert app.state.config.collections == []
    mock_store.delete_collection_meta.assert_awaited_once_with(name, "default")
    mock_store.discard_lock.assert_called_once_with(name)
    assert not mock_store.lock_for.return_value.locked()


def test_remove_collection_drop_failure_keeps_status_when_rollback_save_fails(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """A failing rollback config save after a drop failure is logged only: the response stays the
    sanitized collection_drop_failed 500 and the in-memory config is still restored."""
    app, mock_store, name, collections_before, pinned_before = _remove_app(tmp_path, tmp_store, with_meta=True)
    mock_store.drop_collection.side_effect = OSError("disk failure")
    calls = {"n": 0}

    def _save(config: object, config_path: object) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("disk full")

    with patch("archon_search.server.routes_collections.save_config", side_effect=_save):
        response = _authed_client(app).delete(f"/collections/{name}")

    assert response.status_code == 500
    assert response.json() == _drop_failed_body()
    assert calls["n"] == 2
    assert app.state.config.collections == collections_before
    assert app.state.config.pinned_collections == pinned_before
    assert not mock_store.lock_for.return_value.locked()


def test_remove_collection_drop_failure_rollback_keeps_concurrent_config_change(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """A path another request appends while DELETE awaits the drop survives the rollback, and the
    removed entry is re-inserted at its original index."""
    from archon_search.config import load_config

    app, mock_store, name, collections_before, pinned_before = _remove_app(tmp_path, tmp_store, with_meta=True)
    added_during_drop = str(tmp_path / "added-during-drop")

    async def _append_then_fail(*args: object, **kwargs: object) -> None:
        app.state.config.collections.append(added_during_drop)
        raise OSError("disk failure")

    mock_store.drop_collection.side_effect = _append_then_fail

    response = _authed_client(app).delete(f"/collections/{name}")

    assert response.status_code == 500
    assert response.json() == _drop_failed_body()
    assert app.state.config.collections == [*collections_before, added_during_drop]
    assert app.state.config.pinned_collections == pinned_before
    assert load_config(Path(app.state.config_path)).collections == [*collections_before, added_during_drop]


def test_remove_collection_success_discards_lock_after_release(tmp_path: Path, tmp_store: JobStore) -> None:
    """After a successful drop the route forgets the collection's lock entry — only once it has
    released the lock, so the store never hands out a second lock while the first is held."""
    app, mock_store, name, _collections_before, _pinned_before = _remove_app(tmp_path, tmp_store, with_meta=True)
    lock = mock_store.lock_for.return_value
    held_at_discard: list[bool] = []
    mock_store.discard_lock.side_effect = lambda collection: held_at_discard.append(lock.locked())

    response = _authed_client(app).delete(f"/collections/{name}")

    assert response.status_code == 200
    mock_store.discard_lock.assert_called_once_with(name)
    assert held_at_discard == [False]


def _reindex_app(tmp_path: Path, tmp_store: JobStore) -> tuple[object, MagicMock, str]:
    """App with one configured collection whose meta row exists, for POST /collections/{name}/reindex."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    name = path_to_collection_name(str(src))

    search_store = MagicMock()
    search_store.get_collection_meta = AsyncMock(return_value=CollectionMeta(name=name, namespace="default"))
    search_store.update_collection_meta = AsyncMock()
    search_store.migrate_namespace = AsyncMock()
    search_store.migrate_acl = AsyncMock()
    search_store.connect = AsyncMock()
    search_store.disconnect = AsyncMock()
    search_store.lock_for = MagicMock(return_value=asyncio.Lock())

    with mock.patch("archon_search.server.app.DocumentChunker"):
        app = create_app(cfg, tmp_store)
    app.state.search_store = search_store
    app.state.embedder_cache = MagicMock()
    app.state.pipeline = MagicMock()
    return app, search_store, name


def test_reindex_meta_write_failure_marks_job_failed(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing meta write after the reindex job was created → logged, 500, and the job ends FAILED
    (not PENDING) with the sanitized REINDEX_FAILED_DETAIL, no error text leaked."""
    from archon_search.server.routes_jobs import REINDEX_FAILED_DETAIL

    app, search_store, name = _reindex_app(tmp_path, tmp_store)
    meta_error = RuntimeError(_SECRET_EXC_TEXT)
    search_store.update_collection_meta.side_effect = meta_error

    with caplog.at_level("ERROR", logger="archon_search.server.routes_collections"):
        response = _authed_client(app).post(f"/collections/{name}/reindex")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    [job] = tmp_store.list()
    assert job.status == JobStatus.FAILED
    assert job.error == REINDEX_FAILED_DETAIL
    [record] = [r for r in caplog.records if r.getMessage().startswith("Failed to record the reindex job")]
    assert record.exc_info is not None and record.exc_info[1] is meta_error
    # The pointer is written under the collection lock (not reentrant: the caller says it holds it), then released.
    assert search_store.update_collection_meta.await_args.kwargs == {"_locked_by_caller": True}
    assert not search_store.lock_for.return_value.locked()


@pytest.mark.parametrize("update_error", [OSError(28, "No space left on device", _SECRET_EXC_TEXT), KeyError("job")])
def test_reindex_meta_write_failure_keeps_500_when_marking_job_failed_raises(
    tmp_path: Path, tmp_store: JobStore, caplog: pytest.LogCaptureFixture, update_error: Exception
) -> None:
    """If marking the job FAILED itself raises, the handler logs it and still returns the sanitized 500."""
    app, search_store, name = _reindex_app(tmp_path, tmp_store)
    search_store.update_collection_meta.side_effect = RuntimeError("disk error")

    with (
        patch.object(tmp_store, "update", side_effect=update_error),
        caplog.at_level("ERROR", logger="archon_search.server.routes_jobs"),
    ):
        response = _authed_client(app).post(f"/collections/{name}/reindex")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    [record] = [r for r in caplog.records if r.getMessage().startswith("reindex_collection: could not persist FAILED status for job ")]
    assert record.exc_info is not None and record.exc_info[1] is update_error


@pytest.mark.parametrize("missing_dependency", ["embedder_cache", "pipeline", "search_store"])
def test_reindex_service_not_ready_creates_no_job(tmp_path: Path, tmp_store: JobStore, missing_dependency: str) -> None:
    """No embedder cache, pipeline or search store yet → 503 before anything is written: no reindex job (not even
    a FAILED one) and no meta write pointing reindex_job_id at it."""
    app, search_store, name = _reindex_app(tmp_path, tmp_store)
    setattr(app.state, missing_dependency, None)

    response = _authed_client(app).post(f"/collections/{name}/reindex")

    assert response.status_code == 503
    assert response.json() == {"detail": "service not ready"}
    assert tmp_store.list() == []
    search_store.update_collection_meta.assert_not_awaited()


def test_reindex_without_a_search_store_answers_503_whatever_the_name(tmp_path: Path, tmp_store: JobStore) -> None:
    """Readiness is checked first: without a search store no reindex can run, so even a name absent from the config
    answers 503 — not a 404 that a configured name would never get either."""
    app, _, _ = _reindex_app(tmp_path, tmp_store)
    app.state.search_store = None

    response = _authed_client(app).post("/collections/not-configured/reindex")

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "service not ready"}
    assert tmp_store.list() == []


# No "search_store" case: without a store no meta row (so no 409) can be read at all.
@pytest.mark.parametrize("missing_dependency", ["embedder_cache", "pipeline"])
def test_reindex_service_not_ready_answers_503_before_the_in_progress_409(
    tmp_path: Path, tmp_store: JobStore, missing_dependency: str
) -> None:
    """With a reindex already active AND the service not ready, the answer is the readiness 503: a 409 would tell
    the caller to wait for a job, while a retry after readiness is what can succeed."""
    app, search_store, name = _reindex_app(tmp_path, tmp_store)
    active = tmp_store.create_reindex(name)  # PENDING: an active reindex
    search_store.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name=name, namespace="default", reindex_job_id=active.job_id)
    )
    setattr(app.state, missing_dependency, None)

    response = _authed_client(app).post(f"/collections/{name}/reindex")

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "service not ready"}
    assert tmp_store.list() == [active]


# ---------------------------------------------------------------------------
# POST /collections/ — path safety validation (Task 1.2 / A5a)
# ---------------------------------------------------------------------------


def test_add_collection_rejects_dotdot_path(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ with a dotdot path returns 400 with 'path is unsafe:' detail."""
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": "/foo/../bar"})

    assert response.status_code == 400
    assert response.json()["detail"].startswith("path is unsafe:")


def test_add_collection_uses_validator_returned_path(
    tmp_path: Path, tmp_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handler must use the path returned by validate_ingest_path, not re-resolve body.path."""
    import asyncio as _asyncio
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())
    app.state.search_store = mock_store

    # Patch the validator only in the route module: the ingest body must carry its
    # result, never re-validate (a second call could raise after the lock is held).
    validated: list[str] = []

    def _sentinel_validator(raw: str) -> Path:
        validated.append(raw)
        return Path("/sentinel/value")

    monkeypatch.setattr("archon_search.server.routes_collections.validate_ingest_path", _sentinel_validator)

    # Capture the IngestRequest passed to either ingest task variant.
    # The handler branches to _default_ingest_task_with_lock when lock_result is not None.
    captured: list[str] = []

    # Must stay await-free: the assertion below relies on this completing in a single
    # event-loop step before the response is returned (no await point => no race).
    async def _capturing_ingest_task(job_id, store, body, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(body.path)

    monkeypatch.setattr(
        "archon_search.server.routes_jobs._default_ingest_task",
        _capturing_ingest_task,
    )
    monkeypatch.setattr(
        "archon_search.server.routes_jobs._default_ingest_task_with_lock",
        _capturing_ingest_task,
    )

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    # Do NOT patch asyncio.create_task here — the task must actually run so that
    # captured receives body.path.  TestClient drives a real event loop that will
    # schedule and execute the capturing task before the response is returned.
    response = c.post("/collections/", json={"path": "/some/legitimate/path"})

    assert response.status_code == 202
    assert captured == [str(Path("/sentinel/value"))]
    assert validated == ["/some/legitimate/path"]


def _tilde_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """A ``~`` path and the server-side path it resolves to."""
    home = tmp_path / "server-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return "~/missing/s284", (home / "missing" / "s284").resolve()


def _symlink_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """A path through a symlink and the link target's path it resolves to."""
    target = tmp_path / "server-internal-target"
    target.mkdir()
    link = tmp_path / "caller-link"
    link.symlink_to(target, target_is_directory=True)
    return str(link / "missing" / "s284"), (target / "missing" / "s284").resolve()


@pytest.mark.parametrize("submitted_path", [_tilde_path, _symlink_path], ids=["tilde", "symlink"])
def test_add_collection_ingest_body_keeps_the_submitted_path(
    tmp_path: Path, tmp_store: JobStore, monkeypatch: pytest.MonkeyPatch, submitted_path: Any
) -> None:
    """The ingest job's not-found text must echo the caller's raw path, not the server's resolution of it."""
    import asyncio as _asyncio

    from archon_search.server.routes_jobs import INGEST_PATH_NOT_FOUND_PREFIX, IngestPathNotFoundError

    submitted, resolved = submitted_path(tmp_path, monkeypatch)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())
    app.state.search_store = mock_store
    captured: list[object] = []

    async def _capturing_ingest_task(job_id, store, body, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(body)

    monkeypatch.setattr("archon_search.server.routes_jobs._default_ingest_task", _capturing_ingest_task)
    monkeypatch.setattr("archon_search.server.routes_jobs._default_ingest_task_with_lock", _capturing_ingest_task)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": submitted})

    assert response.status_code == 202, response.text
    [body] = captured
    # Presence anchor: the ingest itself runs on the server's resolved path.
    assert body.path == str(resolved)
    assert body.path != submitted
    assert body.submitted_path == submitted
    assert str(IngestPathNotFoundError(body.submitted_path)) == f"{INGEST_PATH_NOT_FOUND_PREFIX}: {submitted}"


def test_add_collection_rejects_relative_path(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ with a relative path returns 400."""
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": "./foo"})

    assert response.status_code == 400


def test_add_collection_rejects_empty_path(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ with an empty path returns 400 with 'empty' in detail."""
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": ""})

    assert response.status_code == 400
    assert "empty" in response.json()["detail"]


def test_add_collection_unauth_takes_precedence(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """Auth check fires before path validation: dotdot path WITHOUT auth → 401."""
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store
    # No auth headers
    c = TestClient(app)

    response = c.post("/collections/", json={"path": "/foo/../bar"})

    assert response.status_code == 401


def test_add_collection_accepts_legitimate_absolute_path(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ with a valid absolute path still returns 202 (regression guard)."""
    import asyncio as _asyncio
    import uuid as _uuid
    src = tmp_path / f"legit-{_uuid.uuid4().hex}"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())
    app.state.search_store = mock_store
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.PENDING.value


def test_add_collection_openapi_lists_400_response(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """OpenAPI spec for POST /collections/ includes a 400 response with ErrorDetail schema."""
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, tmp_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()

    post_responses = spec["paths"]["/collections/"]["post"]["responses"]
    assert "400" in post_responses

    # The 400 response schema must reference ErrorDetail
    schema_ref = post_responses["400"]["content"]["application/json"]["schema"]["$ref"]
    # $ref looks like "#/components/schemas/ErrorDetail"
    assert schema_ref.endswith("ErrorDetail")


def test_reindex_openapi_lists_every_error_status_it_returns(tmp_path: Path, tmp_store: JobStore) -> None:
    """POST /collections/{name}/reindex declares the 409, 503 and 500 it returns, each with ErrorDetail."""
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    spec = create_app(cfg, tmp_store).openapi()

    responses = spec["paths"]["/collections/{name}/reindex"]["post"]["responses"]
    for status in ("401", "404", "409", "500", "503"):
        assert responses[status]["content"]["application/json"]["schema"]["$ref"].endswith("ErrorDetail"), status


# ---------------------------------------------------------------------------
# OSError on JobStore writes → 500 envelope (Task 2.6)
# ---------------------------------------------------------------------------


def test_collection_add_oserror_returns_500_envelope(
    tmp_path: Path,
) -> None:
    """POST /collections/ returns the 500 envelope when store.create raises OSError."""
    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    job_store = MagicMock()
    job_store.create.side_effect = OSError("disk full")

    import asyncio as _asyncio

    # search_store.update_collection_meta must succeed so execution reaches store.create
    search_store = MagicMock()
    search_store.get_all_collections_meta = AsyncMock(return_value=[])
    search_store.update_collection_meta = AsyncMock()
    search_store.migrate_namespace = AsyncMock()
    search_store.migrate_acl = AsyncMock()
    search_store.connect = AsyncMock()
    search_store.disconnect = AsyncMock()
    search_store.lock_for = MagicMock(return_value=_asyncio.Lock())

    with mock.patch("archon_search.server.app.DocumentChunker"):
        app = create_app(cfg, job_store)
    app.state.search_store = search_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}


def test_create_collection_returns_503_on_lock_timeout(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ returns 503 with Retry-After when update_collection_meta raises StoreBusyError."""
    import asyncio as _asyncio
    from archon_search.store import StoreBusyError

    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock(side_effect=StoreBusyError(timeout_s=30.0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    # Body must use standard {"detail": "..."} shape — no "error" key
    data = response.json()
    assert "error" not in data
    assert data["detail"] == "store busy; retry in 30 seconds"
    # Config must be reverted — path should not be persisted on busy lock
    updated_config: SearchConfig = app.state.config
    assert str(src.resolve()) not in updated_config.collections


def test_create_collection_returns_503_on_ingest_lock_timeout(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ returns standard 503 shape when the ingest lock is held."""
    import asyncio as _asyncio

    src = tmp_path / "myproject"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.delete_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    busy_response = JSONResponse(
        {"error": "store_busy", "detail": "reindex in progress; retry after Retry-After seconds"},
        status_code=503,
        headers={"Retry-After": "30"},
    )
    with patch(
        "archon_search.server.routes_collections.acquire_collection_lock_or_503",
        new_callable=AsyncMock,
        return_value=busy_response,
    ):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    data = response.json()
    assert "error" not in data
    assert data["detail"] == "store busy; retry in 30 seconds"


# ---------------------------------------------------------------------------
# PATCH /collections/{name} — per-collection embedding model (Task 5.1)
# ---------------------------------------------------------------------------


def _make_patch_app(
    tmp_path: Path,
    tmp_store: JobStore,
    *,
    meta,
    count_chunks: int = 5,
    validate_model_dim: int = 384,
    validate_model_raises: Exception | None = None,
) -> "tuple[TestClient, MagicMock]":
    """Helper: build an app with a mock search_store wired for PATCH tests."""
    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=count_chunks)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    if validate_model_raises is not None:
        validate_patch = patch(
            "archon_search.server.routes_collections.validate_embedding_model",
            side_effect=validate_model_raises,
        )
    else:
        validate_patch = patch(
            "archon_search.server.routes_collections.validate_embedding_model",
            return_value=validate_model_dim,
        )

    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return client, mock_store, validate_patch


def test_patch_returns_200_on_model_change(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH /collections/{name} with a new model triggers state-b: sets pending model."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="BAAI/bge-small-en-v1.5")

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=None)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=768,
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": "BAAI/bge-base-en-v1.5"})

    assert response.status_code == 200
    mock_store.update_collection_meta.assert_called_once()
    saved_meta = mock_store.update_collection_meta.call_args[0][0]
    assert saved_meta.pending_embedding_model == "BAAI/bge-base-en-v1.5"
    assert saved_meta.needs_reindex is True


def test_patch_returns_409_on_active_reindex(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH returns 409 when a reindex job is currently RUNNING."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import IngestJob, JobStatus

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model="BAAI/bge-small-en-v1.5",
        reindex_job_id="job-running-123",
    )

    running_job = IngestJob(
        job_id="job-running-123",
        status=JobStatus.RUNNING,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        namespace="default",
    )

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=None)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    # Wire a real-ish job_store mock
    job_store_mock = MagicMock()
    job_store_mock.get = MagicMock(return_value=running_job)
    app.state.job_store = job_store_mock

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=384,
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": "BAAI/bge-base-en-v1.5"})

    assert response.status_code == 409


def test_patch_returns_422_on_unknown_model(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH returns 422 when validate_embedding_model raises ModelValidationError."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.model_validation import ModelValidationError

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=0)
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=None)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        side_effect=ModelValidationError("unknown model"),
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": "unknown/model"})

    assert response.status_code == 422


def test_patch_different_dimension_sets_pending_model(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH with a model whose dimensions differ from stored vectors sets
    pending_embedding_model and needs_reindex=true (S327).

    The documented behavior is to defer the model change to a reindex, not
    reject it with 422.
    """
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="BAAI/bge-small-en-v1.5")

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=1)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=768,  # different from stored 384
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": "BAAI/bge-base-en-v1.5"})

    assert response.status_code == 200
    mock_store.update_collection_meta.assert_called_once()
    saved_meta = mock_store.update_collection_meta.call_args[0][0]
    assert saved_meta.pending_embedding_model == "BAAI/bge-base-en-v1.5"
    assert saved_meta.needs_reindex is True
    assert saved_meta.active_embedding_model == "BAAI/bge-small-en-v1.5"


def test_patch_idempotent_same_active_model(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH with same model as active returns 200 no-op (state-a)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    model = "BAAI/bge-small-en-v1.5"
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model=model)

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=384)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=384,
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": model})

    assert response.status_code == 200
    # No update should be written for a no-op
    mock_store.update_collection_meta.assert_not_called()


def test_patch_namespace_isolation(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH returns 404 when collection meta is not found for caller's namespace."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    caller_key = "n" * 64
    cfg.namespaces = {caller_key: "tenantN"}

    app = create_app(cfg, tmp_store)

    mock_store = MagicMock()
    # Returns None — namespace mismatch
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})
    name = path_to_collection_name(str(src))

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=384,
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": "BAAI/bge-small-en-v1.5"})

    assert response.status_code == 404


def test_patch_state_c_revert(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH requesting active model while pending is set clears pending (state-c)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    active_model = "BAAI/bge-small-en-v1.5"
    pending_model = "BAAI/bge-base-en-v1.5"
    meta = CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model=active_model,
        pending_embedding_model=pending_model,
        needs_reindex=True,
    )

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=384)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=384,
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": active_model})

    assert response.status_code == 200
    mock_store.update_collection_meta.assert_called_once()
    saved_meta = mock_store.update_collection_meta.call_args[0][0]
    assert saved_meta.pending_embedding_model is None
    assert saved_meta.needs_reindex is False


def test_patch_state_d_replace_pending(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH requesting a third model (C≠A,C≠B) replaces pending model (state-d)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    active_model = "BAAI/bge-small-en-v1.5"
    pending_model = "BAAI/bge-base-en-v1.5"
    new_model = "sentence-transformers/all-MiniLM-L6-v2"
    meta = CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model=active_model,
        pending_embedding_model=pending_model,
        needs_reindex=True,
    )

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=None)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=384,
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": new_model})

    assert response.status_code == 200
    mock_store.update_collection_meta.assert_called_once()
    saved_meta = mock_store.update_collection_meta.call_args[0][0]
    assert saved_meta.pending_embedding_model == new_model
    assert saved_meta.needs_reindex is True


def test_patch_empty_collection_sets_active_directly(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH on empty collection (count_chunks==0) sets active_model directly, no pending."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="BAAI/bge-small-en-v1.5")

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=0)  # empty collection
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=None)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=768,
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": "BAAI/bge-base-en-v1.5"})

    assert response.status_code == 200
    mock_store.update_collection_meta.assert_called_once()
    saved_meta = mock_store.update_collection_meta.call_args[0][0]
    assert saved_meta.active_embedding_model == "BAAI/bge-base-en-v1.5"
    assert saved_meta.pending_embedding_model is None
    assert saved_meta.needs_reindex is False


def test_patch_stale_reindex_job_id_auto_cleared(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH auto-clears reindex_job_id when job is in DONE state and proceeds."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import IngestJob, JobStatus

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model="BAAI/bge-small-en-v1.5",
        reindex_job_id="job-done-456",
    )

    done_job = IngestJob(
        job_id="job-done-456",
        status=JobStatus.DONE,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        namespace="default",
    )

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=None)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    job_store_mock = MagicMock()
    job_store_mock.get = MagicMock(return_value=done_job)
    app.state.job_store = job_store_mock

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=768,
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": "BAAI/bge-base-en-v1.5"})

    assert response.status_code == 200
    # reindex_job_id should be cleared
    saved_meta = mock_store.update_collection_meta.call_args[0][0]
    assert saved_meta.reindex_job_id is None


def test_patch_stale_cancelled_reindex_job_id_auto_cleared(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH auto-clears reindex_job_id when job is in CANCELLED state and proceeds."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import IngestJob, JobStatus

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model="BAAI/bge-small-en-v1.5",
        reindex_job_id="job-cancelled-789",
    )

    cancelled_job = IngestJob(
        job_id="job-cancelled-789",
        status=JobStatus.CANCELLED,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        namespace="default",
    )

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=None)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    job_store_mock = MagicMock()
    job_store_mock.get = MagicMock(return_value=cancelled_job)
    app.state.job_store = job_store_mock

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=768,
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": "BAAI/bge-base-en-v1.5"})

    assert response.status_code == 200
    saved_meta = mock_store.update_collection_meta.call_args[0][0]
    assert saved_meta.reindex_job_id is None


def test_patch_stale_failed_reindex_job_id_auto_cleared(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH auto-clears reindex_job_id when job is in FAILED state and proceeds."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import IngestJob, JobStatus

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model="BAAI/bge-small-en-v1.5",
        reindex_job_id="job-failed-000",
    )

    failed_job = IngestJob(
        job_id="job-failed-000",
        status=JobStatus.FAILED,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        namespace="default",
    )

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=None)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    job_store_mock = MagicMock()
    job_store_mock.get = MagicMock(return_value=failed_job)
    app.state.job_store = job_store_mock

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=768,
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": "BAAI/bge-base-en-v1.5"})

    assert response.status_code == 200
    saved_meta = mock_store.update_collection_meta.call_args[0][0]
    assert saved_meta.reindex_job_id is None


def test_patch_state_a_prime_same_pending(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH requesting same model as pending returns 200 no-op (state-a')."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    active_model = "BAAI/bge-small-en-v1.5"
    pending_model = "BAAI/bge-base-en-v1.5"
    meta = CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model=active_model,
        pending_embedding_model=pending_model,
        needs_reindex=True,
    )

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_chunks = AsyncMock(return_value=5)
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=None)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=768,
    ):
        response = c.patch(f"/collections/{name}", json={"embedding_model": pending_model})

    assert response.status_code == 200
    # No-op: should NOT write
    mock_store.update_collection_meta.assert_not_called()


def test_patch_missing_embedding_model_accepted(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """E2a BE-4: PATCH without embedding_model is now valid (no-op on model); returns 200."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="BAAI/bge-small-en-v1.5")

    # _make_patch_app returns (client, mock_store, validate_patch_ctx); unpack all 3
    client, _, _validate_patch_ctx = _make_patch_app(tmp_path, tmp_store, meta=meta, count_chunks=0)

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        side_effect=AssertionError("must not be called when embedding_model is absent"),
    ):
        response = client.patch(f"/collections/{name}", json={})
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"


def test_patch_collection_response_chunk_count_real(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH /collections/{name} response carries the live chunk_count, not a hardcoded 0 (bug-080)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="BAAI/bge-small-en-v1.5")

    client, _, _ = _make_patch_app(tmp_path, tmp_store, meta=meta, count_chunks=9)

    # default_ttl_seconds-only PATCH skips embedding-model validation entirely.
    response = client.patch(f"/collections/{name}", json={"default_ttl_seconds": 3600})
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"
    assert response.json()["chunk_count"] == 9
    assert response.json()["default_ttl_seconds"] == 3600
    assert response.json()["schema_version"] == 0


def test_patch_null_embedding_model_accepted(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """E2a BE-4: PATCH with null embedding_model is now valid (no-op on model); returns 200."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="BAAI/bge-small-en-v1.5")

    client, _, _validate_patch_ctx = _make_patch_app(tmp_path, tmp_store, meta=meta, count_chunks=0)

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        side_effect=AssertionError("must not be called when embedding_model is null"),
    ):
        response = client.patch(f"/collections/{name}", json={"embedding_model": None})
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"


def test_patch_empty_string_embedding_model_returns_422(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH with empty-string embedding_model returns 422."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    mock_store = MagicMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    name = path_to_collection_name(str(src))
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.patch(f"/collections/{name}", json={"embedding_model": ""})
    assert response.status_code == 422


def test_patch_nonexistent_collection_returns_404(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH for a collection name not in config returns 404."""
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, tmp_store)

    mock_store = MagicMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.validate_embedding_model",
        return_value=384,
    ):
        response = c.patch("/collections/nonexistent-collection", json={"embedding_model": "BAAI/bge-small-en-v1.5"})

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Task 6.1 — POST /collections/ gains embedding_model field
# ---------------------------------------------------------------------------


def _make_post_app(
    tmp_path: Path,
    tmp_store: "JobStore",
    *,
    validate_raises: Exception | None = None,
) -> "tuple[object, MagicMock]":
    """Helper: app + mock_store for POST /collections/ embedding_model tests."""
    import asyncio as _asyncio

    src = tmp_path / "myproject"
    src.mkdir(exist_ok=True)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, tmp_store)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=_asyncio.Lock())
    app.state.search_store = mock_store

    if validate_raises is not None:
        validate_patch = patch(
            "archon_search.server.routes_collections.validate_embedding_model",
            side_effect=validate_raises,
        )
    else:
        validate_patch = patch(
            "archon_search.server.routes_collections.validate_embedding_model",
            return_value=384,
        )

    return app, mock_store, validate_patch, src


def test_create_collection_with_embedding_model(
    tmp_path: Path, tmp_store: "JobStore"
) -> None:
    """POST /collections/ with embedding_model stores it as active_embedding_model in stub meta."""
    from archon_search.collection_meta import CollectionMeta

    app, mock_store, validate_patch, src = _make_post_app(tmp_path, tmp_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with validate_patch:
        with patch(
            "archon_search.server.routes_collections.asyncio.create_task",
            side_effect=lambda coro: (coro.close(), MagicMock())[1],
        ):
            response = c.post(
                "/collections/",
                json={"path": str(src), "embedding_model": "model-X"},
            )

    assert response.status_code == 202
    mock_store.update_collection_meta.assert_called_once()
    call_arg = mock_store.update_collection_meta.call_args[0][0]
    assert isinstance(call_arg, CollectionMeta)
    assert call_arg.active_embedding_model == "model-X"
    assert call_arg.pending_embedding_model is None
    assert call_arg.needs_reindex is False
    assert call_arg.reindex_job_id is None


def test_create_collection_without_embedding_model_uses_global(
    tmp_path: Path, tmp_store: "JobStore"
) -> None:
    """POST /collections/ without embedding_model uses config.embedding_model as active."""
    from archon_search.collection_meta import CollectionMeta

    app, mock_store, validate_patch, src = _make_post_app(tmp_path, tmp_store)
    cfg: SearchConfig = app.state.config
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch(
        "archon_search.server.routes_collections.asyncio.create_task",
        side_effect=lambda coro: (coro.close(), MagicMock())[1],
    ):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 202
    mock_store.update_collection_meta.assert_called_once()
    call_arg = mock_store.update_collection_meta.call_args[0][0]
    assert isinstance(call_arg, CollectionMeta)
    assert call_arg.active_embedding_model == cfg.embedding_model


def test_create_collection_unknown_model_returns_422(
    tmp_path: Path, tmp_store: "JobStore"
) -> None:
    """POST /collections/ with unknown embedding_model returns 422."""
    from archon_search.model_validation import ModelValidationError

    app, mock_store, validate_patch, src = _make_post_app(
        tmp_path, tmp_store, validate_raises=ModelValidationError("unknown model")
    )
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with validate_patch:
        response = c.post(
            "/collections/",
            json={"path": str(src), "embedding_model": "not/a/real/model"},
        )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Task 6.2 — CollectionDetail schema + GET /collections/{name}
# ---------------------------------------------------------------------------


def test_get_collection_returns_active_embedding_model(
    tmp_path: Path, tmp_store: "JobStore"
) -> None:
    """GET /collections/{name} returns active_embedding_model from meta, not global config."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="model-X")
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    data = response.json()
    assert data["active_embedding_model"] == "model-X"
    assert data["active_embedding_model"] != cfg.embedding_model


def test_get_collection_pending_null_before_patch(
    tmp_path: Path, tmp_store: "JobStore"
) -> None:
    """GET /collections/{name} returns pending_embedding_model=null before any PATCH."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs2"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="BAAI/bge-small-en-v1.5")
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    data = response.json()
    assert data["pending_embedding_model"] is None
    assert data["needs_reindex"] is False
    assert data["reindex_job_id"] is None


def test_get_collection_reflects_patch_state(
    tmp_path: Path, tmp_store: "JobStore"
) -> None:
    """GET after PATCH: active unchanged, pending set, needs_reindex=true."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs3"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model="BAAI/bge-small-en-v1.5",
        pending_embedding_model="BAAI/bge-base-en-v1.5",
        needs_reindex=True,
    )
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=5)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    data = response.json()
    assert data["active_embedding_model"] == "BAAI/bge-small-en-v1.5"
    assert data["pending_embedding_model"] == "BAAI/bge-base-en-v1.5"
    assert data["needs_reindex"] is True


def test_get_collection_reflects_reindex_completion(
    tmp_path: Path, tmp_store: "JobStore"
) -> None:
    """After reindex completes: active promoted, pending=null, needs_reindex=false."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs4"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model="BAAI/bge-base-en-v1.5",
        pending_embedding_model=None,
        needs_reindex=False,
        reindex_job_id=None,
    )
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=5)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    data = response.json()
    assert data["active_embedding_model"] == "BAAI/bge-base-en-v1.5"
    assert data["pending_embedding_model"] is None
    assert data["needs_reindex"] is False
    assert data["reindex_job_id"] is None


# ---------------------------------------------------------------------------
# Task 6.3 — CollectionSummary + GET /collections/ list
# ---------------------------------------------------------------------------


def test_list_collections_includes_active_embedding_model(
    tmp_path: Path, tmp_store: "JobStore"
) -> None:
    """GET /collections/ response includes active_embedding_model in each entry."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model="BAAI/bge-small-en-v1.5")
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[meta])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert "active_embedding_model" in data[0]
    assert data[0]["active_embedding_model"] == "BAAI/bge-small-en-v1.5"


def test_list_collections_includes_needs_reindex(
    tmp_path: Path, tmp_store: "JobStore"
) -> None:
    """GET /collections/ includes needs_reindex in each entry."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", needs_reindex=True)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[meta])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert "needs_reindex" in data[0]
    assert data[0]["needs_reindex"] is True


# ---------------------------------------------------------------------------
# POST /collections/{name}/reindex — Task 8.3: ReindexJob + 409 guard
# ---------------------------------------------------------------------------


def _make_reindex_app(tmp_path: Path, tmp_store: JobStore, meta_kwargs: dict | None = None) -> tuple:
    """Helper: create an app with one collection and a mock search_store."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    name = path_to_collection_name(str(src))
    extra = meta_kwargs or {}
    meta = CollectionMeta(name=name, namespace="default", **extra)

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=asyncio.Lock())

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store
    app.state.embedder_cache = MagicMock()
    app.state.pipeline = MagicMock()

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return c, name, meta, mock_store


def test_reindex_endpoint_sets_reindex_job_id(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST reindex sets meta.reindex_job_id before spawning the task."""
    c, name, meta, mock_store = _make_reindex_app(tmp_path, tmp_store)

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 202
    data = response.json()
    job_id = data["job_id"]
    assert job_id

    # update_collection_meta was called with the job_id set
    mock_store.update_collection_meta.assert_called_once()
    updated_meta: CollectionMeta = mock_store.update_collection_meta.call_args[0][0]
    assert updated_meta.reindex_job_id == job_id


def test_reindex_endpoint_captures_pending_model(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """ReindexJob gets target_embedding_model from meta.pending_embedding_model."""
    from archon_search.types import ReindexJob

    c, name, meta, mock_store = _make_reindex_app(
        tmp_path, tmp_store, {"pending_embedding_model": "model-X"}
    )

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = tmp_store.get(job_id)
    assert isinstance(job, ReindexJob)
    assert job.target_embedding_model == "model-X"


def test_reindex_endpoint_data_only_sets_null_target(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """When pending_embedding_model is None, job has target_embedding_model=None."""
    from archon_search.types import ReindexJob

    c, name, meta, mock_store = _make_reindex_app(
        tmp_path, tmp_store, {"pending_embedding_model": None}
    )

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = tmp_store.get(job_id)
    assert isinstance(job, ReindexJob)
    assert job.target_embedding_model is None


def test_reindex_endpoint_returns_409_on_active_reindex(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST returns 409 when meta.reindex_job_id points to a RUNNING job."""
    from archon_search.types import ReindexJob
    from archon_search.jobs.model import JobStatus

    # Pre-create a RUNNING ReindexJob in tmp_store
    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    running_job = ReindexJob(
        job_id=str(uuid.uuid4()),
        status=JobStatus.RUNNING,
        created_at=now,
        updated_at=now,
        namespace="default",
        target_embedding_model=None,
    )
    tmp_store.create_job(running_job)

    c, name, meta, mock_store = _make_reindex_app(
        tmp_path, tmp_store, {"reindex_job_id": running_job.job_id}
    )

    response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 409
    assert response.json()["detail"] == "reindex already in progress"
    # No new job should have been created
    assert len(tmp_store.list()) == 1


def test_reindex_endpoint_returns_409_on_pending_reindex(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST returns 409 when meta.reindex_job_id points to a PENDING job."""
    from archon_search.types import ReindexJob
    from archon_search.jobs.model import JobStatus

    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    pending_job = ReindexJob(
        job_id=str(uuid.uuid4()),
        status=JobStatus.PENDING,
        created_at=now,
        updated_at=now,
        namespace="default",
        target_embedding_model=None,
    )
    tmp_store.create_job(pending_job)

    c, name, meta, mock_store = _make_reindex_app(
        tmp_path, tmp_store, {"reindex_job_id": pending_job.job_id}
    )

    response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 409
    assert response.json()["detail"] == "reindex already in progress"
    assert len(tmp_store.list()) == 1


def test_reindex_endpoint_clears_stale_reindex_job_id_and_proceeds(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST proceeds when meta.reindex_job_id points to a DONE (terminal) job."""
    from archon_search.types import ReindexJob
    from archon_search.jobs.model import JobStatus

    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    done_job = ReindexJob(
        job_id=str(uuid.uuid4()),
        status=JobStatus.DONE,
        created_at=now,
        updated_at=now,
        namespace="default",
        target_embedding_model=None,
    )
    tmp_store.create_job(done_job)

    c, name, meta, mock_store = _make_reindex_app(
        tmp_path, tmp_store, {"reindex_job_id": done_job.job_id}
    )

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 202
    new_job_id = response.json()["job_id"]
    assert new_job_id != done_job.job_id


def test_reindex_endpoint_creates_reindex_job_not_ingest_job(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/reindex stores a ReindexJob (not a plain IngestJob)."""
    from archon_search.types import ReindexJob

    c, name, meta, mock_store = _make_reindex_app(tmp_path, tmp_store)

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = tmp_store.get(job_id)
    assert isinstance(job, ReindexJob), f"Expected ReindexJob, got {type(job)}"


# ---------------------------------------------------------------------------
# GET /collections/{name}/migrations/pending  (BE-4)
# ---------------------------------------------------------------------------


def _make_migrations_pending_app(
    tmp_path: Path,
    tmp_store: JobStore,
    *,
    meta_override: "CollectionMeta | None" = None,
    pending_migrations_result: list | None = None,
) -> "tuple[TestClient, str, MagicMock]":
    """Helper: create a TestClient wired for migrations/pending tests."""
    import os
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    name = path_to_collection_name(str(src))
    if meta_override is None:
        meta_override = CollectionMeta(name=name, namespace="default")

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta_override)
    mock_store.pending_migrations = AsyncMock(return_value=pending_migrations_result or [])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return c, name, mock_store


def test_get_migrations_pending_200(tmp_path: Path, tmp_store: JobStore) -> None:
    """GET /collections/{name}/migrations/pending returns 200 with pending migrations."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import MigrationKind, MigrationSpec
    from archon_search.sync import path_to_collection_name as _ptn

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = _ptn(str(src))
    meta = CollectionMeta(name=col_name, namespace="default", schema_version=0)
    spec = MigrationSpec(
        name="migrate_namespace",
        kind=MigrationKind.IN_PLACE,
        description="add namespace column",
        introduced_at=0,
    )

    c, name, mock_store = _make_migrations_pending_app(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[spec],
    )

    response = c.get(f"/collections/{name}/migrations/pending")

    assert response.status_code == 200
    data = response.json()
    assert data["collection"] == name
    assert data["schema_version"] == 0
    assert len(data["pending"]) == 1
    assert data["pending"][0]["name"] == "migrate_namespace"
    assert data["pending"][0]["kind"] == "in_place"
    assert data["pending"][0]["description"] == "add namespace column"
    assert data["pending"][0]["introduced_at"] == 0
    mock_store.pending_migrations.assert_called_once_with(name, "default")


def test_get_migrations_pending_empty(tmp_path: Path, tmp_store: JobStore) -> None:
    """GET /collections/{name}/migrations/pending returns 200 with empty list when schema is current."""
    c, name, mock_store = _make_migrations_pending_app(tmp_path, tmp_store, pending_migrations_result=[])

    response = c.get(f"/collections/{name}/migrations/pending")

    assert response.status_code == 200
    data = response.json()
    assert data["pending"] == []
    assert data["collection"] == name
    assert data["schema_version"] == 0


def test_get_migrations_pending_404_unknown_collection(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name}/migrations/pending returns 404 for non-existent collection."""
    import os

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    # "nonexistent-collection" is not derived from any configured path
    # → config-path check fires first; get_collection_meta is never reached
    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/collections/nonexistent-collection/migrations/pending")
    assert response.status_code == 404
    mock_store.get_collection_meta.assert_not_called()


def test_get_migrations_pending_in_config_no_meta_returns_404(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name}/migrations/pending returns 404 when collection is in config but has no meta (cross-namespace access)."""
    import os
    from archon_search.sync import path_to_collection_name as _ptn

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    name = _ptn(str(src))  # Correct name — passes config gate

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=None)  # Fails meta gate
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}/migrations/pending")
    assert response.status_code == 404
    mock_store.get_collection_meta.assert_called_once()


def test_get_migrations_pending_unauthenticated_returns_401(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name}/migrations/pending returns 401 for unauthenticated request."""
    from archon_search.sync import path_to_collection_name as _ptn

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    name = _ptn(str(src))

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    # No auth header
    c = TestClient(app)
    response = c.get(f"/collections/{name}/migrations/pending")
    assert response.status_code == 401


def test_get_migrations_pending_multiple_specs_order_preserved(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name}/migrations/pending returns all specs in order."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import MigrationKind, MigrationSpec
    from archon_search.sync import path_to_collection_name as _ptn

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = _ptn(str(src))
    meta = CollectionMeta(name=col_name, namespace="default", schema_version=0)
    specs = [
        MigrationSpec(name="migrate_namespace", kind=MigrationKind.IN_PLACE, description="add ns", introduced_at=0),
        MigrationSpec(name="migrate_acl", kind=MigrationKind.IN_PLACE, description="add acl", introduced_at=0),
        MigrationSpec(name="rebuild_embeddings", kind=MigrationKind.REWRITE, description="rewrite vecs", introduced_at=1),
    ]

    c, name, mock_store = _make_migrations_pending_app(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=specs,
    )

    response = c.get(f"/collections/{name}/migrations/pending")

    assert response.status_code == 200
    data = response.json()
    assert len(data["pending"]) == 3
    assert data["pending"][0]["name"] == "migrate_namespace"
    assert data["pending"][1]["name"] == "migrate_acl"
    assert data["pending"][2]["name"] == "rebuild_embeddings"
    assert data["pending"][2]["kind"] == "rewrite"


def test_get_migrations_pending_export_rebuild_kind(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name}/migrations/pending includes export_rebuild migrations with correct kind."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import MigrationKind, MigrationSpec
    from archon_search.sync import path_to_collection_name as _ptn

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = _ptn(str(src))
    meta = CollectionMeta(name=col_name, namespace="default", schema_version=0)
    spec = MigrationSpec(
        name="rebuild_embeddings",
        kind=MigrationKind.EXPORT_REBUILD,
        description="re-embed after model upgrade; operators must re-ingest manually",
        introduced_at=1,
    )

    c, name, mock_store = _make_migrations_pending_app(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[spec],
    )

    response = c.get(f"/collections/{name}/migrations/pending")

    assert response.status_code == 200
    data = response.json()
    assert len(data["pending"]) == 1
    pending_item = data["pending"][0]
    assert pending_item["kind"] == "export_rebuild"
    assert pending_item["name"] == "rebuild_embeddings"
    assert pending_item["description"] == "re-embed after model upgrade; operators must re-ingest manually"
    assert pending_item["introduced_at"] == 1


# ---------------------------------------------------------------------------
# POST /collections/{name}/migrate  (BE-7) — in-place synchronous path
# ---------------------------------------------------------------------------


def _make_migrate_app(
    tmp_path: "Path",
    tmp_store: "JobStore",
    *,
    meta_override: "CollectionMeta | None" = None,
    pending_migrations_result: list | None = None,
) -> "tuple[TestClient, str, MagicMock]":
    """Helper: create a TestClient wired for POST /migrate tests."""
    import os
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    name = path_to_collection_name(str(src))
    if meta_override is None:
        meta_override = CollectionMeta(name=name, namespace="default")

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta_override)
    mock_store.pending_migrations = AsyncMock(return_value=pending_migrations_result or [])
    mock_store.apply_in_place_migrations = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return c, name, mock_store


def test_post_migrate_in_place_returns_200_with_migrations_applied(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/migrate returns 200 with migrations_applied; no job created."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import MigrationKind, MigrationSpec

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=col_name, namespace="default", schema_version=0)
    spec = MigrationSpec(
        name="migrate_namespace",
        kind=MigrationKind.IN_PLACE,
        description="add namespace column",
        introduced_at=0,
    )

    c, name, mock_store = _make_migrate_app(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[spec],
    )

    response = c.post(f"/collections/{name}/migrate", json={})

    assert response.status_code == 200
    data = response.json()
    assert "migrations_applied" in data
    assert data["migrations_applied"] == ["migrate_namespace"]
    # No job created — JobStore must not have been used
    assert tmp_store.list() == []
    # apply_in_place_migrations must have been called with exact args
    mock_store.apply_in_place_migrations.assert_called_once_with(name, "default", [spec])


def test_post_migrate_applies_only_in_place_specs_when_mixed_pending(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /migrate without backup_confirmed=True returns 422 when rewrite is pending (even with mixed specs)."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import MigrationKind, MigrationSpec

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=col_name, namespace="default", schema_version=0)
    in_place_spec = MigrationSpec(
        name="migrate_namespace",
        kind=MigrationKind.IN_PLACE,
        description="add namespace column",
        introduced_at=0,
    )
    rewrite_spec = MigrationSpec(
        name="rebuild_embeddings",
        kind=MigrationKind.REWRITE,
        description="re-embed after model upgrade",
        introduced_at=1,
    )

    c, name, mock_store = _make_migrate_app_with_reindex(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[in_place_spec, rewrite_spec],
    )

    # backup_confirmed=False (default) with REWRITE pending → 422
    response = c.post(f"/collections/{name}/migrate", json={})
    assert response.status_code == 422


def test_post_migrate_mixed_backup_confirmed_returns_202(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /migrate with backup_confirmed=True and both in_place and rewrite pending returns 202."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import MigrationJob, MigrationKind, MigrationSpec

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=col_name, namespace="default", schema_version=0)
    in_place_spec = MigrationSpec(
        name="migrate_namespace",
        kind=MigrationKind.IN_PLACE,
        description="add namespace column",
        introduced_at=0,
    )
    rewrite_spec = MigrationSpec(
        name="rebuild_embeddings",
        kind=MigrationKind.REWRITE,
        description="re-embed after model upgrade",
        introduced_at=1,
    )

    c, name, mock_store = _make_migrate_app_with_reindex(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[in_place_spec, rewrite_spec],
    )

    # backup_confirmed=True with mixed specs → 202 (rewrite job created)
    response = c.post(f"/collections/{name}/migrate", json={"backup_confirmed": True})
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == "RUNNING"
    jobs = tmp_store.list()
    assert len(jobs) == 1
    assert isinstance(jobs[0], MigrationJob)
    # In-place was applied before queuing the rewrite job
    mock_store.apply_in_place_migrations.assert_called_once_with(name, "default", [in_place_spec])


def test_post_migrate_dry_run_true_returns_pending_list(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/migrate with dry_run=true returns same body as GET pending; no side effect."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import MigrationKind, MigrationSpec

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=col_name, namespace="default", schema_version=0)
    spec = MigrationSpec(
        name="migrate_namespace",
        kind=MigrationKind.IN_PLACE,
        description="add namespace column",
        introduced_at=0,
    )

    c, name, mock_store = _make_migrate_app(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[spec],
    )

    response = c.post(f"/collections/{name}/migrate", json={"dry_run": True})

    assert response.status_code == 200
    data = response.json()
    # dry_run returns the pending list (same as GET pending)
    assert "pending" in data
    assert len(data["pending"]) == 1
    assert data["pending"][0]["name"] == "migrate_namespace"
    assert data["collection"] == name
    assert data["schema_version"] == 0
    # No apply called
    mock_store.apply_in_place_migrations.assert_not_called()
    # No job created
    assert tmp_store.list() == []
    # pending_migrations must have been called (read-path correctness)
    mock_store.pending_migrations.assert_called_once_with(name, "default")


def test_post_migrate_in_place_404_unknown_collection(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/migrate returns 404 for a non-existent collection (config-miss gate)."""
    import os

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/nonexistent-collection/migrate", json={})
    assert response.status_code == 404
    mock_store.get_collection_meta.assert_not_called()


def test_post_migrate_in_place_404_meta_miss(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/migrate returns 404 when in config but meta missing (cross-namespace gate)."""
    import os
    from archon_search.sync import path_to_collection_name as _ptn

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    name = _ptn(str(src))

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post(f"/collections/{name}/migrate", json={})
    assert response.status_code == 404
    mock_store.get_collection_meta.assert_called_once()


def test_post_migrate_unauthenticated_returns_401(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/migrate returns 401 for unauthenticated request."""
    from archon_search.sync import path_to_collection_name as _ptn

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    name = _ptn(str(src))

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    # No auth header
    c = TestClient(app)
    response = c.post(f"/collections/{name}/migrate", json={})
    assert response.status_code == 401


def test_post_migrate_apply_failure_returns_500(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /migrate returns 500 with structured error when apply_in_place_migrations raises."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import MigrationKind, MigrationSpec

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=col_name, namespace="default", schema_version=0)
    spec = MigrationSpec(
        name="migrate_namespace",
        kind=MigrationKind.IN_PLACE,
        description="add namespace column",
        introduced_at=0,
    )

    c, name, mock_store = _make_migrate_app(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[spec],
    )
    mock_store.apply_in_place_migrations = AsyncMock(side_effect=RuntimeError("db crashed"))

    response = c.post(f"/collections/{name}/migrate", json={})

    assert response.status_code == 500
    data = response.json()
    assert "detail" in data
    assert "migration failed" in data["detail"]


# ---------------------------------------------------------------------------
# JobResponse migration fields (BE-11)
# ---------------------------------------------------------------------------


def test_job_response_migration_fields_default_none() -> None:
    """JobResponse from a base IngestJob serializes migrations_applied=None and backup_confirmed=None."""
    from archon_search.jobs.model import job_to_dict
    from archon_search.server.schemas import JobResponse
    from archon_search.types import IngestJob, JobStatus

    job = IngestJob(
        job_id="job-001",
        status=JobStatus.DONE,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        namespace="default",
    )

    d = job_to_dict(job)
    resp = JobResponse(**d)

    assert resp.kind is None
    assert resp.migrations_applied is None
    assert resp.backup_confirmed is None


def test_job_response_migration_fields_populated() -> None:
    """JobResponse from a MigrationJob serializes migrations_applied and backup_confirmed correctly."""
    from archon_search.jobs.model import job_to_dict
    from archon_search.server.schemas import JobResponse
    from archon_search.types import MigrationJob, MigrationKind, JobStatus

    job = MigrationJob(
        job_id="job-002",
        status=JobStatus.DONE,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        namespace="default",
        collection="my-collection",
        kind=MigrationKind.IN_PLACE,
        migrations_applied=["migrate_namespace", "migrate_acl"],
        backup_confirmed=True,
    )

    d = job_to_dict(job)
    resp = JobResponse(**d)

    assert resp.kind == "in_place"
    assert resp.migrations_applied == ["migrate_namespace", "migrate_acl"]
    assert resp.backup_confirmed is True


def test_job_response_backup_confirmed_false_not_coerced_to_none() -> None:
    """backup_confirmed=False is preserved as False, not coerced to None."""
    from archon_search.jobs.model import job_to_dict
    from archon_search.server.schemas import JobResponse
    from archon_search.types import MigrationJob, MigrationKind, JobStatus

    job = MigrationJob(
        job_id="job-003",
        status=JobStatus.DONE,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        namespace="default",
        collection="my-collection",
        kind=MigrationKind.IN_PLACE,
        migrations_applied=[],
        backup_confirmed=False,
    )

    d = job_to_dict(job)
    resp = JobResponse(**d)

    assert resp.backup_confirmed is False
    assert resp.backup_confirmed is not None


def test_job_response_migrations_applied_empty_list_not_coerced_to_none() -> None:
    """migrations_applied=[] is preserved as empty list, not coerced to None."""
    from archon_search.jobs.model import job_to_dict
    from archon_search.server.schemas import JobResponse
    from archon_search.types import MigrationJob, MigrationKind, JobStatus

    job = MigrationJob(
        job_id="job-004",
        status=JobStatus.DONE,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        namespace="default",
        collection="my-collection",
        kind=MigrationKind.IN_PLACE,
        migrations_applied=[],
        backup_confirmed=True,
    )

    d = job_to_dict(job)
    resp = JobResponse(**d)

    assert resp.migrations_applied == []
    assert resp.migrations_applied is not None


# ---------------------------------------------------------------------------
# POST /collections/{name}/migrate  (BE-12) — rewrite async path
# ---------------------------------------------------------------------------


def _make_migrate_app_with_reindex(
    tmp_path: "Path",
    tmp_store: "JobStore",
    *,
    meta_override: "CollectionMeta | None" = None,
    pending_migrations_result: list | None = None,
    reindex_job: "object | None" = None,
) -> "tuple[TestClient, str, MagicMock]":
    """Helper: create a TestClient wired for POST /migrate rewrite tests.

    ``reindex_job`` can be injected into ``tmp_store`` to simulate an active
    ``ReindexJob``; the real ``JobStore`` is passed so the route can look it up.
    """
    import os
    from archon_search.collection_meta import CollectionMeta as _CollectionMeta

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    name = path_to_collection_name(str(src))
    if meta_override is None:
        meta_override = _CollectionMeta(name=name, namespace="default")

    if reindex_job is not None:
        tmp_store.create_job(reindex_job)  # type: ignore[arg-type]

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta_override)
    mock_store.pending_migrations = AsyncMock(return_value=pending_migrations_result or [])
    mock_store.apply_in_place_migrations = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return c, name, mock_store


def test_post_migrate_rewrite_returns_202_with_job_id(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /migrate with backup_confirmed=true and a rewrite migration pending returns 202 + job_id."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import MigrationJob, MigrationKind, MigrationSpec

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=col_name, namespace="default", schema_version=0)
    spec = MigrationSpec(
        name="rebuild_embeddings",
        kind=MigrationKind.REWRITE,
        description="re-embed after model upgrade",
        introduced_at=1,
    )

    c, name, mock_store = _make_migrate_app_with_reindex(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[spec],
    )

    response = c.post(f"/collections/{name}/migrate", json={"backup_confirmed": True})

    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == "RUNNING"
    # A MigrationJob must have been persisted.
    jobs = tmp_store.list()
    assert len(jobs) == 1
    assert isinstance(jobs[0], MigrationJob)
    assert jobs[0].backup_confirmed is True
    assert jobs[0].collection == name
    assert jobs[0].kind == MigrationKind.REWRITE


def test_post_migrate_rewrite_422_without_backup_confirmed(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /migrate without backup_confirmed=true returns 422 when rewrite migration is pending."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import MigrationKind, MigrationSpec

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=col_name, namespace="default", schema_version=0)
    spec = MigrationSpec(
        name="rebuild_embeddings",
        kind=MigrationKind.REWRITE,
        description="re-embed after model upgrade",
        introduced_at=1,
    )

    c, name, mock_store = _make_migrate_app_with_reindex(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[spec],
    )

    # Without backup_confirmed (defaults to False)
    response = c.post(f"/collections/{name}/migrate", json={})
    assert response.status_code == 422
    assert "detail" in response.json()

    # Explicitly false
    response2 = c.post(f"/collections/{name}/migrate", json={"backup_confirmed": False})
    assert response2.status_code == 422


def test_post_migrate_export_rebuild_422_not_implemented(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /migrate with export_rebuild migration pending returns 422 (D3 does not execute it)."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import MigrationKind, MigrationSpec

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=col_name, namespace="default", schema_version=0)
    spec = MigrationSpec(
        name="rebuild_all",
        kind=MigrationKind.EXPORT_REBUILD,
        description="full re-ingest required; operators must re-ingest manually",
        introduced_at=1,
    )

    c, name, mock_store = _make_migrate_app_with_reindex(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[spec],
    )

    # Even with backup_confirmed=true, export_rebuild is not executable in D3
    response = c.post(f"/collections/{name}/migrate", json={"backup_confirmed": True})
    assert response.status_code == 422
    data = response.json()
    assert "detail" in data
    # Message should reference export_rebuild or manual re-ingest
    detail_lower = data["detail"].lower()
    assert "export_rebuild" in detail_lower or "manual" in detail_lower


def test_post_migrate_409_if_reindex_job_running(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /migrate returns 409 when a ReindexJob is RUNNING for the same collection."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import JobStatus, MigrationKind, MigrationSpec, ReindexJob

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = path_to_collection_name(str(src))
    meta = CollectionMeta(
        name=col_name,
        namespace="default",
        schema_version=0,
        reindex_job_id="rj-001",
    )
    spec = MigrationSpec(
        name="rebuild_embeddings",
        kind=MigrationKind.REWRITE,
        description="re-embed after model upgrade",
        introduced_at=1,
    )

    now_iso = "2026-01-01T00:00:00+00:00"
    running_reindex = ReindexJob(
        job_id="rj-001",
        status=JobStatus.RUNNING,
        created_at=now_iso,
        updated_at=now_iso,
        namespace="default",
    )

    c, name, mock_store = _make_migrate_app_with_reindex(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[spec],
        reindex_job=running_reindex,
    )

    response = c.post(f"/collections/{name}/migrate", json={"backup_confirmed": True})
    assert response.status_code == 409
    data = response.json()
    assert "detail" in data


def test_post_migrate_409_if_reindex_job_queued(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /migrate returns 409 when a ReindexJob is QUEUED for the same collection."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.types import JobStatus, MigrationKind, MigrationSpec, ReindexJob

    src = tmp_path / "docs"
    src.mkdir(exist_ok=True)
    col_name = path_to_collection_name(str(src))
    meta = CollectionMeta(
        name=col_name,
        namespace="default",
        schema_version=0,
        reindex_job_id="rj-002",
    )
    spec = MigrationSpec(
        name="rebuild_embeddings",
        kind=MigrationKind.REWRITE,
        description="re-embed after model upgrade",
        introduced_at=1,
    )

    now_iso = "2026-01-01T00:00:00+00:00"
    queued_reindex = ReindexJob(
        job_id="rj-002",
        status=JobStatus.QUEUED,
        created_at=now_iso,
        updated_at=now_iso,
        namespace="default",
    )

    c, name, mock_store = _make_migrate_app_with_reindex(
        tmp_path, tmp_store,
        meta_override=meta,
        pending_migrations_result=[spec],
        reindex_job=queued_reindex,
    )

    response = c.post(f"/collections/{name}/migrate", json={"backup_confirmed": True})
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# _migration_task unit tests (BE-12)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_task_marks_failed_on_apply_rewrite_exception(tmp_path: Path) -> None:
    """_migration_task marks job FAILED when apply_rewrite_migration raises.

    The caller (route or scheduler) is responsible for transitioning the job to
    RUNNING before invoking _migration_task; we simulate that here.
    """
    from archon_search.jobs.store import JobStore
    from archon_search.types import MigrationKind, MigrationSpec, JobStatus
    from archon_search.server.routes_collections import MIGRATION_FAILED_DETAIL, _migration_task
    from unittest.mock import AsyncMock, MagicMock

    job_store = JobStore(path=tmp_path / "jobs.json")
    queued_job = job_store.create_migration(collection="my-col", kind=MigrationKind.REWRITE, backup_confirmed=True)
    # Simulate the route/scheduler transitioning to RUNNING before dispatch.
    running_job = job_store.transition(queued_job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
    assert running_job is not None

    spec = MigrationSpec(name="rebuild", kind=MigrationKind.REWRITE, description="test", introduced_at=1)

    mock_search_store = MagicMock()
    mock_search_store.apply_rewrite_migration = AsyncMock(side_effect=RuntimeError("lancedb crashed"))

    await _migration_task(job=running_job, job_store=job_store, search_store=mock_search_store, spec=spec)

    final_job = job_store.get(queued_job.job_id)
    assert final_job is not None
    assert final_job.status == JobStatus.FAILED
    # The store exception stays in the server log; the job carries the sanitized text.
    assert final_job.error == MIGRATION_FAILED_DETAIL
    assert "lancedb crashed" not in (final_job.error or "")


@pytest.mark.asyncio
async def test_migration_task_writes_the_meta_row_before_done(tmp_path: Path) -> None:
    """The rewrite's meta write (``apply_rewrite_migration`` re-reads and rewrites the row's schema_version) happens
    while the job is still RUNNING, and DONE is written after it returns: DELETE /collections/{name} refuses only
    while a migration is RUNNING/CANCELLING, so a meta write after DONE could re-create a collection dropped in
    between."""
    from archon_search.server.routes_collections import _migration_task
    from archon_search.types import MigrationSpec

    job_store = JobStore(path=tmp_path / "jobs.json")
    running_job = job_store.create_migration(
        collection="my-col", kind=MigrationKind.REWRITE, backup_confirmed=True, status=JobStatus.RUNNING
    )
    spec = MigrationSpec(name="rebuild", kind=MigrationKind.REWRITE, description="test", introduced_at=1)
    status_at_meta_write: list[JobStatus] = []

    async def apply_rewrite_migration(*_args: Any, **_kwargs: Any) -> int:
        status_at_meta_write.append(job_store.get(running_job.job_id).status)  # type: ignore[union-attr]
        return 7

    search_store = MagicMock()
    search_store.apply_rewrite_migration = AsyncMock(side_effect=apply_rewrite_migration)

    await _migration_task(job=running_job, job_store=job_store, search_store=search_store, spec=spec)

    assert status_at_meta_write == [JobStatus.RUNNING]
    final = job_store.get(running_job.job_id)
    assert final is not None
    assert (final.status, final.result) == (JobStatus.DONE, {"migrated_chunks": 7})


@pytest.mark.asyncio
async def test_migration_task_spec_none_fetches_pending_and_uses_first_rewrite(tmp_path: Path) -> None:
    """_migration_task with spec=None fetches pending migrations and uses the first REWRITE spec.

    This exercises the scheduler resume path where the job arrives already RUNNING.
    """
    from archon_search.jobs.store import JobStore
    from archon_search.types import MigrationKind, MigrationSpec, JobStatus
    from archon_search.server.routes_collections import _migration_task
    from unittest.mock import AsyncMock, MagicMock

    job_store = JobStore(path=tmp_path / "jobs.json")
    queued_job = job_store.create_migration(collection="my-col", kind=MigrationKind.REWRITE, backup_confirmed=True)
    # Simulate the scheduler's _tick() transitioning QUEUED → RUNNING before dispatch.
    running_job = job_store.transition(queued_job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
    assert running_job is not None

    spec = MigrationSpec(name="rebuild", kind=MigrationKind.REWRITE, description="test", introduced_at=1)

    mock_search_store = MagicMock()
    mock_search_store.pending_migrations = AsyncMock(return_value=[spec])
    mock_search_store.apply_rewrite_migration = AsyncMock(return_value=42)

    await _migration_task(job=running_job, job_store=job_store, search_store=mock_search_store, spec=None)

    final_job = job_store.get(queued_job.job_id)
    assert final_job is not None
    assert final_job.status == JobStatus.DONE
    assert final_job.result == {"migrated_chunks": 42}
    mock_search_store.pending_migrations.assert_called_once_with("my-col", running_job.namespace)


@pytest.mark.asyncio
async def test_migration_task_spec_none_no_rewrite_pending_marks_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """_migration_task with spec=None marks job FAILED when no pending REWRITE migration exists.

    This exercises the scheduler resume path where the REWRITE spec has already been
    applied (or never existed) by the time the scheduler picks up the job. The curated,
    expected failure is logged as a warning, without a traceback.
    """
    import logging
    from archon_search.jobs.store import JobStore
    from archon_search.types import MigrationKind, JobStatus
    from archon_search.server.routes_collections import _migration_task
    from unittest.mock import AsyncMock, MagicMock

    job_store = JobStore(path=tmp_path / "jobs.json")
    queued_job = job_store.create_migration(collection="my-col", kind=MigrationKind.REWRITE, backup_confirmed=True)
    # Simulate the scheduler's _tick() transitioning QUEUED → RUNNING before dispatch.
    running_job = job_store.transition(queued_job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
    assert running_job is not None

    mock_search_store = MagicMock()
    mock_search_store.pending_migrations = AsyncMock(return_value=[])

    with caplog.at_level(logging.WARNING, logger="archon_search.server.routes_collections"):
        await _migration_task(job=running_job, job_store=job_store, search_store=mock_search_store, spec=None)

    final_job = job_store.get(queued_job.job_id)
    assert final_job is not None
    assert final_job.status == JobStatus.FAILED
    assert "no pending REWRITE" in (final_job.error or "")
    failure_logs = [r for r in caplog.records if "no pending REWRITE" in r.getMessage()]
    assert [r.levelno for r in failure_logs] == [logging.WARNING], caplog.text
    assert failure_logs[0].exc_info is None
    assert "Traceback" not in caplog.text


# ---------------------------------------------------------------------------
# bug-090 — collection responses surface the stored description (not "")
# ---------------------------------------------------------------------------


def _app_with_meta_store(
    tmp_path: Path,
    tmp_store: JobStore,
    *,
    description: str | None = None,
    has_meta: bool = True,
):
    """Build ``(client, name)`` for a single default-namespace collection app.

    The mock store returns a ``CollectionMeta`` (built here with the
    config-derived name so the namespace gate passes) from both
    ``get_all_collections_meta`` (list) and ``get_collection_meta``
    (detail/patch), so the same fixture drives all three handlers. Pass
    ``has_meta=False`` to model a config-only collection with no meta row.
    """
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = (
        CollectionMeta(name=name, namespace="default", description=description)
        if has_meta
        else None
    )
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[] if meta is None else [meta])
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.count_chunks = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"}), name


def test_list_collections_surfaces_stored_description(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/ returns the stored description, not a hardcoded ""."""
    c, name = _app_with_meta_store(tmp_path, tmp_store, description="A handbook about widgets.")

    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["description"] == "A handbook about widgets."


def test_list_collections_description_falls_back_to_empty_when_none(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """A collection whose meta.description is None reports "" (no None leak).

    Discriminates against a fix that drops the ``or ""`` coercion: a bare
    ``col_meta.description`` would surface ``None``, which the ``str``-typed
    ``CollectionSummary.description`` rejects with a 500.
    """
    c, name = _app_with_meta_store(tmp_path, tmp_store, description=None)

    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["description"] == ""


def test_list_collections_no_meta_row_description_empty(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """A config-only collection with no meta row reports "" without crashing.

    Guards the ``col_meta is None`` branch: a bare ``col_meta.description``
    would raise AttributeError here.
    """
    c, name = _app_with_meta_store(tmp_path, tmp_store, has_meta=False)

    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == name
    assert data[0]["description"] == ""


def test_get_collection_info_surfaces_stored_description(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns the stored description, not a hardcoded ""."""
    c, name = _app_with_meta_store(tmp_path, tmp_store, description="Detailed summary.")

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    assert response.json()["description"] == "Detailed summary."


def test_get_collection_info_description_none_falls_back_to_empty(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} reports "" when meta.description is None."""
    c, name = _app_with_meta_store(tmp_path, tmp_store, description=None)

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    assert response.json()["description"] == ""


def test_patch_collection_surfaces_stored_description(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH /collections/{name} (no-op body) returns the stored description."""
    c, name = _app_with_meta_store(tmp_path, tmp_store, description="Patched view.")

    # No-op PATCH (empty body) still rebuilds the CollectionDetail response.
    response = c.patch(f"/collections/{name}", json={})
    assert response.status_code == 200
    assert response.json()["description"] == "Patched view."


def test_patch_collection_description_survives_field_change(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH with a real field change preserves the description.

    A ``default_ttl_seconds`` change rebuilds ``meta`` via ``dataclasses.replace``
    before the response is built; this pins that ``description`` survives that
    reassignment (a replace that dropped/renamed ``description`` would break only
    this mutate path, which the no-op test cannot catch).
    """
    c, name = _app_with_meta_store(tmp_path, tmp_store, description="Kept through TTL change.")

    response = c.patch(f"/collections/{name}", json={"default_ttl_seconds": 3600})
    assert response.status_code == 200
    assert response.json()["description"] == "Kept through TTL change."


# ---------------------------------------------------------------------------
# POST /collections/{name}/reindex-metadata — CSP120 BE-4
# ---------------------------------------------------------------------------


def _make_reindex_metadata_app(
    tmp_path: Path,
    tmp_store: JobStore,
    *,
    meta_kwargs: dict | None = None,
    has_meta: bool = True,
    in_config: bool = True,
) -> tuple:
    """Helper: create an app with one collection and a mock search_store for reindex-metadata tests."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    if in_config:
        cfg.collections = [str(src)]

    name = path_to_collection_name(str(src))
    extra = meta_kwargs or {}
    meta = CollectionMeta(name=name, namespace="default", **extra) if has_meta else None

    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=asyncio.Lock())

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return c, name, meta, mock_store


def test_reindex_metadata_returns_202_running(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/reindex-metadata → 202 + RUNNING JobResponse."""
    c, name, meta, mock_store = _make_reindex_metadata_app(tmp_path, tmp_store)

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post(f"/collections/{name}/reindex-metadata", json={})

    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == "RUNNING"


def test_reindex_metadata_404_collection_not_in_config(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/reindex-metadata → 404 when collection absent from config."""
    c, name, meta, mock_store = _make_reindex_metadata_app(
        tmp_path, tmp_store, in_config=False
    )

    response = c.post(f"/collections/{name}/reindex-metadata", json={})

    assert response.status_code == 404


def test_reindex_metadata_404_meta_row_absent(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/reindex-metadata → 404 when meta row is absent."""
    c, name, meta, mock_store = _make_reindex_metadata_app(
        tmp_path, tmp_store, has_meta=False
    )

    response = c.post(f"/collections/{name}/reindex-metadata", json={})

    assert response.status_code == 404


@pytest.mark.parametrize("active_status", [JobStatus.RUNNING, JobStatus.CANCELLING])
def test_reindex_metadata_409_duplicate_submission(
    tmp_path: Path, tmp_store: JobStore, active_status: JobStatus
) -> None:
    """POST /collections/{name}/reindex-metadata → 409 when metadata_reindex_job_id is active.

    CANCELLING counts: DELETE /jobs/{id} does not stop the running task.
    """
    from archon_search.types import MetadataReindexJob, JobKind
    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    running_job = MetadataReindexJob(
        job_id=str(uuid.uuid4()),
        status=active_status,
        created_at=now,
        updated_at=now,
        namespace="default",
        collection="",
        kind=JobKind.metadata_reindex,
    )
    tmp_store.create_job(running_job)

    c, name, meta, mock_store = _make_reindex_metadata_app(
        tmp_path, tmp_store, meta_kwargs={"metadata_reindex_job_id": running_job.job_id}
    )

    response = c.post(f"/collections/{name}/reindex-metadata", json={})

    assert response.status_code == 409
    assert "metadata reindex already in progress" in response.json()["detail"]


def _poll_reindex_job_until_terminal(
    job_store: JobStore, job_id: str, timeout: float = 5.0
) -> object:
    """Poll job_store.get until status is terminal. Returns the terminal job."""
    import time

    terminal = {JobStatus.DONE, JobStatus.FAILED, JobStatus.FAILED_EXPIRED, JobStatus.CANCELLED}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = job_store.get(job_id)
        if job is not None and job.status in terminal:
            return job
        time.sleep(0.05)
    pytest.fail(f"Job {job_id!r} did not reach a terminal status within {timeout}s")


def test_reindex_metadata_task_done_path_and_flag_forwarding(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """Background task runs to DONE; dry_run/normalize_timestamps forwarded to reindex_metadata.

    T1: task DONE path is exercised.
    T2: flag forwarding (dry_run=True, normalize_timestamps=False) asserted on the store call.
    """
    from archon_search.store import ReindexResult

    result_obj = ReindexResult(processed=3, updated=2, skipped=1, ts_normalized=0, warnings=["w"])

    # Update the mock_store's reindex_metadata BEFORE making the request so
    # the task picks it up when it runs.
    c, name, meta, mock_store = _make_reindex_metadata_app(tmp_path, tmp_store)
    mock_store.reindex_metadata = AsyncMock(return_value=result_obj)

    # Do NOT patch asyncio.create_task — let the task actually run.
    response = c.post(
        f"/collections/{name}/reindex-metadata",
        json={"dry_run": True, "normalize_timestamps": False},
    )

    assert response.status_code == 202
    job_id = response.json()["job_id"]

    # Poll until the background task reaches a terminal state.
    terminal_job = _poll_reindex_job_until_terminal(tmp_store, job_id)

    assert terminal_job.status == JobStatus.DONE, (
        f"Expected DONE, got {terminal_job.status!r}; error={getattr(terminal_job, 'error', None)!r}"
    )
    # Verify the result fields are stored.
    assert terminal_job.result is not None
    assert terminal_job.result["processed"] == 3
    assert terminal_job.result["updated"] == 2
    assert terminal_job.result["warnings"] == ["w"]

    # T2: assert the store was called with the forwarded flags.
    mock_store.reindex_metadata.assert_called_once_with(
        name,
        dry_run=True,
        normalize_timestamps=False,
    )


@pytest.mark.parametrize(("body", "expected_dry_run"), [({}, False), ({"dry_run": False}, False), ({"dry_run": True}, True)])
def test_reindex_metadata_records_dry_run_on_its_job(
    tmp_path: Path, tmp_store: JobStore, body: dict[str, bool], expected_dry_run: bool
) -> None:
    """The job the route creates carries the request's ``dry_run`` (DELETE /collections/{name} reads it from the job
    store, never from the request): off unless asked."""
    c, name, _meta, mock_store = _make_reindex_metadata_app(tmp_path, tmp_store)
    mock_store.reindex_metadata = AsyncMock(return_value=MagicMock(
        processed=0, updated=0, skipped=0, ts_normalized=0, warnings=[]
    ))

    response = c.post(f"/collections/{name}/reindex-metadata", json=body)

    assert response.status_code == 202, response.text
    job = _poll_reindex_job_until_terminal(tmp_store, response.json()["job_id"])
    assert job.dry_run is expected_dry_run


def test_reindex_metadata_task_failed_path(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """Background task transitions to FAILED on OSError; metadata_reindex_job_id is cleared.

    T5: FAILED branch (except Exception) is exercised.
    T4: id is cleared in finally even on failure, so a re-submit returns 202.
    """
    c, name, meta, mock_store = _make_reindex_metadata_app(tmp_path, tmp_store)
    mock_store.reindex_metadata = AsyncMock(side_effect=OSError("table dropped"))

    response = c.post(f"/collections/{name}/reindex-metadata", json={})
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    # Poll until terminal (expect FAILED).
    terminal_job = _poll_reindex_job_until_terminal(tmp_store, job_id)
    assert terminal_job.status == JobStatus.FAILED
    # The store exception stays in the server log; the job carries the sanitized text.
    from archon_search.server.routes_collections import METADATA_REINDEX_FAILED_DETAIL

    assert terminal_job.error == METADATA_REINDEX_FAILED_DETAIL
    assert "table dropped" not in terminal_job.error

    # T4: after failure, the finally block should have cleared metadata_reindex_job_id,
    # so a second POST must return 202 (not 409).
    # update_collection_meta is a mock, so reset side effects to a fresh return.
    # Re-create the mock meta to reflect cleared id (get_collection_meta returns fresh meta).
    from archon_search.collection_meta import CollectionMeta

    fresh_meta = CollectionMeta(name=name, namespace="default")
    mock_store.get_collection_meta = AsyncMock(return_value=fresh_meta)
    mock_store.reindex_metadata = AsyncMock(return_value=MagicMock(
        processed=0, updated=0, skipped=0, ts_normalized=0, warnings=[]
    ))

    response2 = c.post(f"/collections/{name}/reindex-metadata", json={})
    assert response2.status_code == 202, (
        f"Expected 202 on re-submit after FAILED, got {response2.status_code}: {response2.text}"
    )


@pytest.mark.parametrize(
    ("reindex_outcome", "terminal_status"),
    [
        pytest.param({"return_value": MagicMock(processed=0, updated=0, skipped=0, ts_normalized=0, warnings=[])},
                     JobStatus.DONE, id="done"),
        pytest.param({"side_effect": OSError("store failure")}, JobStatus.FAILED, id="failed"),
    ],
)
def test_reindex_metadata_task_clears_job_id_before_terminal_status(
    tmp_store: JobStore, reindex_outcome: dict[str, Any], terminal_status: JobStatus
) -> None:
    """The meta-pointer clear (an unlocked meta read + upsert) runs while the job is still RUNNING.

    DELETE /collections/{name} refuses only while the job is RUNNING/CANCELLING: a clear after the terminal
    write could upsert the meta row of a collection DELETE dropped in between, re-creating it.
    """
    from archon_search.server.routes_collections import _reindex_metadata_task

    job = tmp_store.create_metadata_reindex(collection="col")
    meta = CollectionMeta(name="col", namespace="default", metadata_reindex_job_id=job.job_id)
    status_at_clear: list[JobStatus] = []
    search_store = MagicMock()
    search_store.reindex_metadata = AsyncMock(**reindex_outcome)
    search_store.get_collection_meta = AsyncMock(return_value=meta)
    search_store.update_collection_meta = AsyncMock(
        side_effect=lambda _meta: status_at_clear.append(tmp_store.get(job.job_id).status)
    )

    asyncio.run(_reindex_metadata_task(job, tmp_store, search_store, dry_run=False, normalize_timestamps=False))

    assert status_at_clear == [JobStatus.RUNNING]
    assert meta.metadata_reindex_job_id is None
    assert tmp_store.get(job.job_id).status == terminal_status


def test_reindex_metadata_task_done_write_failure_ends_failed_after_clearing_the_pointer(
    tmp_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing DONE write leaves the job FAILED with the sanitized detail (not RUNNING forever), after the meta
    pointer was cleared; the log tells the failed DONE write apart from a failed reindex."""
    from archon_search.server.routes_collections import METADATA_REINDEX_FAILED_DETAIL, _reindex_metadata_task

    job = tmp_store.create_metadata_reindex(collection="col")
    meta = CollectionMeta(name="col", namespace="default", metadata_reindex_job_id=job.job_id)
    search_store = MagicMock()
    search_store.reindex_metadata = AsyncMock(
        return_value=MagicMock(processed=1, updated=1, skipped=0, ts_normalized=0, warnings=[])
    )
    search_store.get_collection_meta = AsyncMock(return_value=meta)
    search_store.update_collection_meta = AsyncMock()
    real_update = tmp_store.update

    def update(job_id: str, **fields: Any) -> None:
        if fields.get("status") == JobStatus.DONE:
            raise OSError("simulated DONE write failure")
        real_update(job_id, **fields)

    with patch.object(tmp_store, "update", side_effect=update), caplog.at_level("ERROR"):
        asyncio.run(_reindex_metadata_task(job, tmp_store, search_store, dry_run=False, normalize_timestamps=False))

    final = tmp_store.get(job.job_id)
    assert final is not None
    assert (final.status, final.error) == (JobStatus.FAILED, METADATA_REINDEX_FAILED_DETAIL)
    assert meta.metadata_reindex_job_id is None
    search_store.update_collection_meta.assert_awaited_once()
    messages = [r.getMessage() for r in caplog.records]
    assert f"_reindex_metadata_task: could not record DONE for job {job.job_id}" in messages, messages
    assert f"_reindex_metadata_task: job {job.job_id} failed" not in messages, messages


async def test_reindex_metadata_task_cancelled_clears_the_pointer_and_leaves_the_job_running(
    tmp_store: JobStore,
) -> None:
    """A task cancelled mid-reindex (server shutdown) still clears its meta pointer, then re-raises the cancellation
    without writing a terminal status: the job stays RUNNING for the next start to fail as ``process_restart``."""
    from archon_search.server.routes_collections import _reindex_metadata_task

    job = tmp_store.create_metadata_reindex(collection="col")
    meta = CollectionMeta(name="col", namespace="default", metadata_reindex_job_id=job.job_id)
    reindexing = asyncio.Event()

    async def reindex_until_cancelled(*_args: Any, **_kwargs: Any) -> None:
        reindexing.set()
        await asyncio.Event().wait()

    search_store = MagicMock()
    search_store.reindex_metadata = AsyncMock(side_effect=reindex_until_cancelled)
    search_store.get_collection_meta = AsyncMock(return_value=meta)
    search_store.update_collection_meta = AsyncMock()
    task = asyncio.create_task(
        _reindex_metadata_task(job, tmp_store, search_store, dry_run=False, normalize_timestamps=False)
    )
    await asyncio.wait_for(reindexing.wait(), timeout=_ASYNC_STEP_TIMEOUT_S)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=_ASYNC_STEP_TIMEOUT_S)

    assert meta.metadata_reindex_job_id is None
    search_store.update_collection_meta.assert_awaited_once_with(meta)
    assert tmp_store.get(job.job_id).status == JobStatus.RUNNING


@pytest.mark.parametrize("stale_status", [JobStatus.DONE, JobStatus.QUEUED, JobStatus.PENDING])
def test_reindex_metadata_stale_job_id_clears_and_returns_202(
    tmp_path: Path, tmp_store: JobStore, stale_status: JobStatus
) -> None:
    """Stale metadata_reindex_job_id pointing at a DONE job is lazy-cleared; 202 returned.

    T3: the stale-job lazy-clear path (guard 3) is exercised. A QUEUED/PENDING job (written by an older
    server, which created it QUEUED first) is stale too: nothing schedules a MetadataReindexJob, so it
    would never run and would otherwise answer 409 forever. Such a job is also marked FAILED, so it
    becomes terminal (evictable, deletable).
    """
    from archon_search.types import MetadataReindexJob, JobKind
    import uuid
    from datetime import datetime, timezone

    # Create a job in a status that will never run again.
    now = datetime.now(timezone.utc).isoformat()
    done_job = MetadataReindexJob(
        job_id=str(uuid.uuid4()),
        status=stale_status,
        created_at=now,
        updated_at=now,
        namespace="default",
        collection="",
        kind=JobKind.metadata_reindex,
    )
    tmp_store.create_job(done_job)

    # Point meta at the DONE job — simulates stale id after a previous run.
    c, name, meta, mock_store = _make_reindex_metadata_app(
        tmp_path, tmp_store, meta_kwargs={"metadata_reindex_job_id": done_job.job_id}
    )

    from unittest.mock import patch

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post(f"/collections/{name}/reindex-metadata", json={})

    # Stale DONE id should be cleared and a new 202 returned.
    assert response.status_code == 202, (
        f"Expected 202 for stale DONE job_id, got {response.status_code}: {response.text}"
    )
    new_job_id = response.json()["job_id"]
    assert new_job_id != done_job.job_id, "New job must have a fresh job_id"
    # A legacy QUEUED/PENDING row is marked FAILED so it does not stay non-terminal (never evicted) forever;
    # a terminal job is left untouched.
    from archon_search.server.routes_jobs import JOB_SUPERSEDED_DETAIL

    old_job = tmp_store.get(done_job.job_id)
    if stale_status is JobStatus.DONE:
        assert old_job.status is JobStatus.DONE
        assert old_job.error is None
    else:
        assert old_job.status is JobStatus.FAILED
        assert old_job.error == JOB_SUPERSEDED_DETAIL


# ---------------------------------------------------------------------------
# AddCollectionRequest schema contract — BE-1 (brief 220)
# ---------------------------------------------------------------------------


def test_add_collection_request_has_no_collection_name_field() -> None:
    """AddCollectionRequest must have exactly path and embedding_model — no collection_name.

    Contract C1 (brief 220): the server derives the collection name via
    path_to_collection_name; the client never sends a name override.
    """
    from archon_search.server.routes_collections import AddCollectionRequest

    field_names = set(AddCollectionRequest.model_fields)
    assert field_names == {"path", "embedding_model"}, (
        f"AddCollectionRequest has unexpected fields: {field_names}. "
        "collection_name must not be a field — the server derives it from path."
    )


# ---------------------------------------------------------------------------
# CollectionDetail schema — default_ttl_seconds + schema_version (brief 350)
# ---------------------------------------------------------------------------


def test_get_collection_info_includes_ttl_and_schema_version(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns default_ttl_seconds and schema_version (brief 350)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(
        name=name,
        namespace="default",
        active_embedding_model=cfg.embedding_model,
        default_ttl_seconds=3600,
        schema_version=1,
    )
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.count_chunks = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    data = response.json()
    assert data["default_ttl_seconds"] == 3600
    assert data["schema_version"] == 1


def test_get_collection_info_ttl_null_when_not_set(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns default_ttl_seconds=null when unset (brief 350)."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default", active_embedding_model=cfg.embedding_model)
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=0)
    mock_store.count_chunks = AsyncMock(return_value=0)
    mock_store.get_acl_stats = AsyncMock(return_value=(0, 0))
    mock_store.get_collection_meta = AsyncMock(return_value=meta)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    data = response.json()
    assert data["default_ttl_seconds"] is None
    assert data["schema_version"] == 0


# ---------------------------------------------------------------------------
# S284 — collection add with nonexistent path: behavioral + doc-consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_ingest_raises_for_nonexistent_path() -> None:
    """_dispatch_ingest raises IngestPathNotFoundError when the path does not exist (S284).

    This proves the wire-truth: a nonexistent path → FAILED job whose error is this
    curated message, because both ingest tasks record IngestPathNotFoundError's text
    verbatim (tests/test_no_internal_error_leak_jobs.py drives that end to end).
    """
    from archon_search.server.routes_jobs import (
        INGEST_PATH_NOT_FOUND_PREFIX,
        IngestPathNotFoundError,
        IngestRequest,
        _dispatch_ingest,
    )

    body = IngestRequest(
        collection="test-col",
        path="/nonexistent/archon/s284/path",
    )
    mock_store = MagicMock()
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_embedder_cache = MagicMock()
    mock_embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())
    mock_config = MagicMock()
    mock_config.embedding_model = "test-model"

    with pytest.raises(IngestPathNotFoundError) as excinfo:
        await _dispatch_ingest(
            body,
            namespace="default",
            search_store=mock_store,
            embedder_cache=mock_embedder_cache,
            pipeline=MagicMock(),
            config=mock_config,
        )
    assert isinstance(excinfo.value, FileNotFoundError)
    assert str(excinfo.value) == f"{INGEST_PATH_NOT_FOUND_PREFIX}: /nonexistent/archon/s284/path"


def test_nonexistent_path_behavior_documented() -> None:
    """User manual documents that a nonexistent path produces a FAILED job (S284).

    The doc section for ``collection add`` must mention the FAILED-job outcome
    so users know what to expect when the path doesn't exist.
    """
    doc_path = (
        Path(__file__).resolve().parent.parent
        / "Documentation"
        / "UserManual"
        / "50_ingestion_and_collections.md"
    )
    content = doc_path.read_text()
    # Scope to the collection-add section only
    start = content.index("### `archon-search collection add")
    # Find the next h3 section
    next_h3 = content.index("\n### ", start + 1)
    section = content[start:next_h3]
    assert "does not exist" in section.lower() or "nonexistent" in section.lower(), (
        "S284: the 'collection add' doc section must describe the behavior "
        "when the path does not exist (FAILED job)"
    )


# ---------------------------------------------------------------------------
# S630 — config-only collection: listed but unaddressable (list vs info/remove)
# ---------------------------------------------------------------------------


def test_s630_config_only_collection_listed_but_info_and_remove_404(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """S630: a collection registered in config but with NO meta-store row is shown
    by GET /collections/ yet GET /collections/{name} and DELETE /collections/{name}
    return 404. The list and the info/remove surfaces must agree; today they don't.
    """
    src = tmp_path / "s630-docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    # Config path registered, but NO meta row anywhere (default namespace).
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.get_collection_meta = AsyncMock(return_value=None)
    mock_store.count_chunks = AsyncMock(return_value=0)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.lock_for = MagicMock(return_value=asyncio.Lock())
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    name = path_to_collection_name(str(src))

    # GET /collections/ lists the config-only collection.
    list_resp = c.get("/collections/")
    assert list_resp.status_code == 200
    listed_names = [e["name"] for e in list_resp.json()]
    assert name in listed_names, f"S630: {name!r} must be listed, got {listed_names}"

    # But GET /collections/{name} and DELETE /collections/{name} must NOT 404 for a
    # listed collection. Today they do — this is the S630 bug.
    info_resp = c.get(f"/collections/{name}")
    assert info_resp.status_code != 404, (
        "S630: a listed collection must be addressable via GET /collections/{name}, "
        f"but got 404 for {name!r}"
    )

    remove_resp = c.delete(f"/collections/{name}")
    assert remove_resp.status_code != 404, (
        "S630: a listed collection must be addressable via DELETE /collections/{name}, "
        f"but got 404 for {name!r}"
    )
