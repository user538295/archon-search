"""Tests for the advisory install lock in archon_search.install."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import InstallLockError, _acquire_install_lock, _install_lock_path

pytestmark = pytest.mark.xdist_group("install")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dead_pid() -> int:
    """Return a PID guaranteed to be dead by mocking os.kill to raise ProcessLookupError."""
    return 99999999


# ---------------------------------------------------------------------------
# 1. Lock file is created and contains current PID during context
# ---------------------------------------------------------------------------

def test_lock_creates_pid_file(tmp_path: Path) -> None:
    lock_path = tmp_path / ".install.lock"
    with patch("archon_search.install.lock._install_lock_path", return_value=lock_path):
        with _acquire_install_lock():
            assert lock_path.exists()
            contents = lock_path.read_text()
            pid_str, ts_str = contents.split(":")
            assert int(pid_str) == os.getpid()
            assert int(ts_str) > 0


# ---------------------------------------------------------------------------
# 2. Lock file is removed after normal exit
# ---------------------------------------------------------------------------

def test_lock_removes_file_on_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / ".install.lock"
    with patch("archon_search.install.lock._install_lock_path", return_value=lock_path):
        with _acquire_install_lock():
            pass
        assert not lock_path.exists()


# ---------------------------------------------------------------------------
# 3. Lock file is removed even when context raises
# ---------------------------------------------------------------------------

def test_lock_removes_file_on_exception(tmp_path: Path) -> None:
    lock_path = tmp_path / ".install.lock"
    with patch("archon_search.install.lock._install_lock_path", return_value=lock_path):
        with pytest.raises(RuntimeError):
            with _acquire_install_lock():
                raise RuntimeError("boom")
        assert not lock_path.exists()


# ---------------------------------------------------------------------------
# 4. Raises InstallLockError when current process holds the lock (live PID)
# ---------------------------------------------------------------------------

def test_lock_raises_if_live_pid_holds_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / ".install.lock"
    # Write current PID — definitely alive
    lock_path.write_text(f"{os.getpid()}:{int(time.time())}")
    with patch("archon_search.install.lock._install_lock_path", return_value=lock_path):
        with pytest.raises(InstallLockError):
            with _acquire_install_lock():
                pass


# ---------------------------------------------------------------------------
# 5. Stale dead-PID lock is removed and context proceeds
# ---------------------------------------------------------------------------

def test_lock_removes_stale_dead_pid_and_proceeds(tmp_path: Path) -> None:
    lock_path = tmp_path / ".install.lock"
    dead_pid = _dead_pid()
    lock_path.write_text(f"{dead_pid}:{int(time.time())}")

    def fake_kill(pid: int, sig: int) -> None:
        if pid == dead_pid:
            raise ProcessLookupError
        # real process
        return None

    with patch("archon_search.install.lock._install_lock_path", return_value=lock_path):
        with patch("archon_search.install.os.kill", side_effect=fake_kill):
            with _acquire_install_lock():
                assert lock_path.exists()
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# 6. PermissionError from os.kill → live process (different user) → InstallLockError
# ---------------------------------------------------------------------------

def test_lock_treats_permission_error_from_kill_as_live_process(tmp_path: Path) -> None:
    lock_path = tmp_path / ".install.lock"
    some_pid = 12345
    lock_path.write_text(f"{some_pid}:{int(time.time())}")

    def fake_kill(pid: int, sig: int) -> None:
        raise PermissionError

    with patch("archon_search.install.lock._install_lock_path", return_value=lock_path):
        with patch("archon_search.install.os.kill", side_effect=fake_kill):
            with pytest.raises(InstallLockError):
                with _acquire_install_lock():
                    pass


# ---------------------------------------------------------------------------
# 7. O_EXCL | O_CREAT flags are used for atomic creation
# ---------------------------------------------------------------------------

def test_lock_uses_o_excl_for_atomic_creation(tmp_path: Path) -> None:
    lock_path = tmp_path / ".install.lock"
    real_os_open = os.open
    captured_flags: list[int] = []

    def fake_os_open(path: str, flags: int, mode: int = 0o777) -> int:
        captured_flags.append(flags)
        return real_os_open(path, flags, mode)

    with patch("archon_search.install.lock._install_lock_path", return_value=lock_path):
        with patch("archon_search.install.os.open", side_effect=fake_os_open):
            with _acquire_install_lock():
                pass

    assert captured_flags, "os.open was not called"
    assert captured_flags[0] & os.O_EXCL, "O_EXCL flag not set"
    assert captured_flags[0] & os.O_CREAT, "O_CREAT flag not set"


# ---------------------------------------------------------------------------
# 8. On Windows: psutil.pid_exists used; os.kill NOT called
# ---------------------------------------------------------------------------

def test_lock_uses_platform_safe_pid_check(tmp_path: Path) -> None:
    lock_path = tmp_path / ".install.lock"
    some_pid = 12345
    lock_path.write_text(f"{some_pid}:{int(time.time())}")

    mock_psutil = MagicMock()
    mock_psutil.pid_exists.return_value = True  # treat as alive → InstallLockError

    with patch("archon_search.install.lock._install_lock_path", return_value=lock_path):
        with patch("archon_search.install.sys.platform", "win32"):
            with patch.dict("sys.modules", {"psutil": mock_psutil}):
                with pytest.raises(InstallLockError):
                    with _acquire_install_lock():
                        pass

    mock_psutil.pid_exists.assert_called_once_with(some_pid)


# ---------------------------------------------------------------------------
# 9. Corrupted/unparseable PID file → treated as stale → context proceeds
# ---------------------------------------------------------------------------

def test_lock_handles_corrupted_pid_file(tmp_path: Path) -> None:
    lock_path = tmp_path / ".install.lock"
    lock_path.write_text("not-a-pid")

    with patch("archon_search.install.lock._install_lock_path", return_value=lock_path):
        # Should treat as stale and succeed
        with _acquire_install_lock():
            assert lock_path.exists()
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# 10. Concurrent acquisition: second caller raises InstallLockError
# ---------------------------------------------------------------------------

def test_lock_concurrent_acquisition_blocks_second_caller(tmp_path: Path) -> None:
    lock_path = tmp_path / ".install.lock"
    lock_held = threading.Event()
    lock_released = threading.Event()
    second_result: list[Exception | None] = [None]

    def first_thread() -> None:
        with patch("archon_search.install.lock._install_lock_path", return_value=lock_path):
            with _acquire_install_lock():
                lock_held.set()
                lock_released.wait(timeout=5)

    def second_thread() -> None:
        lock_held.wait(timeout=5)
        with patch("archon_search.install.lock._install_lock_path", return_value=lock_path):
            try:
                with _acquire_install_lock():
                    pass
            except InstallLockError as exc:
                second_result[0] = exc

    t1 = threading.Thread(target=first_thread)
    t2 = threading.Thread(target=second_thread)
    t1.start()
    t2.start()
    t2.join(timeout=5)
    lock_released.set()
    t1.join(timeout=5)

    assert isinstance(second_result[0], InstallLockError), (
        f"Expected InstallLockError from second thread, got {second_result[0]!r}"
    )


def test_lock_retry_raises_when_second_o_excl_fails(tmp_path: Path) -> None:
    """Concurrent process wins the race after stale-lock removal: retry O_EXCL fails → InstallLockError."""
    from archon_search.install import InstallLockError, _acquire_install_lock

    lock_path = tmp_path / ".install.lock"
    lock_path.write_text("99999999:0")  # stale dead PID

    call_count = [0]
    real_os_open = os.open

    def patched_os_open(path, flags, mode=0o644):
        if str(lock_path) in str(path) and (flags & os.O_EXCL):
            call_count[0] += 1
            if call_count[0] == 1:
                raise FileExistsError("stale lock exists")
            if call_count[0] == 2:
                raise FileExistsError("concurrent process grabbed it")
        return real_os_open(path, flags, mode)

    with (
        patch("archon_search.install.lock._install_lock_path", return_value=lock_path),
        patch("archon_search.install.os.kill", side_effect=ProcessLookupError),
        patch("archon_search.install.os.open", side_effect=patched_os_open),
    ):
        import pytest
        with pytest.raises(InstallLockError):
            with _acquire_install_lock():
                pass
