"""Integration tests for GET /status backup extension — D2 Task 4.2."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search._types import CollectionInfo
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


def _make_mock_search_store(collections: list[CollectionInfo] | None = None) -> MagicMock:
    mock = MagicMock()
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock.migrate_namespace = AsyncMock()
    mock.migrate_description_embedding = AsyncMock()
    mock.migrate_acl = AsyncMock()
    mock.migrate_centroid_sum = AsyncMock()
    mock.migrate_per_collection_model = AsyncMock()
    mock.ping = AsyncMock(return_value=True)
    cols = collections or []
    mock.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name=c.name, namespace=c.namespace) for c in cols]
    )
    mock.get_collection_meta = AsyncMock(return_value=None)
    mock.list_collections = AsyncMock(return_value=cols)
    mock.count_untagged_language_chunks = AsyncMock(return_value=0)
    return mock


def _make_client(
    tmp_path: Path,
    auth_headers: dict[str, str],
    collections: list[CollectionInfo],
    interval_hours: int = 1,
    exclude: list[str] | None = None,
) -> TestClient:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    Path(cfg.db_path).mkdir(parents=True, exist_ok=True)
    cfg.backup.output_dir = str(tmp_path / "backups")
    cfg.backup.exclude = exclude or []
    cfg.backup.interval_hours = interval_hours
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(cfg, job_store)
    app.state.search_store = _make_mock_search_store(collections=collections)
    return TestClient(app, headers=auth_headers)


@pytest.mark.integration
def test_status_includes_backup_object_when_enabled(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """interval_hours>0 → backup is non-null with correct enabled/interval fields."""
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, auth_headers, collections, interval_hours=24)
    with client:
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["backup"] is not None
    assert body["backup"]["enabled"] is True
    assert body["backup"]["interval_hours"] == 24
    assert body["backup"]["collections_excluded"] == []
    assert isinstance(body["backup"]["collection_status"], list)


@pytest.mark.integration
def test_status_backup_enabled_false_when_interval_zero(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, auth_headers, collections, interval_hours=0)
    with client:
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["backup"] is not None
    assert body["backup"]["enabled"] is False
    assert body["backup"]["interval_hours"] == 0


@pytest.mark.integration
def test_status_collection_status_includes_never_backed_up(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, auth_headers, collections, interval_hours=1)
    with client:
        response = client.get("/status")
    body = response.json()
    cs = body["backup"]["collection_status"]
    assert len(cs) == 1
    assert cs[0]["collection"] == "docs"
    assert cs[0]["last_backup_at"] is None
    assert cs[0]["archive_count"] == 0


@pytest.mark.integration
def test_status_collection_status_archive_count(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, auth_headers, collections, interval_hours=1)
    # Create fake archive files in {output_dir}/{ns}/
    ns_dir = tmp_path / "backups" / DEFAULT_NAMESPACE
    ns_dir.mkdir(parents=True)
    (ns_dir / "docs.backup.20260101T000000Z.tar.gz").write_bytes(b"")
    (ns_dir / "docs.backup.20260102T000000Z.tar.gz").write_bytes(b"")
    (ns_dir / "docs.backup.20260103T000000Z.tar.gz").write_bytes(b"")
    # Non-matching file should not be counted
    (ns_dir / "other.backup.20260101T000000Z.tar.gz").write_bytes(b"")
    with client:
        response = client.get("/status")
    body = response.json()
    cs = body["backup"]["collection_status"]
    docs_entry = next(c for c in cs if c["collection"] == "docs")
    assert docs_entry["archive_count"] == 3


@pytest.mark.integration
def test_status_collection_status_namespace_scoped(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    collections = [
        CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE),
        CollectionInfo(name="other", doc_count=1, chunk_count=1, namespace="tenant-a"),
    ]
    client = _make_client(tmp_path, auth_headers, collections, interval_hours=1)
    with client:
        response = client.get("/status")
    body = response.json()
    cs = body["backup"]["collection_status"]
    names = {c["collection"] for c in cs}
    assert "docs" in names
    assert "other" not in names


@pytest.mark.integration
def test_status_next_run_at_computed_from_last_tick(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, auth_headers, collections, interval_hours=2)
    with client:
        # Set _last_tick_at on the backup_loop
        last_tick = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()
        client.app.state.backup_loop._last_tick_at = last_tick
        response = client.get("/status")
    body = response.json()
    assert body["backup"]["last_tick_at"] == last_tick
    expected_next = (
        datetime.fromisoformat(last_tick) + timedelta(hours=2)
    ).isoformat()
    assert body["backup"]["next_run_at"] == expected_next


@pytest.mark.integration
def test_status_last_backup_at_reflects_state_file(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """A populated .backup-state.json entry surfaces as last_backup_at."""
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, auth_headers, collections, interval_hours=1)
    expected_ts = "2026-05-01T12:00:00+00:00"
    with client:
        # Seed state via the backup_loop's own writer to exercise the real path.
        client.app.state.backup_loop._save_state({f"{DEFAULT_NAMESPACE}/docs": expected_ts})
        response = client.get("/status")
    body = response.json()
    docs_entry = next(c for c in body["backup"]["collection_status"] if c["collection"] == "docs")
    assert docs_entry["last_backup_at"] == expected_ts


@pytest.mark.integration
def test_status_backup_null_without_backup_loop(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """If app.state.backup_loop is missing, backup field is None."""
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, auth_headers, collections, interval_hours=1)
    with client:
        # Remove backup_loop attribute
        if hasattr(client.app.state, "backup_loop"):
            delattr(client.app.state, "backup_loop")
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["backup"] is None
