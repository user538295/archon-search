"""Tests for GET/POST/DELETE /collections/* endpoints ."""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.model import JobStatus
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.sync import path_to_collection_name


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
    mock_store._lock_for = MagicMock(return_value=_asyncio.Lock())
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
    mock_store._lock_for = MagicMock(return_value=_asyncio.Lock())
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
    from archon_search.collection_meta import CollectionMeta as _CM
    _meta = _CM(name=path_to_collection_name(str(src)), namespace="default")
    mock_search_store = MagicMock()
    mock_search_store.get_collection_meta = AsyncMock(return_value=_meta)
    mock_search_store.drop_collection = AsyncMock()
    mock_search_store.delete_collection_meta = AsyncMock()
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
    mock_search_store.drop_collection.assert_called_once_with(name)


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


def test_reindex_unknown_collection_returns_404(client: TestClient) -> None:
    """POST /collections/{name}/reindex for unknown collection returns 404."""
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

    meta_a = CollectionMeta(name=name_a, namespace="tenantA")
    meta_b = CollectionMeta(name=name_b, namespace="tenantB")

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
    mock_store._lock_for = MagicMock(return_value=_asyncio.Lock())

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
    mock_store._lock_for = MagicMock(return_value=_asyncio.Lock())

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
    mock_store._lock_for = MagicMock(return_value=_asyncio.Lock())

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
    mock_store._lock_for = MagicMock(return_value=_asyncio.Lock())

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

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

    c = TestClient(app, headers={"Authorization": f"Bearer {caller_key}"})
    response = c.delete(f"/collections/{name}")

    assert response.status_code == 200
    mock_store.drop_collection.assert_called_once_with(name)
    mock_store.delete_collection_meta.assert_called_once_with(name, caller_ns)
    assert deleted["done"] is True


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
    mock_store._lock_for = MagicMock(return_value=_asyncio.Lock())

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
    mock_store._lock_for = MagicMock(return_value=_asyncio.Lock())
    app.state.search_store = mock_store

    # Patch the validator in the route module namespace to return a sentinel path.
    monkeypatch.setattr(
        "archon_search.server.routes_collections.validate_ingest_path",
        lambda raw: Path("/sentinel/value"),
    )

    # Capture the IngestRequest passed to either ingest task variant.
    # The handler branches to _default_ingest_task_with_lock when lock_result is not None.
    captured: list[str] = []

    # Must stay await-free: the assertion below relies on this completing in a single
    # event-loop step before the response is returned (no await point => no race).
    async def _capturing_ingest_task(job_id, store, body, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(body.path)

    monkeypatch.setattr(
        "archon_search.server.routes_collections._default_ingest_task",
        _capturing_ingest_task,
    )
    monkeypatch.setattr(
        "archon_search.server.routes_collections._default_ingest_task_with_lock",
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
    mock_store._lock_for = MagicMock(return_value=_asyncio.Lock())
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
    search_store._lock_for = MagicMock(return_value=_asyncio.Lock())

    with mock.patch("archon_search.server.app.DocumentChunker"):
        app = create_app(cfg, job_store)
    app.state.search_store = search_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}


def test_collection_reindex_oserror_returns_500_envelope(
    tmp_path: Path,
) -> None:
    """POST /collections/{name}/reindex returns the 500 envelope when store.create raises OSError."""
    from archon_search.collection_meta import CollectionMeta

    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]

    name = path_to_collection_name(str(src))
    meta = CollectionMeta(name=name, namespace="default")

    job_store = MagicMock()
    job_store.create_reindex.side_effect = OSError("disk full")

    search_store = MagicMock()
    search_store.get_collection_meta = AsyncMock(return_value=meta)
    search_store.migrate_namespace = AsyncMock()
    search_store.migrate_acl = AsyncMock()
    search_store.connect = AsyncMock()
    search_store.disconnect = AsyncMock()

    with mock.patch("archon_search.server.app.DocumentChunker"):
        app = create_app(cfg, job_store)
    app.state.search_store = search_store
    app.state.embedder_cache = MagicMock()
    app.state.pipeline = MagicMock()

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post(f"/collections/{name}/reindex")

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
    mock_store._lock_for = MagicMock(return_value=_asyncio.Lock())

    app = _make_app_with_mock_store(cfg, tmp_store, mock_store)

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 503
    assert "Retry-After" in response.headers
    # Config must be reverted — path should not be persisted on busy lock
    updated_config: SearchConfig = app.state.config
    assert str(src.resolve()) not in updated_config.collections


# ---------------------------------------------------------------------------
# PATCH /collections/{name} — per-collection embedding model (Task 5.1)
# ---------------------------------------------------------------------------


def _make_patch_app(
    tmp_path: Path,
    tmp_store: JobStore,
    *,
    meta,
    count_chunks: int = 5,
    stored_dim: int | None = None,
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
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=stored_dim)
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


def test_patch_returns_422_on_dimension_mismatch(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH returns 422 when new model dimension differs from stored vectors."""
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
    mock_store.get_stored_vector_dimension = AsyncMock(return_value=384)  # stored dim
    mock_store.update_collection_meta = AsyncMock()
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

    assert response.status_code == 422
    assert "dimension mismatch" in response.json()["detail"]


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


def test_patch_missing_embedding_model_returns_422(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH without embedding_model field returns 422."""
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

    response = c.patch(f"/collections/{name}", json={})
    assert response.status_code == 422


def test_patch_null_embedding_model_returns_422(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """PATCH with null embedding_model returns 422."""
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

    response = c.patch(f"/collections/{name}", json={"embedding_model": None})
    assert response.status_code == 422


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
    mock_store._lock_for = MagicMock(return_value=_asyncio.Lock())
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
    from archon_search.collection_meta import CollectionMeta

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
    from archon_search.jobs.model import IngestJob

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
    import os
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
    import os
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
    from archon_search.server.routes_collections import _migration_task
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
    assert "lancedb crashed" in (final_job.error or "")


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
async def test_migration_task_spec_none_no_rewrite_pending_marks_failed(tmp_path: Path) -> None:
    """_migration_task with spec=None marks job FAILED when no pending REWRITE migration exists.

    This exercises the scheduler resume path where the REWRITE spec has already been
    applied (or never existed) by the time the scheduler picks up the job.
    """
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

    await _migration_task(job=running_job, job_store=job_store, search_store=mock_search_store, spec=None)

    final_job = job_store.get(queued_job.job_id)
    assert final_job is not None
    assert final_job.status == JobStatus.FAILED
    assert "no pending REWRITE" in (final_job.error or "")
