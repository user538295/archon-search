"""Shared app/client builders for tests that read the maintenance block of ``GET /status``.

Used by ``tests/test_routes_maintenance.py`` and ``tests/test_no_internal_error_leak_jobs.py``;
kept here so neither test module imports the other.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from archon_search._types import CollectionInfo
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


def make_mock_search_store(collections: list[CollectionInfo] | None = None) -> MagicMock:
    """A SearchStore stand-in that answers every call ``create_app`` and ``GET /status`` make."""
    mock = MagicMock()
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock._run_startup_migrations = AsyncMock()
    mock.ping = AsyncMock(return_value=True)
    mock.get_all_collections_meta = AsyncMock(
        return_value=[
            CollectionMeta(name=c.name, namespace=c.namespace)
            for c in (collections or [])
        ]
    )
    mock.get_collection_meta = AsyncMock(return_value=None)
    mock.list_collections = AsyncMock(return_value=collections or [])
    mock.count_untagged_language_chunks = AsyncMock(return_value=0)
    mock.pending_migrations = AsyncMock(return_value=[])
    mock.count_expired_chunks = AsyncMock(return_value=0)
    return mock


def make_status_client(
    tmp_path: Path,
    auth_headers: dict[str, str],
    *,
    interval_hours: int = 0,
    collections: list[CollectionInfo] | None = None,
) -> TestClient:
    """An authenticated client over an app whose data lives under ``tmp_path``."""
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.backup.output_dir = str(tmp_path / "backups")
    cfg.maintenance.interval_hours = interval_hours
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(cfg, job_store)
    app.state.search_store = make_mock_search_store(collections=collections)
    return TestClient(app, headers=auth_headers)
