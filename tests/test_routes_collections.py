"""Tests for GET/POST/DELETE /collections/* endpoints (Task 5.7)."""
from __future__ import annotations

import os
from pathlib import Path
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
    src = tmp_path / "myproject"
    src.mkdir()
    app = create_app(config, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
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
    src = tmp_path / "myproject"
    src.mkdir()
    app = create_app(config, tmp_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
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
    meta = CollectionMeta(name=name, namespace="default")
    mock_store = MagicMock()
    mock_store.count_documents = AsyncMock(return_value=0)
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
    assert "embedding_model" in data
    assert "centroid_present" in data
    assert "last_indexed" in data

    # Fix 8: assert embedding_model value matches config
    assert data["embedding_model"] == cfg.embedding_model


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
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

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
# GET /collections/{name} — real doc_count and centroid_present (Task 3.1)
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
# GET /collections/ namespace filter (Task 4.1 — FEAT-043)
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
# namespace field in GET /collections/ (Task 4.1 — FEAT-042)
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
# namespace field in GET /collections/{name} (Task 4.2 — FEAT-042)
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
# POST /collections/ — namespace enforcement (Task 4.2 — FEAT-043)
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
# DELETE /collections/{name} — namespace enforcement (Task 4.3 — FEAT-043)
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
# GET /collections/{name} — namespace enforcement (Task 4.4 — FEAT-043)
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
    # would return None after Task 1.3 (defaults to DEFAULT_NAMESPACE), so
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
# POST /collections/{name}/reindex — namespace enforcement (Task 4.5 — FEAT-043)
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
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

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
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    app = create_app(cfg, tmp_store)
    app.state.search_store = mock_store

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
