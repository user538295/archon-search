"""Tests for Task 3.3 — SystemdSearchService (Linux)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout=stdout, stderr=stderr)


# ── ABC conformance ────────────────────────────────────────────────────────────

def test_is_instance_of_lifecycle() -> None:
    from archon_search.platform.linux import SystemdSearchService
    from archon_search.platform.service import SearchServiceLifecycle
    assert issubclass(SystemdSearchService, SearchServiceLifecycle)


# ── start ──────────────────────────────────────────────────────────────────────

def test_start_calls_systemctl_start() -> None:
    """start() calls systemctl --user start archon-search."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    with patch.object(svc, "_run", side_effect=mock_run):
        svc.start()

    assert ["systemctl", "--user", "start", "archon-search"] in calls


def test_start_raises_on_failure() -> None:
    """start() raises RuntimeError when systemctl returns non-zero."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    with patch.object(svc, "_run", return_value=_fail("start error")):
        with pytest.raises(RuntimeError, match="systemctl start failed"):
            svc.start()


def test_start_raises_when_systemctl_not_found() -> None:
    """start() raises RuntimeError when systemctl binary is absent."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    with patch.object(svc, "_run", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match="systemctl binary not found"):
            svc.start()


# ── stop ───────────────────────────────────────────────────────────────────────

def test_stop_calls_systemctl_stop() -> None:
    """stop() calls systemctl --user stop archon-search."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    with patch.object(svc, "_run", side_effect=mock_run):
        svc.stop()

    assert ["systemctl", "--user", "stop", "archon-search"] in calls


def test_stop_raises_on_failure() -> None:
    """stop() raises RuntimeError when systemctl returns non-zero."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    with patch.object(svc, "_run", return_value=_fail("stop error")):
        with pytest.raises(RuntimeError, match="systemctl stop failed"):
            svc.stop()


def test_stop_raises_when_systemctl_not_found() -> None:
    """stop() raises RuntimeError when systemctl binary is absent."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    with patch.object(svc, "_run", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match="systemctl binary not found"):
            svc.stop()


# ── status ─────────────────────────────────────────────────────────────────────

def test_status_running() -> None:
    """status() returns ServiceStatus(running=True) when is-active returns 'active'."""
    from archon_search.platform.linux import SystemdSearchService
    from archon_search.platform.service import ServiceStatus
    svc = SystemdSearchService()

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "is-active" in cmd:
            return _ok(stdout="active\n")
        if "show" in cmd:
            return _ok(stdout="MainPID=12345\n")
        return _ok()

    with patch.object(svc, "_run", side_effect=mock_run):
        s = svc.status()

    assert isinstance(s, ServiceStatus)
    assert s.running is True
    assert s.pid == 12345


def test_status_stopped() -> None:
    """status() returns ServiceStatus(running=False) when is-active returns 'inactive'."""
    from archon_search.platform.linux import SystemdSearchService
    from archon_search.platform.service import ServiceStatus
    svc = SystemdSearchService()

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "is-active" in cmd:
            return _fail(stdout="inactive\n")
        return _ok()

    with patch.object(svc, "_run", side_effect=mock_run):
        s = svc.status()

    assert isinstance(s, ServiceStatus)
    assert s.running is False


def test_status_returns_stopped_on_exception() -> None:
    """status() returns ServiceStatus(running=False) when systemctl raises."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    with patch.object(svc, "_run", side_effect=FileNotFoundError):
        s = svc.status()
    assert s.running is False


def test_status_pid_zero_returns_stopped() -> None:
    """PID=0 means service is not running."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "is-active" in cmd:
            return _ok(stdout="active\n")
        if "show" in cmd:
            return _ok(stdout="MainPID=0\n")
        return _ok()

    with patch.object(svc, "_run", side_effect=mock_run):
        s = svc.status()

    assert s.running is False


# ── register ───────────────────────────────────────────────────────────────────

def test_register_writes_unit_file(tmp_path: Path) -> None:
    """register() writes a systemd unit file to the correct path."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"

    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch.object(svc, "_run", return_value=_ok()),
    ):
        svc.register()

    assert unit.exists()
    content = unit.read_text()
    assert "archon_search.server" in content
    assert sys.executable in content


def test_register_raises_on_permission_error(tmp_path: Path) -> None:
    """register() raises RuntimeError when it cannot write the unit file."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"
    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch("pathlib.Path.write_text", side_effect=PermissionError),
    ):
        with pytest.raises(RuntimeError):
            svc.register()


def test_register_calls_daemon_reload_and_enable(tmp_path: Path) -> None:
    """register() calls systemctl daemon-reload then systemctl enable."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"
    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch.object(svc, "_run", side_effect=mock_run),
    ):
        svc.register()

    cmds_flat = [" ".join(c) for c in calls]
    assert any("daemon-reload" in c for c in cmds_flat)
    assert any("enable" in c for c in cmds_flat)


# ── unregister ─────────────────────────────────────────────────────────────────

