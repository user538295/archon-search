"""Tests for Pruner.prune_once — Task 2.3 (TDD: tests first)."""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import unittest.mock

import pytest

from archon_search.telemetry.pruner import Pruner


def _make_file(log_dir: Path, d: date) -> Path:
    """Create an empty JSONL file named after the given date."""
    p = log_dir / f"{d.isoformat()}.jsonl"
    p.write_bytes(b"")
    return p


class TestPruneOnce:
    def test_pruner_deletes_files_older_than_retention(self, tmp_path: Path) -> None:
        today = date(2025, 6, 15)
        ages = [0, 15, 29, 30, 31, 60]
        files = {age: _make_file(tmp_path, today - timedelta(days=age)) for age in ages}
        pruner = Pruner(tmp_path, retention_days=30)
        count = pruner.prune_once(now=today)
        assert count == 2
        assert files[0].exists()
        assert files[15].exists()
        assert files[29].exists()
        assert files[30].exists()
        assert not files[31].exists()
        assert not files[60].exists()

    def test_pruner_keeps_files_within_retention(self, tmp_path: Path) -> None:
        today = date(2025, 6, 15)
        for age in [0, 15, 29]:
            _make_file(tmp_path, today - timedelta(days=age))
        pruner = Pruner(tmp_path, retention_days=30)
        count = pruner.prune_once(now=today)
        assert count == 0
        assert len(list(tmp_path.glob("*.jsonl"))) == 3

    def test_pruner_deletes_exactly_at_boundary(self, tmp_path: Path) -> None:
        today = date(2025, 6, 15)
        # A file exactly retention_days old is kept (only strictly older files are deleted).
        file_30 = _make_file(tmp_path, today - timedelta(days=30))
        file_31 = _make_file(tmp_path, today - timedelta(days=31))
        file_29 = _make_file(tmp_path, today - timedelta(days=29))
        pruner = Pruner(tmp_path, retention_days=30)
        count = pruner.prune_once(now=today)
        assert count == 1
        assert file_30.exists()
        assert not file_31.exists()
        assert file_29.exists()

    def test_pruner_never_deletes_today_or_future(self, tmp_path: Path) -> None:
        today = date(2025, 6, 15)
        today_file = _make_file(tmp_path, today)
        tomorrow_file = _make_file(tmp_path, today + timedelta(days=1))
        pruner = Pruner(tmp_path, retention_days=1)
        count = pruner.prune_once(now=today)
        assert count == 0
        assert today_file.exists()
        assert tomorrow_file.exists()

    def test_pruner_uses_filename_not_mtime(self, tmp_path: Path) -> None:
        today = date(2025, 6, 15)
        today_file = _make_file(tmp_path, today)
        # Set mtime to 60 days ago
        very_old_ts = (today - timedelta(days=60)).timetuple()
        import time
        ts = time.mktime(very_old_ts)
        os.utime(today_file, (ts, ts))
        pruner = Pruner(tmp_path, retention_days=30)
        count = pruner.prune_once(now=today)
        assert count == 0
        assert today_file.exists()

    def test_pruner_skips_non_jsonl_files(self, tmp_path: Path) -> None:
        today = date(2025, 6, 15)
        readme = tmp_path / "README.md"
        readme.write_text("hello")
        old_file = _make_file(tmp_path, today - timedelta(days=60))
        pruner = Pruner(tmp_path, retention_days=30)
        pruner.prune_once(now=today)
        assert readme.exists()
        assert not old_file.exists()

    def test_pruner_skips_malformed_filenames(self, tmp_path: Path) -> None:
        bad = tmp_path / "not-a-date.jsonl"
        bad.write_bytes(b"")
        pruner = Pruner(tmp_path, retention_days=30)
        count = pruner.prune_once(now=date(2025, 6, 15))
        assert count == 0
        assert bad.exists()

    def test_pruner_handles_missing_directory_gracefully(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent"
        pruner = Pruner(missing, retention_days=30)
        count = pruner.prune_once(now=date(2025, 6, 15))
        assert count == 0

    def test_pruner_returns_delete_count(self, tmp_path: Path) -> None:
        today = date(2025, 6, 15)
        for age in [40, 50, 60]:
            _make_file(tmp_path, today - timedelta(days=age))
        pruner = Pruner(tmp_path, retention_days=30)
        count = pruner.prune_once(now=today)
        assert count == 3

    def test_pruner_uses_default_now(self, tmp_path: Path) -> None:
        """With now=None, the pruner uses today (UTC) — old file should be pruned."""
        from datetime import UTC, datetime

        real_today = datetime.now(UTC).date()
        old_date = real_today - timedelta(days=60)
        old_file = _make_file(tmp_path, old_date)
        pruner = Pruner(tmp_path, retention_days=30)
        count = pruner.prune_once()  # now=None
        assert count == 1
        assert not old_file.exists()

    def test_pruner_oserror_on_unlink_is_caught_and_logged(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """OSError during unlink is caught, WARNING logged via archon.search, count not incremented."""
        import logging
        today = date(2025, 6, 15)
        old_file = _make_file(tmp_path, today - timedelta(days=60))
        pruner = Pruner(tmp_path, retention_days=30)
        with unittest.mock.patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            with caplog.at_level(logging.WARNING, logger="archon.search"):
                count = pruner.prune_once(now=today)
        assert count == 0
        assert any("pruner: failed to delete" in r.message for r in caplog.records if r.levelno == logging.WARNING)

    def test_pruner_retention_zero_keeps_today_deletes_older(self, tmp_path: Path) -> None:
        """retention_days=0 means keep only today; yesterday's file is deleted."""
        today = date(2025, 6, 15)
        today_file = _make_file(tmp_path, today)
        yesterday_file = _make_file(tmp_path, today - timedelta(days=1))
        pruner = Pruner(tmp_path, retention_days=0)
        count = pruner.prune_once(now=today)
        assert count == 1
        assert today_file.exists()
        assert not yesterday_file.exists()
