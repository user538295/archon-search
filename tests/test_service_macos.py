"""Tests for — LaunchdSearchService (macOS)."""
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
    """stop() calls launchctl unload <plist> when service is loaded.

    ``_wait_until_stopped`` is patched out so this test asserts only the unload
    command (the S04 wait is verified separately in
    ``test_stop_waits_until_stopped``).
    """
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
        patch.object(svc, "_wait_until_stopped", return_value=True),
    ):
        svc.stop()

    assert calls == [["launchctl", "unload", str(plist)]]


def test_stop_waits_until_stopped(tmp_path: Path) -> None:
    """stop() calls _wait_until_stopped() after a successful unload (S04)."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=True),
        patch.object(svc, "_run", return_value=_ok()),
        patch.object(svc, "_wait_until_stopped", return_value=True) as mock_wait,
    ):
        svc.stop()

    mock_wait.assert_called_once()


def test_stop_warns_and_returns_1_when_not_stopped_in_time(tmp_path: Path) -> None:
    """stop() logs a WARNING and returns 1 when the wait times out (S04)."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=True),
        patch.object(svc, "_run", return_value=_ok()),
        patch.object(svc, "_wait_until_stopped", return_value=False),
        patch("archon_search.platform.macos.log") as mock_log,
    ):
        rc = svc.stop()

    assert rc == 1
    mock_log.warning.assert_called_once()


def test_stop_returns_0_when_confirmed_stopped(tmp_path: Path) -> None:
    """stop() returns 0 when the wait confirms the service is down (S04)."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"

    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch.object(svc, "_is_loaded", return_value=True),
        patch.object(svc, "_run", return_value=_ok()),
        patch.object(svc, "_wait_until_stopped", return_value=True),
    ):
        assert svc.stop() == 0


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
    """register() launches the server via a wrapper script that uses sys.executable."""
    from pathlib import Path as _Path
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(_Path, "home", return_value=tmp_path):
        svc.register()
    # The wrapper script (not the plist) contains the Python executable path
    wrapper = tmp_path / ".archon-search" / "run-server.sh"
    assert sys.executable in wrapper.read_text()


def test_register_plist_uses_taskpolicy(tmp_path: Path) -> None:
    """register() wraps server command with taskpolicy -b for background QoS."""
    from pathlib import Path as _Path
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(_Path, "home", return_value=tmp_path):
        svc.register()
    plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
    content = plist.read_text()
    assert "/usr/sbin/taskpolicy" in content
    assert "<string>-b</string>" in content


def test_register_writes_requested_config_path_into_plist(tmp_path: Path) -> None:
    """register(config_path=X) writes X into the plist's ARCHON_SEARCH_CONFIG (S206).

    The wizard's --config flag must reach the service it starts: the generated
    plist's ARCHON_SEARCH_CONFIG must point at the requested config, not the
    hardcoded ~/.archon-search/archon-search.toml default.
    """
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    requested = tmp_path / "custom" / "archon-search.toml"
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register(config_path=str(requested))
    plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
    content = plist.read_text()
    assert f"<string>{requested}</string>" in content
    hardcoded_default = tmp_path / ".archon-search" / "archon-search.toml"
    assert f"<string>{hardcoded_default}</string>" not in content


def test_register_defaults_config_path_when_omitted(tmp_path: Path) -> None:
    """register() with no config_path keeps the ~/.archon-search default."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register()
    plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
    default = tmp_path / ".archon-search" / "archon-search.toml"
    assert f"<string>{default}</string>" in plist.read_text()


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


def test_unregister_removes_wrapper_script(tmp_path: Path) -> None:
    """unregister() removes run-server.sh alongside the plist."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        # Pre-create both files that register() would have written
        plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("<plist/>")
        wrapper = tmp_path / ".archon-search" / "run-server.sh"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("#!/bin/sh\n")

        with patch.object(svc, "_is_loaded", return_value=False):
            svc.unregister()

    assert not plist.exists(), "unregister() must remove the plist"
    assert not wrapper.exists(), "unregister() must remove the wrapper script"


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
        patch.object(svc, "stop", side_effect=lambda **_: calls.append("stop")),
        patch.object(svc, "start", side_effect=lambda **_: calls.append("start")),
    ):
        svc.restart()
    assert calls == ["stop", "start"]


# ── — path migration ──────────────────────────────────────────────────

def test_service_label_unchanged() -> None:
    """Service label com.archon.search must remain unchanged."""
    from archon_search.platform.macos import _LABEL
    assert _LABEL == "com.archon.search"


def test_cwd_is_archon_search(tmp_path: Path) -> None:
    """register() must use ~/.archon-search as WorkingDirectory."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
        svc.register()
    content = plist.read_text()
    expected_cwd = str(tmp_path / ".archon-search")
    assert expected_cwd in content


def test_config_path_is_archon_search(tmp_path: Path) -> None:
    """register() must reference ~/.archon-search/archon-search.toml as config."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
        svc.register()
    content = plist.read_text()
    expected_config = str(tmp_path / ".archon-search" / "archon-search.toml")
    assert expected_config in content


def test_log_path_is_archon_search(tmp_path: Path) -> None:
    """register() must route stdout/stderr logs to ~/.archon-search/logs/archon-search.log."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
        svc.register()
    content = plist.read_text()
    expected_log = str(tmp_path / ".archon-search" / "logs" / "archon-search.log")
    assert expected_log in content


# ── BE-11: wrapper script + EnvironmentFile ────────────────────────────────────

def test_macos_plist_uses_wrapper_script(tmp_path: Path) -> None:
    """register() writes a plist whose ProgramArguments points to run-server.sh, not python directly."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register()
    plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
    content = plist.read_text()
    assert "run-server.sh" in content
    # The Python executable lives in the wrapper, not the plist directly
    assert sys.executable not in content


def test_macos_wrapper_script_content_guards_missing_file(tmp_path: Path) -> None:
    """register() writes a wrapper script that guards against absent .secrets.env with [ -f ] && set -a."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register()
    wrapper = tmp_path / ".archon-search" / "run-server.sh"
    assert wrapper.exists()
    content = wrapper.read_text()
    assert "[ -f" in content
    assert "set -a" in content
    assert "set +a" in content
    assert ".secrets.env" in content


def test_register_writes_wrapper_script_on_macos(tmp_path: Path) -> None:
    """register() writes run-server.sh with mode 0o755 in ~/.archon-search/."""
    import stat as _stat
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register()
    wrapper = tmp_path / ".archon-search" / "run-server.sh"
    assert wrapper.exists(), "run-server.sh must be written by register()"
    mode = wrapper.stat().st_mode
    assert _stat.S_IMODE(mode) == 0o755, f"run-server.sh must have mode 0o755, got {oct(_stat.S_IMODE(mode))}"


def test_wrapper_script_syntax_is_valid(tmp_path: Path) -> None:
    """Generated wrapper script passes sh -n syntax check; [ -f ] guard handles absent .secrets.env."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register()
    wrapper = tmp_path / ".archon-search" / "run-server.sh"
    result = subprocess.run(["sh", "-n", str(wrapper)], capture_output=True, text=True)
    assert result.returncode == 0, f"sh -n syntax check failed: {result.stderr}"


def test_register_dry_run_skips_wrapper_creation(tmp_path: Path) -> None:
    """register(dry_run=True) must not create run-server.sh or plist."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register(dry_run=True)
    wrapper = tmp_path / ".archon-search" / "run-server.sh"
    assert not wrapper.exists(), "dry_run must not create the wrapper script"
    plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
    assert not plist.exists(), "dry_run must not create the plist"
