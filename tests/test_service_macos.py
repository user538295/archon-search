"""Tests for Task 3.2 — LaunchdSearchService (macOS)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _fail(stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


# ── ABC conformance ────────────────────────────────────────────────────────────

def test_is_instance_of_lifecycle() -> None:
    from archon_search.platform.macos import LaunchdSearchService
    from archon_search.platform.service import SearchServiceLifecycle
    assert issubclass(LaunchdSearchService, SearchServiceLifecycle)


# ── start ──────────────────────────────────────────────────────────────────────

def test_start_calls_launchctl_load_and_start(tmp_path: Path) -> None:
    """start() calls launchctl load <plist> then launchctl start <label>."""
    from archon_search.platform.macos import LaunchdSearchService, _LABEL
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    plist.write_text("<plist/>")

    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=False),
        patch.object(svc, "_run", side_effect=mock_run),
    ):
        svc.start()

    assert calls[0] == ["launchctl", "load", str(plist)]
    assert calls[1] == ["launchctl", "start", _LABEL]


def test_start_skips_if_already_loaded(tmp_path: Path) -> None:
    """start() is idempotent — does nothing when already loaded."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    plist.write_text("<plist/>")

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=True),
        patch.object(svc, "_run") as mock_run,
    ):
        svc.start()
        mock_run.assert_not_called()


def test_start_raises_if_plist_missing(tmp_path: Path) -> None:
    """start() raises RuntimeError when plist is not registered."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "no.plist"  # does not exist

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=False),
    ):
        with pytest.raises(RuntimeError, match="Plist not installed"):
            svc.start()


def test_start_raises_on_load_failure(tmp_path: Path) -> None:
    """start() raises RuntimeError when launchctl load returns non-zero."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    plist.write_text("<plist/>")

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=False),
        patch.object(svc, "_run", return_value=_fail("load error")),
    ):
        with pytest.raises(RuntimeError, match="launchctl load failed"):
            svc.start()


def test_start_raises_on_start_failure(tmp_path: Path) -> None:
    """start() raises RuntimeError when launchctl start returns non-zero after successful load."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    plist.write_text("<plist/>")

    call_count = 0

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        # First call (load) succeeds, second call (start) fails
        return _ok() if call_count == 1 else _fail("start error")

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=False),
        patch.object(svc, "_run", side_effect=mock_run),
    ):
        with pytest.raises(RuntimeError, match="launchctl start failed"):
            svc.start()


def test_start_raises_when_launchctl_not_found(tmp_path: Path) -> None:
    """start() raises RuntimeError when launchctl binary is absent."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    plist.write_text("<plist/>")

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=False),
        patch.object(svc, "_run", side_effect=FileNotFoundError),
    ):
        with pytest.raises(RuntimeError, match="launchctl binary not found"):
            svc.start()


# ── stop ───────────────────────────────────────────────────────────────────────

def test_stop_calls_launchctl_unload(tmp_path: Path) -> None:
    """stop() calls launchctl unload <plist> when service is loaded."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"

    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=True),
        patch.object(svc, "_run", side_effect=mock_run),
    ):
        svc.stop()

    assert calls == [["launchctl", "unload", str(plist)]]


def test_stop_noop_when_not_loaded() -> None:
    """stop() does nothing when service is not loaded."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()

    with (
        patch.object(svc, "_is_loaded", return_value=False),
        patch.object(svc, "_run") as mock_run,
    ):
        svc.stop()
        mock_run.assert_not_called()


def test_stop_raises_on_unload_failure() -> None:
    """stop() raises RuntimeError when launchctl unload returns non-zero."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()

    with (
        patch.object(svc, "_is_loaded", return_value=True),
        patch.object(svc, "_run", return_value=_fail("unload error")),
    ):
        with pytest.raises(RuntimeError, match="launchctl unload failed"):
            svc.stop()


def test_stop_raises_when_launchctl_not_found() -> None:
    """stop() raises RuntimeError when launchctl binary is absent."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()

    with (
        patch.object(svc, "_is_loaded", return_value=True),
        patch.object(svc, "_run", side_effect=FileNotFoundError),
    ):
        with pytest.raises(RuntimeError, match="launchctl binary not found"):
            svc.stop()


# ── status ─────────────────────────────────────────────────────────────────────

def test_status_returns_running_when_listed() -> None:
    """status() returns ServiceStatus(running=True, pid=N) when launchctl reports a PID."""
    from archon_search.platform.macos import LaunchdSearchService
    from archon_search.platform.service import ServiceStatus
    svc = LaunchdSearchService()
    mock_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='"PID" = 12345;', stderr=""
    )
    with patch.object(svc, "_run", return_value=mock_result):
        s = svc.status()
    assert isinstance(s, ServiceStatus)
    assert s.running is True
    assert s.pid == 12345


