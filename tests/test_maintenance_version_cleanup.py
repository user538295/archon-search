"""The maintenance pass deletes superseded LanceDB table versions older than ``version_retention_hours``."""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.config import MaintenanceConfig
from archon_search.jobs.maintenance_loop import MaintenanceLoop

_RETENTION_HOURS = 5


def _loop(tmp_path: Path, search_store: MagicMock, graph_store: MagicMock | None = None) -> MaintenanceLoop:
    return MaintenanceLoop(
        job_store=MagicMock(),
        search_store=search_store,
        config=MaintenanceConfig(
            interval_hours=0,
            failed_ingest_retry=False,
            version_retention_hours=_RETENTION_HOURS,
        ),
        data_dir=tmp_path,
        dispatch_retry=lambda _job: None,
        graph_store=graph_store,
    )


def _search_store() -> MagicMock:
    store = MagicMock()
    store.list_collections = AsyncMock(return_value=[])
    store.get_all_collections_meta = AsyncMock(return_value=[])
    store.prune_versions = AsyncMock()
    return store


async def test_a_pass_prunes_the_search_store_with_the_configured_retention(tmp_path: Path) -> None:
    store = _search_store()

    await _loop(tmp_path, store)._run_one_pass()

    store.prune_versions.assert_awaited_once_with(timedelta(hours=_RETENTION_HOURS))


async def test_a_pass_prunes_the_graph_store_too(tmp_path: Path) -> None:
    store, graph = _search_store(), MagicMock()
    graph.prune_versions = AsyncMock()

    await _loop(tmp_path, store, graph)._run_one_pass()

    graph.prune_versions.assert_awaited_once_with(timedelta(hours=_RETENTION_HOURS))


async def test_a_failing_prune_is_logged_and_does_not_abort_the_pass(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store, graph = _search_store(), MagicMock()
    store.prune_versions = AsyncMock(side_effect=OSError("disk went away"))
    graph.prune_versions = AsyncMock()
    loop = _loop(tmp_path, store, graph)

    with caplog.at_level(logging.ERROR, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_one_pass()

    assert any("version_cleanup" in record.getMessage() for record in caplog.records)
    graph.prune_versions.assert_awaited_once()
    assert loop._state_file.exists()


def test_the_server_builds_its_search_store_from_the_loaded_config(tmp_path: Path) -> None:
    """The store reads ``[maintenance].version_retention_hours`` (and ``centroid_recompute_threshold``) from the
    config it is built with; ``create_app`` used to build it with defaults, ignoring the operator's file."""
    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.maintenance.version_retention_hours = _RETENTION_HOURS

    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))

    assert app.state.search_store._config is config
