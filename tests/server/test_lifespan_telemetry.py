"""Tests for telemetry lifespan registration in create_app() — FEAT-039b Task 3.1."""
from __future__ import annotations

import asyncio
import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig, TelemetryConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.telemetry.entry import TelemetryEntry


def _make_config(tmp_path: Path, *, enabled: bool, log_dir: str | None = None) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.telemetry = TelemetryConfig(
        enabled=enabled,
        retention_days=30,
        log_dir=log_dir or str(tmp_path / "telemetry"),
    )
    return cfg


def _make_job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


# ---------------------------------------------------------------------------
# Test 1: disabled — no dir created, writer is None
# ---------------------------------------------------------------------------


def test_lifespan_does_not_create_dir_when_disabled(tmp_path: Path) -> None:
    """When telemetry is disabled, no log_dir is created and writer is None."""
    absent_dir = tmp_path / "should-not-exist"
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.telemetry = TelemetryConfig(
        enabled=False,
        log_dir=str(absent_dir),
    )
    job_store = _make_job_store(tmp_path)
    app = create_app(cfg, job_store)

    with TestClient(app):
        assert not absent_dir.exists()
        assert app.state.telemetry_writer is None


# ---------------------------------------------------------------------------
# Test 2: enabled — writer is not None, two background tasks added
# ---------------------------------------------------------------------------


def test_lifespan_starts_writer_and_pruner_when_enabled(tmp_path: Path) -> None:
    """When telemetry is enabled, writer is set and two background tasks are registered."""
    cfg = _make_config(tmp_path, enabled=True)
    job_store = _make_job_store(tmp_path)
    app = create_app(cfg, job_store)

    with TestClient(app):
        assert app.state.telemetry_writer is not None
        # writer task + pruner task
        assert len(app.state._background_tasks) >= 2


# ---------------------------------------------------------------------------
# Test 3: old files are pruned on startup
# ---------------------------------------------------------------------------


def test_lifespan_runs_initial_prune_before_writer_starts(tmp_path: Path) -> None:
    """Old JSONL files (older than retention_days) are deleted during lifespan startup."""
    log_dir = tmp_path / "telemetry"
    log_dir.mkdir()

    # Create a file that is 31 days old (> retention_days=30)
    old_date = datetime.date.today() - datetime.timedelta(days=31)
    old_file = log_dir / f"{old_date.isoformat()}.jsonl"
    old_file.write_text('{"query_id":"old"}\n', encoding="utf-8")

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.telemetry = TelemetryConfig(
        enabled=True,
        retention_days=30,
        log_dir=str(log_dir),
    )
    job_store = _make_job_store(tmp_path)
    app = create_app(cfg, job_store)

    with TestClient(app):
        assert not old_file.exists(), "Old file should have been pruned on startup"


# ---------------------------------------------------------------------------
# Test 4: initial prune runs in thread (asyncio.to_thread called)
# ---------------------------------------------------------------------------


def test_lifespan_prune_runs_in_thread_not_event_loop(tmp_path: Path) -> None:
    """asyncio.to_thread is called with pruner.prune_once during lifespan startup."""
    cfg = _make_config(tmp_path, enabled=True)
    job_store = _make_job_store(tmp_path)
    app = create_app(cfg, job_store)

    to_thread_calls: list[object] = []
    original_to_thread = asyncio.to_thread

    async def capturing_to_thread(func: object, *args: object, **kwargs: object) -> object:
        to_thread_calls.append(func)
        return await original_to_thread(func, *args, **kwargs)  # type: ignore[arg-type]

    with patch("archon_search.server.app.asyncio.to_thread", side_effect=capturing_to_thread):
        with TestClient(app):
            pass

    assert len(to_thread_calls) == 1, "asyncio.to_thread should be called exactly once"
    # The callable passed must be a bound method named prune_once
    assert getattr(to_thread_calls[0], "__name__", None) == "prune_once"


# ---------------------------------------------------------------------------
# Test 5: writer is drained on shutdown — all enqueued entries are flushed
# ---------------------------------------------------------------------------


def test_lifespan_drains_writer_on_shutdown(tmp_path: Path) -> None:
    """Entries enqueued during the request phase are flushed to disk on shutdown."""
    log_dir = tmp_path / "telemetry"
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.telemetry = TelemetryConfig(
        enabled=True,
        retention_days=30,
        log_dir=str(log_dir),
    )
    job_store = _make_job_store(tmp_path)
    app = create_app(cfg, job_store)

    entries = [
        TelemetryEntry.from_search_tool_result(
            endpoint="search",
            collection="col1",
            result_doc_ids=[f"doc{i}"],
            latency_ms=float(i),
        )
        for i in range(3)
    ]

    with TestClient(app):
        writer = app.state.telemetry_writer
        assert writer is not None
        for entry in entries:
            writer.enqueue(entry)

    # After context exit, lifespan shutdown has completed — all entries flushed.
    jsonl_files = list(log_dir.glob("*.jsonl"))
    assert jsonl_files, "At least one JSONL file must exist after drain"

    lines = []
    for f in jsonl_files:
        lines.extend(f.read_text(encoding="utf-8").splitlines())

    assert len(lines) == 3, f"Expected 3 flushed entries, got {len(lines)}: {lines}"