def test_status_returns_stopped_when_not_listed() -> None:
    """status() returns ServiceStatus(running=False) when launchctl returns non-zero."""
    from archon_search.platform.macos import LaunchdSearchService
    from archon_search.platform.service import ServiceStatus
    svc = LaunchdSearchService()
    mock_result = subprocess.CompletedProcess(args=[], returncode=3, stdout="", stderr="")
    with patch.object(svc, "_run", return_value=mock_result):
        s = svc.status()
    assert isinstance(s, ServiceStatus)
    assert s.running is False


def test_status_returns_stopped_when_launchctl_not_found() -> None:
    """status() returns ServiceStatus(running=False) when launchctl binary is absent."""
    from archon_search.platform.macos import LaunchdSearchService
    from archon_search.platform.service import ServiceStatus
    svc = LaunchdSearchService()
    with patch.object(svc, "_run", side_effect=FileNotFoundError):
        s = svc.status()
    assert s.running is False


def test_status_pid_zero_returns_stopped() -> None:
    """PID=0 in launchctl output means the service is not running."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    mock_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='"PID" = 0;', stderr=""
    )
    with patch.object(svc, "_run", return_value=mock_result):
        s = svc.status()
    assert s.running is False


def test_status_returns_stopped_when_no_pid_key() -> None:
    """status() returns running=False when launchctl output has no PID key."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    # returncode=0 but no "PID" in output (service listed but not running)
    mock_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='"LastExitStatus" = 0;', stderr=""
    )
    with patch.object(svc, "_run", return_value=mock_result):
        s = svc.status()
    assert s.running is False


# ── register / unregister ──────────────────────────────────────────────────────

def test_register_writes_plist_with_label(tmp_path: Path) -> None:
    """register() writes a plist containing the service label."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    with patch.object(type(svc), "_plist_path", property(lambda self: plist)):
        svc.register()
    assert plist.exists()
    assert "com.archon.search" in plist.read_text()


def test_register_writes_plist_with_python_executable(tmp_path: Path) -> None:
    """register() includes sys.executable in ProgramArguments."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    with patch.object(type(svc), "_plist_path", property(lambda self: plist)):
        svc.register()
    assert sys.executable in plist.read_text()


def test_register_plist_uses_taskpolicy(tmp_path: Path) -> None:
    """register() wraps server command with taskpolicy -b for background QoS."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    with patch.object(type(svc), "_plist_path", property(lambda self: plist)):
        svc.register()
    content = plist.read_text()
    assert "/usr/sbin/taskpolicy" in content
    assert "<string>-b</string>" in content


def test_register_raises_on_permission_error(tmp_path: Path) -> None:
    """register() raises RuntimeError when it cannot write the plist."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch("pathlib.Path.write_text", side_effect=PermissionError),
    ):
        with pytest.raises(RuntimeError):
            svc.register()


def test_unregister_unloads_and_deletes_plist(tmp_path: Path) -> None:
    """unregister() calls launchctl unload <plist> then removes the plist file."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    plist.write_text("<plist/>")

    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=True),
        patch.object(svc, "_run", side_effect=mock_run),
    ):
        svc.unregister()

    assert calls == [["launchctl", "unload", str(plist)]]
    assert not plist.exists()


def test_unregister_noop_when_not_loaded(tmp_path: Path) -> None:
    """unregister() deletes plist without calling launchctl when not loaded."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    plist.write_text("<plist/>")

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=False),
        patch.object(svc, "_run") as mock_run,
    ):
        svc.unregister()
        mock_run.assert_not_called()
    assert not plist.exists()


def test_unregister_warns_and_deletes_plist_on_unload_failure(tmp_path: Path) -> None:
    """unregister() logs a warning when launchctl unload fails but still deletes the plist."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    plist.write_text("<plist/>")

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=True),
        patch.object(svc, "_run", return_value=_fail("domain error")),
        patch("archon_search.platform.macos.log") as mock_log,
    ):
        svc.unregister()  # should NOT raise

    mock_log.warning.assert_called_once()
    assert not plist.exists()


def test_unregister_raises_when_launchctl_not_found(tmp_path: Path) -> None:
    """unregister() raises RuntimeError when launchctl binary is absent."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=True),
        patch.object(svc, "_run", side_effect=FileNotFoundError),
    ):
        with pytest.raises(RuntimeError, match="launchctl binary not found"):
            svc.unregister()


# ── restart default ───────────────────────────────────────────────────────────

def test_restart_calls_stop_then_start() -> None:
    """restart() (default impl) calls stop() then start()."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    calls: list[str] = []
    with (
        patch.object(svc, "stop", side_effect=lambda: calls.append("stop")),
        patch.object(svc, "start", side_effect=lambda: calls.append("start")),
    ):
        svc.restart()
    assert calls == ["stop", "start"]
