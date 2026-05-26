"""Tests for GET /status endpoint ."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus, IndexingStateStore
from archon_search.server.app import create_app


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "search"
    db.mkdir()
    return db


def _make_client_with_state(tmp_db: Path, state: IndexingState) -> TestClient:
    store = IndexingStateStore(tmp_db)
    store.write(state)
    config = SearchConfig()
    config.db_path = str(tmp_db)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)

    # Mock search_store so get_all_collections_meta() works without a real DB.
    # All collections in state are owned by the default namespace.
    from archon_search.constants import DEFAULT_NAMESPACE

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name=n, namespace=DEFAULT_NAMESPACE) for n in state.collections]
    )
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def test_status_returns_running_and_collections(tmp_db: Path) -> None:
    state = IndexingState(
        collections={
            "docs": CollectionProgress(
                status=IndexingStatus.DONE,
                total_files=10,
                processed_files=10,
            )
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    # service fields
    assert "running" in data
    assert "pid" in data
    assert "version" in data
    assert data["running"] is True
    assert isinstance(data["pid"], int)
    assert isinstance(data["version"], str)
    # collections
    assert "collections" in data
    assert isinstance(data["collections"], list)
    assert len(data["collections"]) == 1
    col = data["collections"][0]
    assert col["name"] == "docs"
    assert col["status"] == "done"
    assert col["doc_count"] == 0
    assert col["chunk_count"] == 0
    assert "watching" in col


def test_status_includes_eta_when_progress_known(tmp_db: Path) -> None:
    started = datetime.now(UTC).isoformat()
    state = IndexingState(
        collections={
            "big-repo": CollectionProgress(
                status=IndexingStatus.IN_PROGRESS,
                total_files=100,
                processed_files=50,
                started_at=started,
            )
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    col = data["collections"][0]
    assert col["name"] == "big-repo"
    assert col["status"] == "in_progress"
    assert "eta_seconds" in col
    assert col["eta_seconds"] is not None
    # Schema declares float; the underlying compute_eta_seconds returns int and Pydantic
    # coerces it to a JSON number — accept either Python type after JSON round-trip.
    assert isinstance(col["eta_seconds"], (int, float))


def test_status_includes_watching_flag(tmp_db: Path) -> None:
    state = IndexingState(
        collections={
            "watched": CollectionProgress(status=IndexingStatus.DONE)
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    col = data["collections"][0]
    assert "watching" in col
    assert isinstance(col["watching"], bool)


def test_status_includes_progress_and_error_fields(tmp_db: Path) -> None:
    state = IndexingState(
        collections={
            "failing": CollectionProgress(
                status=IndexingStatus.FAILED,
                total_files=20,
                processed_files=5,
                error="disk full",
                error_count=3,
            )
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    col = data["collections"][0]
    assert col["processed_files"] == 5
    assert col["total_files"] == 20
    assert col["error"] == "disk full"
    assert col["error_count"] == 3


def test_status_no_state_file(tmp_db: Path) -> None:
    """GET /status returns 200 with empty collections when no state file exists."""
    config = SearchConfig()
    config.db_path = str(tmp_db)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert data["collections"] == []


def test_status_empty_collections(tmp_db: Path) -> None:
    """GET /status returns 200 with empty collections when state has no collections."""
    state = IndexingState(collections={})
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert data["collections"] == []


def test_status_config_paths_converted_to_names(tmp_db: Path) -> None:
    """Config collections/pinned_collections paths are converted to collection names via path_to_collection_name()."""
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.sync import path_to_collection_name

    config = SearchConfig()
    config.db_path = str(tmp_db)
    config.collections = ["/some/path/my-docs"]
    config.pinned_collections = ["/notes"]
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)

    expected_collection_name = path_to_collection_name("/some/path/my-docs")
    expected_pinned_name = path_to_collection_name("/notes")

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(
        return_value=[
            CollectionMeta(name=expected_collection_name, namespace=DEFAULT_NAMESPACE),
            CollectionMeta(name=expected_pinned_name, namespace=DEFAULT_NAMESPACE),
        ]
    )
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()

    names = [col["name"] for col in data["collections"]]
    assert expected_collection_name in names
    assert expected_pinned_name in names

    # Both are not in state → status must be "not_yet_indexed"
    for col in data["collections"]:
        if col["name"] in (expected_collection_name, expected_pinned_name):
            assert col["status"] == "not_yet_indexed"


# ---------------------------------------------------------------------------
# GET /status namespace filter
# ---------------------------------------------------------------------------


def _make_client_with_namespace(
    tmp_db: Path,
    state: IndexingState,
    meta_rows: list[CollectionMeta],
    tenant_key: str,
) -> TestClient:
    """Create a TestClient with a specific namespace key and mocked search_store."""
    store = IndexingStateStore(tmp_db)
    store.write(state)
    config = SearchConfig()
    config.db_path = str(tmp_db)
    config.namespaces = {tenant_key: "tenantA"}
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=meta_rows)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    app.state.search_store = mock_store

    return TestClient(app, headers={"Authorization": f"Bearer {tenant_key}"})


def test_status_filters_by_namespace(tmp_db: Path) -> None:
    """GET /status with namespace='tenantA' returns only collections belonging to tenantA."""
    tenant_key = "a" * 64
    state = IndexingState(
        collections={
            "colA": CollectionProgress(status=IndexingStatus.DONE, total_files=5, processed_files=5),
            "colB": CollectionProgress(status=IndexingStatus.DONE, total_files=3, processed_files=3),
        }
    )
    meta_rows = [
        CollectionMeta(name="colA", namespace="tenantA"),
        CollectionMeta(name="colB", namespace="tenantB"),
    ]
    c = _make_client_with_namespace(tmp_db, state, meta_rows, tenant_key)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    names = [col["name"] for col in data["collections"]]
    assert "colA" in names
    assert "colB" not in names


def test_status_no_collections_for_namespace(tmp_db: Path) -> None:
    """GET /status with namespace='tenantA' returns empty list when no collections belong to it."""
    tenant_key = "b" * 64
    state = IndexingState(
        collections={
            "colX": CollectionProgress(status=IndexingStatus.DONE, total_files=2, processed_files=2),
        }
    )
    meta_rows = [
        CollectionMeta(name="colX", namespace="tenantB"),
    ]
    c = _make_client_with_namespace(tmp_db, state, meta_rows, tenant_key)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert data["collections"] == []


# ---------------------------------------------------------------------------
# Task 6.1 — readiness sub-object on GET /status
# ---------------------------------------------------------------------------


def _make_client_with_readiness(
    tmp_db: Path,
    *,
    ping_result: bool = True,
    watcher_manager: object = None,
    state: IndexingState | None = None,
) -> TestClient:
    """Build a TestClient with mocked search_store.ping and optionally set watcher_manager."""
    if state is None:
        state = IndexingState(collections={})
    store = IndexingStateStore(tmp_db)
    store.write(state)
    config = SearchConfig()
    config.db_path = str(tmp_db)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)

    from archon_search.constants import DEFAULT_NAMESPACE
    from unittest.mock import AsyncMock, MagicMock

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=ping_result)
    app.state.search_store = mock_store
    app.state.watcher_manager = watcher_manager

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def test_status_readiness_block_present(tmp_db: Path) -> None:
    c = _make_client_with_readiness(tmp_db)
    response = c.get("/status")
    assert response.status_code == 200
    assert "readiness" in response.json()


def test_status_readiness_has_all_fields(tmp_db: Path) -> None:
    c = _make_client_with_readiness(tmp_db)
    data = c.get("/status").json()
    readiness = data["readiness"]
    assert readiness is not None
    expected_keys = {
        "storage_connected", "embedder_warm", "reranker_warm",
        "jobs", "collections_indexing", "collections_failed", "watcher",
    }
    assert set(readiness.keys()) == expected_keys


def test_status_existing_fields_unchanged(tmp_db: Path) -> None:
    c = _make_client_with_readiness(tmp_db)
    data = c.get("/status").json()
    for field in ("running", "pid", "version", "collections"):
        assert field in data, f"field {field!r} missing from /status response"


def test_status_readiness_jobs_counts_correct(tmp_db: Path) -> None:
    from archon_search.types import JobStatus

    config = SearchConfig()
    config.db_path = str(tmp_db)
    job_store = JobStore(path=tmp_db / "jobs.json")

    # Create one PENDING job and one RUNNING job
    job_store.create(namespace="default")
    job2 = job_store.create(namespace="default")
    job_store.update(job2.job_id, status=JobStatus.RUNNING)

    app = create_app(config, job_store)
    from unittest.mock import AsyncMock, MagicMock

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    data = c.get("/status").json()
    jobs = data["readiness"]["jobs"]
    assert jobs["pending"] == 1
    assert jobs["running"] == 1


def test_status_returns_500_when_job_store_raises(tmp_db: Path) -> None:
    from unittest.mock import AsyncMock, MagicMock

    config = SearchConfig()
    config.db_path = str(tmp_db)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    app.state.search_store = mock_store

    # Break count_by_status
    app.state.job_store = MagicMock()
    app.state.job_store.count_by_status = MagicMock(side_effect=RuntimeError("disk error"))

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"}, raise_server_exceptions=False)
    response = c.get("/status")
    assert response.status_code == 500


def test_status_readiness_watcher_running_false_when_slot_is_none(tmp_db: Path) -> None:
    c = _make_client_with_readiness(tmp_db, watcher_manager=None)
    data = c.get("/status").json()
    assert data["readiness"]["watcher"] == {"running": False, "watching": []}


def test_status_readiness_watcher_running_true_with_stub_manager(tmp_db: Path) -> None:
    from unittest.mock import MagicMock

    stub = MagicMock()
    stub.watching_names.return_value = {"colB", "colA"}
    c = _make_client_with_readiness(tmp_db, watcher_manager=stub)
    data = c.get("/status").json()
    assert data["readiness"]["watcher"] == {"running": True, "watching": ["colA", "colB"]}


def test_status_failed_collection_reflected_in_collections_failed(tmp_db: Path) -> None:
    state = IndexingState(
        collections={
            "broken": CollectionProgress(status=IndexingStatus.FAILED),
        }
    )
    c = _make_client_with_readiness(tmp_db, state=state)
    data = c.get("/status").json()
    assert data["readiness"]["collections_failed"] == 1


def test_ready_not_affected_by_failed_collection(tmp_db: Path) -> None:
    """GET /ready only calls ping() — a FAILED collection does not affect its result."""
    from unittest.mock import AsyncMock, MagicMock

    state = IndexingState(
        collections={"broken": CollectionProgress(status=IndexingStatus.FAILED)}
    )
    store = IndexingStateStore(tmp_db)
    store.write(state)
    config = SearchConfig()
    config.db_path = str(tmp_db)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)

    mock_store = MagicMock()
    mock_store.ping = AsyncMock(return_value=True)
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    c = TestClient(app)
    response = c.get("/ready")
    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_status_readiness_embedder_warm_false_before_encode(tmp_db: Path) -> None:
    """embedder_warm is False on a cold pipeline (no encode call)."""
    c = _make_client_with_readiness(tmp_db)
    data = c.get("/status").json()
    assert data["readiness"]["embedder_warm"] is False


def test_status_still_requires_auth(tmp_db: Path) -> None:
    config = SearchConfig()
    config.db_path = str(tmp_db)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)
    c = TestClient(app)
    response = c.get("/status")
    assert response.status_code == 401


def test_status_readiness_storage_connected_reflects_ping(tmp_db: Path) -> None:
    c = _make_client_with_readiness(tmp_db, ping_result=False)
    data = c.get("/status").json()
    assert data["readiness"]["storage_connected"] is False


def test_status_response_readiness_always_populated_by_handler(tmp_db: Path) -> None:
    c = _make_client_with_readiness(tmp_db)
    data = c.get("/status").json()
    assert data["readiness"] is not None
