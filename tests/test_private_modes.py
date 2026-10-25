"""Files and directories the app creates are private to its user (SecReview #9).

Every test runs under ``umask 022`` — the umask that made the data dir 0755 and its files 0644 — on real tmp dirs.
"""
from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon_search._durable_io import (
    PRIVATE_DIR_MODE,
    PRIVATE_FILE_MODE,
    atomic_write_json,
    ensure_private_dir,
)
from archon_search.config import SearchConfig
from archon_search.graph_store import GraphStore
from archon_search.jobs.export_archive import ExportArchiveWriter
from archon_search.jobs.store import JobStore
from archon_search.logging_setup import _attach_file_handler
from archon_search.store import SearchStore
from archon_search.telemetry.entry import TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter

_LOOSE_UMASK = 0o022
_WORLD_READABLE_DIR = 0o755


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
    previous = os.umask(_LOOSE_UMASK)
    yield
    os.umask(previous)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(root))
    return root


# --- ensure_private_dir ---------------------------------------------------


def test_ensure_private_dir_creates_every_missing_component_private(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"

    ensure_private_dir(target)

    for directory in (tmp_path / "a", tmp_path / "a" / "b", target):
        assert _mode(directory) == PRIVATE_DIR_MODE


def test_ensure_private_dir_leaves_an_existing_dir_outside_the_data_dir_alone(tmp_path: Path, data_dir: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=_WORLD_READABLE_DIR)
    shared.chmod(_WORLD_READABLE_DIR)

    ensure_private_dir(shared / "new")

    assert _mode(shared) == _WORLD_READABLE_DIR
    assert _mode(shared / "new") == PRIVATE_DIR_MODE


def test_ensure_private_dir_tightens_existing_dirs_inside_the_data_dir(data_dir: Path) -> None:
    nested = data_dir / "exports" / "default"
    nested.mkdir(parents=True)
    for directory in (data_dir, data_dir / "exports", nested):
        directory.chmod(_WORLD_READABLE_DIR)

    ensure_private_dir(nested)

    for directory in (data_dir, data_dir / "exports", nested):
        assert _mode(directory) == PRIVATE_DIR_MODE


def test_ensure_private_dir_is_idempotent(tmp_path: Path) -> None:
    ensure_private_dir(tmp_path / "x")
    ensure_private_dir(tmp_path / "x")

    assert _mode(tmp_path / "x") == PRIVATE_DIR_MODE


# --- atomic_write_json ----------------------------------------------------


def test_atomic_write_json_creates_a_private_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    atomic_write_json(target, {"k": 1})

    assert _mode(target) == PRIVATE_FILE_MODE


def test_atomic_write_json_tightens_a_world_readable_predecessor(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    target.chmod(0o644)

    atomic_write_json(target, {"k": 1})

    assert _mode(target) == PRIVATE_FILE_MODE


# --- jobs.json ------------------------------------------------------------


def test_job_store_persists_jobs_json_private_in_a_private_dir(data_dir: Path) -> None:
    path = data_dir / "jobs" / "jobs.json"
    store = JobStore(path=path)

    store.create(namespace="default", collection="docs", path="/x")

    assert _mode(path) == PRIVATE_FILE_MODE
    assert _mode(path.parent) == PRIVATE_DIR_MODE
    assert _mode(data_dir) == PRIVATE_DIR_MODE


# --- telemetry ------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_files_and_dir_are_private(data_dir: Path) -> None:
    log_dir = data_dir / "telemetry"
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(log_dir, clock=lambda: fixed)
    await writer.start()
    writer.enqueue(
        TelemetryEntry(
            query_id="q",
            timestamp="2026-05-14T12:00:00Z",
            endpoint="search",
            latency_ms=1.0,
            status="ok",
            collection="c",
            result_count=0,
            result_doc_ids=[],
        )
    )
    await writer.drain_and_stop()

    assert _mode(log_dir / "2026-05-14.jsonl") == PRIVATE_FILE_MODE
    assert _mode(log_dir) == PRIVATE_DIR_MODE


# --- export / backup archives --------------------------------------------


def test_export_archive_and_its_temp_file_are_private(data_dir: Path) -> None:
    archive = data_dir / "exports" / "default" / "docs.tar.gz"
    tmp = archive.with_name("docs.tmp")
    writer = ExportArchiveWriter(tmp)
    with writer:
        assert _mode(tmp) == PRIVATE_FILE_MODE
        writer.write_doc({"id": "1"})
        writer.finalize({"collection": "docs"}, archive)

    assert _mode(archive) == PRIVATE_FILE_MODE
    assert _mode(archive.parent) == PRIVATE_DIR_MODE
    assert _mode(data_dir / "exports") == PRIVATE_DIR_MODE


# --- logs -----------------------------------------------------------------


def test_log_file_and_dir_are_private(data_dir: Path) -> None:
    config = SearchConfig()
    config.log_file = str(data_dir / "logs" / "archon-search.log")
    logger = logging.getLogger("test_private_modes")
    try:
        _attach_file_handler(logger, config)
        logger.error("line")
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    assert _mode(data_dir / "logs" / "archon-search.log") == PRIVATE_FILE_MODE
    assert _mode(data_dir / "logs") == PRIVATE_DIR_MODE


# --- LanceDB directories --------------------------------------------------


@pytest.mark.asyncio
async def test_search_store_db_dir_is_private(data_dir: Path) -> None:
    db_path = data_dir / "search"
    store = SearchStore(db_path)

    await store.connect()
    await store.disconnect()

    assert _mode(db_path) == PRIVATE_DIR_MODE
    assert _mode(data_dir) == PRIVATE_DIR_MODE


@pytest.mark.asyncio
async def test_graph_store_db_dir_is_private(data_dir: Path) -> None:
    db_path = data_dir / "graph"
    store = GraphStore(db_path=db_path)

    await store.connect()
    await store.disconnect()

    assert _mode(db_path) == PRIVATE_DIR_MODE