def test_unregister_stops_disables_and_removes_unit(tmp_path: Path) -> None:
    """unregister() stops, disables, removes unit file, and reloads daemon."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"
    unit.write_text("[Unit]")
    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch.object(svc, "_run", side_effect=mock_run),
    ):
        svc.unregister()

    cmds_flat = [" ".join(c) for c in calls]
    assert any("stop" in c for c in cmds_flat)
    assert any("disable" in c for c in cmds_flat)
    assert any("daemon-reload" in c for c in cmds_flat)
    assert not unit.exists()


# ── restart ────────────────────────────────────────────────────────────────────

def test_restart_calls_systemctl_restart() -> None:
    """restart() calls systemctl --user restart archon-search directly."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    with patch.object(svc, "_run", side_effect=mock_run):
        svc.restart()

    assert ["systemctl", "--user", "restart", "archon-search"] in calls


def test_restart_raises_on_failure() -> None:
    """restart() raises RuntimeError when systemctl returns non-zero."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    with patch.object(svc, "_run", return_value=_fail("restart error")):
        with pytest.raises(RuntimeError, match="systemctl restart failed"):
            svc.restart()


def test_restart_raises_when_systemctl_not_found() -> None:
    """restart() raises RuntimeError when systemctl binary is absent."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    with patch.object(svc, "_run", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match="systemctl binary not found"):
            svc.restart()


# ── register rollback ──────────────────────────────────────────────────────────

def test_register_raises_and_cleans_up_on_enable_failure(tmp_path: Path) -> None:
    """register() deletes unit file and re-runs daemon-reload when enable fails."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"
    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "enable" in cmd:
            return _fail("enable error")
        return _ok()

    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch.object(svc, "_run", side_effect=mock_run),
    ):
        with pytest.raises(RuntimeError):
            svc.register()

    assert not unit.exists(), "unit file should have been deleted on rollback"
    daemon_reload_calls = [c for c in calls if "daemon-reload" in c]
    assert len(daemon_reload_calls) >= 2, "daemon-reload should be called at least twice (initial + rollback)"


def test_register_calls_loginctl_enable_linger(tmp_path: Path) -> None:
    """register() calls loginctl enable-linger for the current user."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"
    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch.object(svc, "_run", side_effect=mock_run),
    ):
        svc.register()

    cmds_flat = [" ".join(c) for c in calls]
    assert any("enable-linger" in c for c in cmds_flat), "loginctl enable-linger must be called"


# ── _parse_pid edge cases ──────────────────────────────────────────────────────

def test_parse_pid_returns_none_for_empty_string() -> None:
    from archon_search.platform.linux import SystemdSearchService
    assert SystemdSearchService._parse_pid("") is None


def test_parse_pid_returns_none_for_no_match() -> None:
    from archon_search.platform.linux import SystemdSearchService
    assert SystemdSearchService._parse_pid("SomeOtherKey=123") is None


def test_parse_pid_returns_none_for_zero() -> None:
    from archon_search.platform.linux import SystemdSearchService
    assert SystemdSearchService._parse_pid("MainPID=0") is None


def test_parse_pid_returns_int_for_valid_pid() -> None:
    from archon_search.platform.linux import SystemdSearchService
    assert SystemdSearchService._parse_pid("MainPID=9999") == 9999


# ── status edge cases ──────────────────────────────────────────────────────────

def test_status_returns_stopped_when_no_mainpid() -> None:
    """status() returns running=False when is-active is 'active' but show returns no MainPID."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "is-active" in cmd:
            return _ok(stdout="active\n")
        if "show" in cmd:
            return _ok(stdout="")
        return _ok()

    with patch.object(svc, "_run", side_effect=mock_run):
        s = svc.status()

    assert s.running is False


# ── unregister edge cases ──────────────────────────────────────────────────────

def test_unregister_noop_when_unit_file_absent(tmp_path: Path) -> None:
    """unregister() completes without raising when unit file does not exist."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"
    # unit file is NOT created

    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch.object(svc, "_run", return_value=_ok()),
    ):
        svc.unregister()  # must not raise


def test_unregister_does_not_raise_when_systemctl_missing() -> None:
    """unregister() completes without raising even when _run raises FileNotFoundError."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    with patch.object(svc, "_run", side_effect=FileNotFoundError):
        svc.unregister()  # must not raise (best-effort contract)


# ── Task 1.7 — path migration ──────────────────────────────────────────────────

def test_service_name_unchanged() -> None:
    """Service name archon-search must remain unchanged."""
    from archon_search.platform.linux import _SERVICE_NAME
    assert _SERVICE_NAME == "archon-search"


def test_cwd_is_archon_search(tmp_path: Path) -> None:
    """register() must use ~/.archon-search as WorkingDirectory."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"
    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch.object(svc, "_run", return_value=_ok()),
    ):
        svc.register()
    content = unit.read_text()
    expected_cwd = str(Path.home() / ".archon-search")
    assert expected_cwd in content


def test_config_path_is_archon_search(tmp_path: Path) -> None:
    """register() must reference ~/.archon-search/archon-search.toml as config."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"
    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch.object(svc, "_run", return_value=_ok()),
    ):
        svc.register()
    content = unit.read_text()
    expected_config = str(Path.home() / ".archon-search" / "archon-search.toml")
    assert expected_config in content
