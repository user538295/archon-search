"""Unit tests for the lazy jobs file path (C9 Task 2.4).

Replaces the old module-level ``JOBS_FILE`` constant with ``get_jobs_file()``
so ``ARCHON_SEARCH_DATA_DIR`` redirects the jobs JSON file at call time, not
at import time. ``JobStore.__init__`` now defaults its ``path`` argument to
``None`` and resolves the path on construction.

The autouse fixture in ``tests/conftest.py`` clears
``ARCHON_SEARCH_DATA_DIR`` between tests, so each test can assume a clean
environment.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.jobs.model import get_jobs_file
from archon_search.jobs.store import JobStore


def test_get_jobs_file_default() -> None:
    """No env vars set → fall back to ``~/.archon-search/archon-search-jobs.json``."""
    assert get_jobs_file() == Path.home() / ".archon-search" / "archon-search-jobs.json"


def test_get_jobs_file_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ARCHON_SEARCH_DATA_DIR="/data"`` → ``/data/archon-search-jobs.json``."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data")
    assert get_jobs_file() == Path("/data/archon-search-jobs.json")


def test_job_store_default_path_is_lazy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``JobStore()`` with no argument picks up ``ARCHON_SEARCH_DATA_DIR`` set
    AFTER import — the default must be resolved lazily inside ``__init__``,
    not captured at module load time."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    store = JobStore()
    assert store._path == data_dir / "archon-search-jobs.json"


def test_job_store_explicit_path_overrides(tmp_path: Path) -> None:
    """An explicit ``path=`` argument wins over the env var default."""
    explicit = tmp_path / "custom" / "jobs.json"
    store = JobStore(path=explicit)
    assert store._path == explicit
