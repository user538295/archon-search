"""Behavioral tests for per-worker install-lock isolation under parallel test runs.

These tests confirm that two pytest workers with DISTINCT DATA_DIRs can both
acquire the install lock simultaneously (no collision), and that two workers
sharing the same DATA_DIR contend correctly (one wins, one gets InstallLockError).

Phase-2 xfail note: install.py still uses Path.home() for the lock path; the
per-child DATA_DIR env override has no effect until Phase 3 migrates those three
callsites to get_data_dir(). The method-level xfail on
test_two_workers_with_distinct_data_dirs_both_acquire is removed in Task 3.1.
"""
from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

import pytest

_CHILD_TIMEOUT: float = 30.0
_HOLD_SECONDS: float = 3.0  # generous hold; spawn + archon_search.install import can take ~1-2 s on CI


# ---------------------------------------------------------------------------
# Child-process worker
# ---------------------------------------------------------------------------

def _child_acquire(
    data_dir: str,
    hold_event: multiprocessing.Event | None,
    wait_event: multiprocessing.Event | None,
    hold_seconds: float,
    result_queue: multiprocessing.Queue,  # type: ignore[type-arg]
) -> None:
    """Run in a child process.

    Sets ARCHON_SEARCH_DATA_DIR to *data_dir* BEFORE importing
    archon_search.install so that _install_lock_path() resolves to the correct
    per-worker directory (once Phase 3 migrates it to get_data_dir()).

    Pushes ("ok", None) on success or ("err", <ExceptionClassName>) on failure.
    """
    os.environ["ARCHON_SEARCH_DATA_DIR"] = data_dir

    # Import AFTER the env var is set so get_data_dir() picks it up.
    from archon_search.install import InstallLockError, _acquire_install_lock  # noqa: PLC0415

    if wait_event is not None:
        wait_event.wait(timeout=15)

    try:
        with _acquire_install_lock():
            if hold_event is not None:
                hold_event.set()
                time.sleep(hold_seconds)
        result_queue.put(("ok", None))
    except InstallLockError:
        result_queue.put(("err", "InstallLockError"))
    except Exception as exc:  # noqa: BLE001
        result_queue.put(("err", type(exc).__name__))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.xdist_group("install")
def test_two_workers_with_distinct_data_dirs_both_acquire(tmp_path: Path) -> None:
    """Two processes with distinct DATA_DIRs must both acquire the lock without contention.

    Before Phase 3, _install_lock_path() still returns Path.home() / ... for all
    children, so both land on the same global lock and this test xfails.
    After Phase 3 migration the lock path is DATA_DIR-relative and they diverge.
    """
    dir_a = tmp_path / "worker_a"
    dir_b = tmp_path / "worker_b"
    dir_a.mkdir()
    dir_b.mkdir()

    ctx = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[tuple[str, str | None]] = ctx.Queue()

    proc_a = ctx.Process(
        target=_child_acquire,
        args=(str(dir_a), None, None, 0.0, queue),
    )
    proc_b = ctx.Process(
        target=_child_acquire,
        args=(str(dir_b), None, None, 0.0, queue),
    )

    proc_a.start()
    proc_b.start()
    proc_a.join(timeout=_CHILD_TIMEOUT)
    proc_b.join(timeout=_CHILD_TIMEOUT)

    assert not proc_a.is_alive(), "proc_a did not finish within timeout"
    assert not proc_b.is_alive(), "proc_b did not finish within timeout"

    results = [queue.get(timeout=5) for _ in range(2)]
    outcomes = {r[0] for r in results}
    assert outcomes == {"ok"}, (
        f"Expected both workers to acquire the lock; got: {results}"
    )


@pytest.mark.xdist_group("install")
def test_two_workers_sharing_data_dir_contend(tmp_path: Path) -> None:
    """Two processes sharing a DATA_DIR must contend: exactly one succeeds, one gets InstallLockError.

    This verifies the regression direction: sharing a DATA_DIR is intentionally
    unsafe (same lock file) and must produce an InstallLockError for the loser.
    """
    shared = tmp_path / "shared"
    shared.mkdir()

    ctx = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[tuple[str, str | None]] = ctx.Queue()
    hold_event = ctx.Event()

    # Process A: acquires immediately, holds for _HOLD_SECONDS, then sets hold_event
    # to release Process B's wait.  Process B waits until hold_event is set before
    # attempting acquisition; at that point A still holds the lock, so B sees a
    # live-PID lock and raises InstallLockError.
    #
    # Sequence:
    #   A starts → acquires lock → sets hold_event (signals B to try)
    #   B starts → waits on wait_event=hold_event → tries to acquire → InstallLockError
    #   A sleeps _HOLD_SECONDS → releases lock → exits
    proc_a = ctx.Process(
        target=_child_acquire,
        args=(str(shared), hold_event, None, _HOLD_SECONDS, queue),
    )
    proc_b = ctx.Process(
        target=_child_acquire,
        args=(str(shared), None, hold_event, 0.0, queue),
    )

    proc_a.start()
    proc_b.start()
    proc_a.join(timeout=_CHILD_TIMEOUT)
    proc_b.join(timeout=_CHILD_TIMEOUT)

    assert not proc_a.is_alive(), "proc_a did not finish within timeout"
    assert not proc_b.is_alive(), "proc_b did not finish within timeout"

    results = [queue.get(timeout=5) for _ in range(2)]
    ok_count = sum(1 for r in results if r[0] == "ok")
    err_count = sum(1 for r in results if r == ("err", "InstallLockError"))
    assert ok_count == 1 and err_count == 1, (
        f"Expected exactly one success and one InstallLockError; got: {results}"
    )
