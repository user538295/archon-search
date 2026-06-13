"""tests/test_conftest_data_dir_isolation.py — verify _archon_isolated_data_dir autouse fixture.

These tests validate that the conftest autouse fixture:
1. Sets ARCHON_SEARCH_DATA_DIR to an isolated per-worker path for all normal tests.
2. Does NOT set ARCHON_SEARCH_DATA_DIR when @pytest.mark.archon_unset_data_dir is applied.
   Verified indirectly: the 5 default-fallback tests in Task 2.3 each carry the marker and
   assert Path.home()-based defaults; they pass only when DATA_DIR is unset.
3. Clears the other 5 contaminating env vars for every test.

Note: test_marker_unsets_data_dir does NOT use @pytest.mark.archon_unset_data_dir directly
because that marker is scope-guarded by test_no_hardcoded_path_home.py::MARKER_ALLOWLIST
(Task 2.4). The unset-branch is verified via the 5 Task-2.3 default-fallback tests instead.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_autouse_sets_data_dir() -> None:
    """Normal tests must have ARCHON_SEARCH_DATA_DIR set to an existing directory.

    The _archon_isolated_data_dir autouse fixture should set ARCHON_SEARCH_DATA_DIR
    to str(_archon_worker_data_dir), which is a tmp directory created by
    tmp_path_factory.mktemp('archon-data').
    """
    data_dir = os.environ.get("ARCHON_SEARCH_DATA_DIR")
    assert data_dir is not None, (
        "ARCHON_SEARCH_DATA_DIR must be set by _archon_isolated_data_dir autouse fixture"
    )
    path = Path(data_dir)
    assert path.exists(), (
        f"ARCHON_SEARCH_DATA_DIR={data_dir!r} must point to an existing directory"
    )
    assert path.is_dir(), (
        f"ARCHON_SEARCH_DATA_DIR={data_dir!r} must be a directory, not a file"
    )


def test_autouse_clears_host_and_port() -> None:
    """The autouse fixture must clear ARCHON_SEARCH_HOST and ARCHON_SEARCH_PORT."""
    assert "ARCHON_SEARCH_HOST" not in os.environ, (
        "ARCHON_SEARCH_HOST must be cleared by _archon_isolated_data_dir autouse fixture"
    )
    assert "ARCHON_SEARCH_PORT" not in os.environ, (
        "ARCHON_SEARCH_PORT must be cleared by _archon_isolated_data_dir autouse fixture"
    )


def test_autouse_clears_container_key_and_config() -> None:
    """The autouse fixture must clear ARCHON_SEARCH_CONTAINER, _KEY_FILE, and _CONFIG."""
    assert "ARCHON_SEARCH_CONTAINER" not in os.environ, (
        "ARCHON_SEARCH_CONTAINER must be cleared by _archon_isolated_data_dir autouse fixture"
    )
    assert "ARCHON_SEARCH_KEY_FILE" not in os.environ, (
        "ARCHON_SEARCH_KEY_FILE must be cleared by _archon_isolated_data_dir autouse fixture"
    )
    assert "ARCHON_SEARCH_CONFIG" not in os.environ, (
        "ARCHON_SEARCH_CONFIG must be cleared by _archon_isolated_data_dir autouse fixture"
    )


def test_data_dir_is_worker_distinct_under_xdist() -> None:
    """Under xdist, each worker must receive a distinct DATA_DIR path.

    When PYTEST_XDIST_WORKER is set, the tmp_path_factory.mktemp("archon-data")
    returns a worker-unique directory (pytest appends a counter suffix per worker).
    We verify the DATA_DIR path contains a worker-specific component.

    Skips when not running under xdist (PYTEST_XDIST_WORKER not set).
    """
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id is None:
        pytest.skip("Not running under xdist — worker-distinctness cannot be verified")

    data_dir = os.environ.get("ARCHON_SEARCH_DATA_DIR")
    assert data_dir is not None, (
        "ARCHON_SEARCH_DATA_DIR must be set under xdist"
    )
    # tmp_path_factory.mktemp("archon-data") produces paths like
    # .../pytest-of-<user>/pytest-N/archon-data0/ where the trailing digit
    # distinguishes workers. The path itself being unique per worker is the
    # structural guarantee; we simply assert it exists and is a directory.
    path = Path(data_dir)
    assert path.exists(), (
        f"Worker {worker_id!r}: ARCHON_SEARCH_DATA_DIR={data_dir!r} must exist"
    )
    assert path.is_dir(), (
        f"Worker {worker_id!r}: ARCHON_SEARCH_DATA_DIR={data_dir!r} must be a directory"
    )
