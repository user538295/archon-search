"""Tests for `list_queued_bulk()` priority sort (Task 1.3 of D2-scheduled-backup plan).

User-sourced bulk jobs sort before backup-sourced bulk jobs. FIFO (by
`created_at` ascending) is preserved within each tier.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from archon_search.jobs.store import JobStore
from archon_search.types import ExportJob


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def _set_created_at(store: JobStore, job_id: str, ts: str) -> None:
    """Forcibly override created_at on a stored job for deterministic ordering."""
    job = store._jobs[job_id]
    store._jobs[job_id] = dataclasses.replace(job, created_at=ts)


def _iso(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


# ---------------------------------------------------------------------------
# Priority sort: user before backup
# ---------------------------------------------------------------------------


def test_user_job_before_backup_job(store: JobStore) -> None:
    """A user job created AFTER a backup job still sorts first."""
    backup = store.create_export(
        collection="b", output_path="/tmp/b.tar.gz", tmp_path="/tmp/.b.tmp", source="backup"
    )
    user = store.create_export(
        collection="u", output_path="/tmp/u.tar.gz", tmp_path="/tmp/.u.tmp", source="user"
    )
    # backup created first (older), user created second (newer)
    _set_created_at(store, backup.job_id, _iso(20))
    _set_created_at(store, user.job_id, _iso(10))

    queued = store.list_queued_bulk()
    assert [j.job_id for j in queued] == [user.job_id, backup.job_id]


def test_fifo_within_user_tier(store: JobStore) -> None:
    """Two user jobs maintain FIFO ordering by created_at."""
    a = store.create_export(
        collection="a", output_path="/tmp/a.tar.gz", tmp_path="/tmp/.a.tmp", source="user"
    )
    b = store.create_export(
        collection="b", output_path="/tmp/b.tar.gz", tmp_path="/tmp/.b.tmp", source="user"
    )
    _set_created_at(store, a.job_id, _iso(20))
    _set_created_at(store, b.job_id, _iso(10))

    queued = store.list_queued_bulk()
    assert [j.job_id for j in queued] == [a.job_id, b.job_id]


def test_fifo_within_backup_tier(store: JobStore) -> None:
    """Two backup jobs maintain FIFO ordering by created_at."""
    a = store.create_export(
        collection="a", output_path="/tmp/a.tar.gz", tmp_path="/tmp/.a.tmp", source="backup"
    )
    b = store.create_export(
        collection="b", output_path="/tmp/b.tar.gz", tmp_path="/tmp/.b.tmp", source="backup"
    )
    _set_created_at(store, a.job_id, _iso(20))
    _set_created_at(store, b.job_id, _iso(10))

    queued = store.list_queued_bulk()
    assert [j.job_id for j in queued] == [a.job_id, b.job_id]


def test_mixed_queue_ordering(store: JobStore) -> None:
    """3 jobs: backup(T1 oldest), user(T2), user(T3) → order is user(T2), user(T3), backup(T1)."""
    backup_t1 = store.create_export(
        collection="bk", output_path="/tmp/bk.tar.gz", tmp_path="/tmp/.bk.tmp", source="backup"
    )
    user_t2 = store.create_export(
        collection="u2", output_path="/tmp/u2.tar.gz", tmp_path="/tmp/.u2.tmp", source="user"
    )
    user_t3 = store.create_export(
        collection="u3", output_path="/tmp/u3.tar.gz", tmp_path="/tmp/.u3.tmp", source="user"
    )
    _set_created_at(store, backup_t1.job_id, _iso(30))
    _set_created_at(store, user_t2.job_id, _iso(20))
    _set_created_at(store, user_t3.job_id, _iso(10))

    queued = store.list_queued_bulk()
    assert [j.job_id for j in queued] == [user_t2.job_id, user_t3.job_id, backup_t1.job_id]
