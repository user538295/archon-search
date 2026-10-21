"""Tests for — LaunchdSearchService (macOS)."""
from __future__ import annotations

import contextlib
import errno
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

_CONFIG_PATH = "/etc/archon-search/archon-search.toml"


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

def test_register_writes_plist_with_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """register() writes a plist containing the service label."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    with patch.object(type(svc), "_plist_path", property(lambda self: plist)):
        svc.register(config_path=_CONFIG_PATH)
    assert plist.exists()
    assert "com.archon.search" in plist.read_text()


def test_register_writes_plist_with_python_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register() launches the server via a wrapper script that uses sys.executable."""
    from pathlib import Path as _Path
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))
    svc = LaunchdSearchService()
    with patch.object(_Path, "home", return_value=tmp_path):
        svc.register(config_path=_CONFIG_PATH)
    # The wrapper script (not the plist) contains the Python executable path
    wrapper = tmp_path / ".archon-search" / "run-server.sh"
    assert f"exec {shlex.quote(sys.executable)} -m archon_search.server\n" in wrapper.read_text()


def test_register_plist_uses_taskpolicy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register() wraps server command with taskpolicy -b for background QoS."""
    from pathlib import Path as _Path
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))
    svc = LaunchdSearchService()
    with patch.object(_Path, "home", return_value=tmp_path):
        svc.register(config_path=_CONFIG_PATH)
    plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
    content = plist.read_text()
    assert "/usr/sbin/taskpolicy" in content
    assert "<string>-b</string>" in content


def test_register_writes_requested_config_path_into_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register(config_path=X) writes X into the plist's ARCHON_SEARCH_CONFIG (S206).

    The wizard's --config flag must reach the service it starts: the generated
    plist's ARCHON_SEARCH_CONFIG must point at the requested config, not the
    hardcoded ~/.archon-search/archon-search.toml default.
    """
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))
    svc = LaunchdSearchService()
    requested = tmp_path / "custom" / "archon-search.toml"
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register(config_path=str(requested))
    plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
    content = plist.read_text()
    assert f"<string>{requested}</string>" in content
    hardcoded_default = tmp_path / ".archon-search" / "archon-search.toml"
    assert f"<string>{hardcoded_default}</string>" not in content


def test_register_plist_includes_data_dir_when_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register() must include ARCHON_SEARCH_DATA_DIR in plist when env is set (S194/S201).

    When ARCHON_SEARCH_DATA_DIR is overridden (e.g. by the smoke test harness),
    the service process must inherit it so lid.176.ftz and logs are found at the
    same location the wizard used, not at the hardcoded ~/.archon-search default.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    plist = tmp_path / "com.archon.search.plist"
    with patch.object(type(svc), "_plist_path", property(lambda self: plist)):
        svc.register(config_path=str(tmp_path / "archon-search.toml"))
    content = plist.read_text()
    assert "ARCHON_SEARCH_DATA_DIR" in content, (
        "plist must declare ARCHON_SEARCH_DATA_DIR so the service finds "
        "models/logs under the same data dir the wizard used"
    )
    assert str(tmp_path) in content, (
        f"plist must propagate the overridden data dir {tmp_path}"
    )


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


def test_unregister_removes_wrapper_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """unregister() removes run-server.sh alongside the plist."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))
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


def test_cwd_is_archon_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """register() must use ARCHON_SEARCH_DATA_DIR as WorkingDirectory."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
        svc.register(config_path=_CONFIG_PATH)
    content = plist.read_text()
    expected_cwd = str(tmp_path / ".archon-search")
    assert expected_cwd in content


def test_log_path_is_archon_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """register() must route stdout/stderr logs to data_dir/logs/archon-search.log."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
        svc.register(config_path=_CONFIG_PATH)
    content = plist.read_text()
    expected_log = str(tmp_path / ".archon-search" / "logs" / "archon-search.log")
    assert expected_log in content
    assert (tmp_path / ".archon-search" / "logs").is_dir()


# ── BE-11: wrapper script + EnvironmentFile ────────────────────────────────────

def test_macos_plist_uses_wrapper_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """register() writes a plist whose ProgramArguments points to run-server.sh, not python directly."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register(config_path=_CONFIG_PATH)
    plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
    content = plist.read_text()
    assert "run-server.sh" in content
    # The Python executable lives in the wrapper, not the plist directly
    assert sys.executable not in content


def test_macos_wrapper_script_content_guards_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register() writes a wrapper script that guards against absent .secrets.env with [ -f ] && set -a."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register(config_path=_CONFIG_PATH)
    wrapper = tmp_path / ".archon-search" / "run-server.sh"
    assert wrapper.exists()
    content = wrapper.read_text()
    assert "[ -f" in content
    assert "set -a" in content
    assert "set +a" in content
    assert ".secrets.env" in content


def test_wrapper_script_sources_secrets_env_under_custom_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ARCHON_SEARCH_DATA_DIR away from home, the wrapper sources <data-dir>/.secrets.env."""
    from archon_search.platform.macos import LaunchdSearchService
    data_dir = tmp_path / "custom-data"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        svc.register(config_path=_CONFIG_PATH)
    content = (data_dir / "run-server.sh").read_text()
    secrets = shlex.quote(f"{tmp_path}/custom-data/.secrets.env")
    assert f"[ -f {secrets} ] && {{ set -a; . {secrets}; set +a; }}\n" in content, content
    assert content.count(".secrets.env") == 2, content


def test_register_writes_wrapper_script_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register() writes run-server.sh with mode 0o755 in data_dir."""
    import stat as _stat
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register(config_path=_CONFIG_PATH)
    wrapper = tmp_path / ".archon-search" / "run-server.sh"
    assert wrapper.exists(), "run-server.sh must be written by register()"
    mode = wrapper.stat().st_mode
    assert _stat.S_IMODE(mode) == 0o755, f"run-server.sh must have mode 0o755, got {oct(_stat.S_IMODE(mode))}"


def test_wrapper_script_syntax_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generated wrapper script passes sh -n syntax check; [ -f ] guard handles absent .secrets.env."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / ".archon-search"))
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register(config_path=_CONFIG_PATH)
    wrapper = tmp_path / ".archon-search" / "run-server.sh"
    result = subprocess.run(["sh", "-n", str(wrapper)], capture_output=True, text=True)
    assert result.returncode == 0, f"sh -n syntax check failed: {result.stderr}"


def test_register_dry_run_skips_wrapper_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """register(dry_run=True) must not create run-server.sh or plist."""
    from archon_search.platform.macos import LaunchdSearchService
    data_dir = tmp_path / ".archon-search"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    svc = LaunchdSearchService()
    with patch.object(Path, "home", return_value=tmp_path):
        svc.register(dry_run=True, config_path=_CONFIG_PATH)
    assert not (data_dir / "run-server.sh").exists(), "dry_run must not create the wrapper script"
    assert not data_dir.exists(), "dry_run must not create the data dir"
    plist = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"
    assert not plist.exists(), "dry_run must not create the plist"


# ── escaping of path values in the plist and the wrapper script ───────────────
# The plist is XML: ``&`` and ``<`` in a <string> make it unparseable unless escaped.
# The wrapper is a sh script: ``"``, ``$``, backticks and ``\`` inside double quotes
# end the word, expand or run commands unless the value is shell-quoted.

# Space, ``&``, ``'``, ``$`` and ``<``: each breaks either XML or a naive quoting.
_XML_AWKWARD_NAME = "a b&c'd$e<f"
# Shell metacharacters, including harmless command-substitution payloads that would
# create a marker file in the wrapper's cwd if the value were not shell-quoted. The
# interpreter dir ends in a lone backtick, which leaves an unquoted wrapper unparseable.
_SHELL_AWKWARD_DATA_NAME = "d a\"t$(touch dmark)`touch dbt`'s\\x&<"
_SHELL_AWKWARD_PYTHON_DIR = "p y\"$(touch pmark)`touch pbt`'q\\$HOME`"
_INJECTION_MARKERS = ("dmark", "dbt", "pmark", "pbt")
_TEST_SECRET_NAME = "ARCHON_TEST_SECRET"
_TEST_SECRET_VALUE = "ok"
_CLEAN_PATH = "/usr/bin:/bin"
_WRAPPER_RUN_TIMEOUT_S = 30
_EXPECTED_THROTTLE_INTERVAL_S = 60


def _expected_job(data_dir: Path, config: Path) -> dict[str, object]:
    """The whole launchd job register() writes for ``data_dir`` and ``config``."""
    log_path = str(data_dir / "logs" / "archon-search.log")
    return {
        "Label": "com.archon.search",
        "ProgramArguments": ["/usr/sbin/taskpolicy", "-b", str(data_dir / "run-server.sh")],
        "WorkingDirectory": str(data_dir),
        "EnvironmentVariables": {
            "ARCHON_SEARCH_CONFIG": str(config),
            "ARCHON_SEARCH_DATA_DIR": str(data_dir),
        },
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "KeepAlive": True,
        "RunAtLoad": True,
        "ThrottleInterval": _EXPECTED_THROTTLE_INTERVAL_S,
    }


def _register_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_dir: Path, config_path: Path
) -> Path:
    """Point ARCHON_SEARCH_DATA_DIR at ``data_dir``, run register() with the plist redirected into ``tmp_path``.

    Returns the plist path; the wrapper lands at ``<data_dir>/run-server.sh``.
    """
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    svc = LaunchdSearchService()
    plist = tmp_path / "LaunchAgents" / "com.archon.search.plist"
    with patch.object(type(svc), "_plist_path", property(lambda self: plist)):
        svc.register(config_path=str(config_path))
    return plist


def test_register_plist_round_trips_xml_special_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every path value in the plist parses back exactly, even with ``&``, ``<``, ``'``, ``$`` and a space.

    Written unescaped, ``&`` and ``<`` make the plist invalid XML, so launchd cannot load the service.
    """
    import plistlib
    data_dir = tmp_path / _XML_AWKWARD_NAME
    config = tmp_path / f"conf {_XML_AWKWARD_NAME}" / "archon-search.toml"
    plist = _register_isolated(tmp_path, monkeypatch, data_dir, config)

    parsed = plistlib.loads(plist.read_bytes())

    assert parsed == _expected_job(data_dir, config)
    # True == 1, so the dict comparison alone would accept an integer here.
    assert parsed["KeepAlive"] is True
    assert parsed["RunAtLoad"] is True
    assert type(parsed["ThrottleInterval"]) is int


# The stub interpreter must be executable for the wrapper's exec to run it.
_EXECUTABLE_MODE = 0o755
_EXPECTED_RECORD = ["-m", "archon_search.server", f"secret={_TEST_SECRET_VALUE}"]


def _install_recording_interpreter(monkeypatch: pytest.MonkeyPatch, stub: Path, record: Path) -> None:
    """Make ``stub`` sys.executable: a sh script that records its argv and the test secret it sees into ``record``."""
    stub.parent.mkdir(parents=True)
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(record))}\n"
        f"printf 'secret=%s\\n' \"${_TEST_SECRET_NAME}\" >> {shlex.quote(str(record))}\n"
    )
    stub.chmod(_EXECUTABLE_MODE)
    monkeypatch.setattr(sys, "executable", str(stub))


def _write_test_secret() -> None:
    """Write the secrets file the wrapper sources, at ``get_secrets_env_file()``, setting the test secret."""
    from archon_search.platform.service import get_secrets_env_file
    get_secrets_env_file().write_text(f"{_TEST_SECRET_NAME}={_TEST_SECRET_VALUE}\n")


def _sh_syntax_check(wrapper: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-n", str(wrapper)], capture_output=True, text=True, check=False, timeout=_WRAPPER_RUN_TIMEOUT_S
    )


def _run_wrapper(wrapper: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``wrapper`` under sh in ``cwd`` with a minimal environment."""
    return subprocess.run(
        ["/bin/sh", str(wrapper)],
        cwd=cwd,
        env={"PATH": _CLEAN_PATH},
        capture_output=True,
        text=True,
        check=False,
        timeout=_WRAPPER_RUN_TIMEOUT_S,
    )


def _recorded(record: Path) -> list[str] | None:
    """The lines the stub interpreter recorded, or None if it never ran."""
    return record.read_text().splitlines() if record.exists() else None


@pytest.fixture
def shell_awkward_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Register with a shell-hostile data dir and interpreter path; return (wrapper, record file, cwd).

    The interpreter is a stub that records its argv and the secret it sees into the record file.
    A secrets file at ``get_secrets_env_file()`` sets the secret.
    """
    data_dir = tmp_path / _SHELL_AWKWARD_DATA_NAME
    record = tmp_path / "record.txt"
    _install_recording_interpreter(monkeypatch, tmp_path / _SHELL_AWKWARD_PYTHON_DIR / "python3", record)

    _register_isolated(tmp_path, monkeypatch, data_dir, data_dir / "archon-search.toml")
    _write_test_secret()

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return data_dir / "run-server.sh", record, cwd


def test_wrapper_script_with_shell_metacharacters_passes_syntax_check(
    shell_awkward_wrapper: tuple[Path, Path, Path],
) -> None:
    """The wrapper stays valid sh when the data dir and interpreter path contain quotes, ``$``, backticks and ``\\``."""
    wrapper, _, _ = shell_awkward_wrapper
    result = _sh_syntax_check(wrapper)
    assert result.returncode == 0, f"sh -n failed: {result.stderr}\n{wrapper.read_text()}"


def test_wrapper_script_sources_exact_secrets_file_and_execs_exact_python(
    shell_awkward_wrapper: tuple[Path, Path, Path],
) -> None:
    """Run under sh, the wrapper sources the exact secrets file and execs the exact interpreter path.

    Unquoted for the shell, a ``"`` ends the word early and ``$(...)``/backticks run as commands; the
    payloads here only create marker files in the wrapper's cwd, which must not exist afterwards.
    """
    wrapper, record, cwd = shell_awkward_wrapper
    result = _run_wrapper(wrapper, cwd)

    assert _recorded(record) == _EXPECTED_RECORD, (
        f"rc={result.returncode} stderr={result.stderr!r}\n{wrapper.read_text()}"
    )
    assert result.returncode == 0, result.stderr
    created = [name for name in _INJECTION_MARKERS if (cwd / name).exists()]
    assert not created, f"wrapper ran injected commands creating {created}"


# ── refusing values the plist or the wrapper cannot carry ─────────────────────
# Verified on CPython 3.13: plistlib.dumps() raises ValueError ("strings can't contain
# control characters") for C0 controls other than \t \n \r (XML 1.0 forbids them), rewrites
# \r as \n, writes U+FFFE/U+FFFF that its own parser then rejects (ExpatError), and raises
# UnicodeEncodeError for a lone surrogate (a non-UTF-8 byte decoded by os.fsdecode).
# \t, \n, \x7f, C1 controls and non-ASCII round-trip, so they are accepted (see the positive test below).
# A NUL can never reach exec() as part of the interpreter path, so the wrapper cannot carry it.

# Copied, not imported: the operator-facing refusal wording is pinned deliberately.
_C0_CAUSE = "containing a C0 control character other than tab and newline"
_NONCHARACTER_CAUSE = "containing U+FFFE or U+FFFF"
_NON_UTF8_CAUSE = "that is not valid UTF-8"
_PLIST_UNREPRESENTABLE = [
    ("\x00", _C0_CAUSE),
    ("\x01", _C0_CAUSE),
    ("\x1f", _C0_CAUSE),
    ("\r", _C0_CAUSE),
    ("\ufffe", _NONCHARACTER_CAUSE),
    ("\uffff", _NONCHARACTER_CAUSE),
    ("\udcff", _NON_UTF8_CAUSE),
]
_PLIST_UNREPRESENTABLE_IDS = ["nul", "soh", "unit-sep", "cr", "u-fffe", "u-ffff", "non-utf8"]
_PLIST_REPRESENTABLE_NAME = "t\tn\nd\x7fc\x85é"
_REFUSED_CONFIG_PATH = "/etc/bad\x01conf/archon-search.toml"
# The server reads ARCHON_SEARCH_CONFIG with str.strip(), so it would open a different file.
_WHITESPACE_WRAPPED_CONFIG_PATHS = [f" {_CONFIG_PATH}", f"{_CONFIG_PATH} ", f"{_CONFIG_PATH}\t", f"{_CONFIG_PATH}\n"]
_WHITESPACE_WRAPPED_IDS = ["leading-space", "trailing-space", "trailing-tab", "trailing-newline"]


def _plist_refusal(label: str, value: str, cause: str) -> str:
    """The regex pytest.raises(match=) needs for the plist refusal of ``value`` as ``label``."""
    return re.escape(f"launchd plist cannot hold a {label} {cause}: {value!r}")


def _strip_refusal(label: str, value: str) -> str:
    """The regex pytest.raises(match=) needs for refusing ``value`` as ``label`` the server would strip."""
    return re.escape(f"archon-search would strip a {label} with leading or trailing whitespace: {value!r}")


@pytest.mark.parametrize(("char", "cause"), _PLIST_UNREPRESENTABLE, ids=_PLIST_UNREPRESENTABLE_IDS)
def test_validate_registration_refuses_config_path_plist_cannot_carry(char: str, cause: str) -> None:
    """A config path the plist would not read back unchanged is refused, naming the value and the specific cause."""
    from archon_search.platform.macos import LaunchdSearchService
    bad = f"/etc/bad{char}conf/archon-search.toml"
    with pytest.raises(RuntimeError, match=_plist_refusal("config path", bad, cause)):
        LaunchdSearchService().validate_registration(config_path=bad)


def test_validate_registration_names_generic_cause_for_unclassified_round_trip_failure() -> None:
    """A value the plist would not read back for a reason no specific check names still gets a refusal cause.

    Only the plain config path is made to fail the round trip, so the data-dir check before it still passes.
    """
    from archon_search.platform.macos import LaunchdSearchService
    with (
        patch("archon_search.platform.macos._plist_round_trips", side_effect=lambda value: value != _CONFIG_PATH),
        pytest.raises(
            RuntimeError, match=f"^{_plist_refusal('config path', _CONFIG_PATH, 'that would not read back unchanged')}$"
        ),
    ):
        LaunchdSearchService().validate_registration(config_path=_CONFIG_PATH)


# NUL is excluded: it cannot be put in an environment variable, so it never reaches get_data_dir().
@pytest.mark.parametrize(("char", "cause"), _PLIST_UNREPRESENTABLE[1:], ids=_PLIST_UNREPRESENTABLE_IDS[1:])
def test_validate_registration_refuses_data_dir_plist_cannot_carry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, char: str, cause: str
) -> None:
    """A data dir (WorkingDirectory, ARCHON_SEARCH_DATA_DIR, wrapper and log paths) the plist cannot carry is refused."""
    from archon_search.platform.macos import LaunchdSearchService
    bad = str(tmp_path / f"bad{char}data")
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", bad)
    with pytest.raises(RuntimeError, match=_plist_refusal("data directory", bad, cause)):
        LaunchdSearchService().validate_registration(config_path=_CONFIG_PATH)


@pytest.mark.parametrize("bad", _WHITESPACE_WRAPPED_CONFIG_PATHS, ids=_WHITESPACE_WRAPPED_IDS)
def test_validate_registration_refuses_config_path_with_surrounding_whitespace(bad: str) -> None:
    """The server strips ARCHON_SEARCH_CONFIG, so a config path with surrounding whitespace would load another file."""
    from archon_search.platform.macos import LaunchdSearchService
    with pytest.raises(RuntimeError, match=_strip_refusal("config path", bad)):
        LaunchdSearchService().validate_registration(config_path=bad)


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
@pytest.mark.parametrize("bad", _WHITESPACE_WRAPPED_CONFIG_PATHS, ids=_WHITESPACE_WRAPPED_IDS)
def test_register_refuses_config_path_with_surrounding_whitespace_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, bad: str
) -> None:
    """register() refuses the same config paths itself, ``--dry-run`` too, before writing anything."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "data"))
    svc = LaunchdSearchService()
    plist = tmp_path / "LaunchAgents" / "com.archon.search.plist"
    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        pytest.raises(RuntimeError, match=_strip_refusal("config path", bad)),
    ):
        svc.register(dry_run=dry_run, config_path=bad)
    assert not any(tmp_path.iterdir()), list(tmp_path.iterdir())


@pytest.mark.parametrize("whitespace", [" ", "\t", "\n"], ids=["space", "tab", "newline"])
def test_validate_registration_refuses_data_dir_with_trailing_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whitespace: str
) -> None:
    """get_data_dir() strips the env var before Path drops its trailing slash, so ``<dir><ws>/`` ends in whitespace.

    The server strips ARCHON_SEARCH_DATA_DIR again, so it would run on a different data dir than the plist names.
    """
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", f"{tmp_path}/data{whitespace}/")
    bad = f"{tmp_path}/data{whitespace}"
    with pytest.raises(RuntimeError, match=_strip_refusal("data directory", bad)):
        LaunchdSearchService().validate_registration(config_path=_CONFIG_PATH)


@pytest.mark.parametrize(
    ("python", "cause"),
    [
        ("/opt/py\x00thon/bin/python3", "containing NUL"),
        ("/opt/py\udcffthon/bin/python3", "that is not valid UTF-8"),
    ],
    ids=["nul", "non-utf8"],
)
def test_validate_registration_refuses_interpreter_path_wrapper_cannot_carry(
    monkeypatch: pytest.MonkeyPatch, python: str, cause: str
) -> None:
    """An interpreter path the wrapper cannot exec (NUL ends a C string) or write as UTF-8 is refused."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setattr(sys, "executable", python)
    expected = re.escape(f"wrapper script cannot hold an interpreter path {cause}: {python!r}")
    with pytest.raises(RuntimeError, match=expected):
        LaunchdSearchService().validate_registration(config_path=_CONFIG_PATH)


@pytest.mark.parametrize(
    ("secrets_name", "cause"),
    [("bad\x00secrets", "containing NUL"), ("bad\udcffsecrets", "that is not valid UTF-8")],
    ids=["nul", "non-utf8"],
)
def test_validate_registration_refuses_secrets_file_path_wrapper_cannot_carry(
    tmp_path: Path, secrets_name: str, cause: str
) -> None:
    """The secrets file path is checked itself, not assumed to be the data dir plus a fixed name."""
    from archon_search.platform.macos import LaunchdSearchService
    bad = tmp_path / secrets_name / ".secrets.env"
    expected = re.escape(f"wrapper script cannot hold a secrets file path {cause}: {str(bad)!r}")
    with (
        patch("archon_search.platform.macos.get_secrets_env_file", return_value=bad),
        pytest.raises(RuntimeError, match=expected),
    ):
        LaunchdSearchService().validate_registration(config_path=_CONFIG_PATH)


@pytest.mark.parametrize("python", ["", None], ids=["empty", "none"])
def test_validate_registration_refuses_undeterminable_interpreter(
    monkeypatch: pytest.MonkeyPatch, python: str | None
) -> None:
    """sys.executable is '' or None when Python cannot determine it; the wrapper would then exec nothing."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setattr(sys, "executable", python)
    expected = re.escape(f"wrapper script cannot exec an empty interpreter path: {python!r}")
    with pytest.raises(RuntimeError, match=expected):
        LaunchdSearchService().validate_registration(config_path=_CONFIG_PATH)


@pytest.mark.parametrize(
    ("config_path", "expectation"),
    [
        (_CONFIG_PATH, contextlib.nullcontext()),
        (_REFUSED_CONFIG_PATH, pytest.raises(RuntimeError, match=_plist_refusal("config path", _REFUSED_CONFIG_PATH, _C0_CAUSE))),
    ],
    ids=["accepted", "refused"],
)
def test_validate_registration_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_path: str,
    expectation: contextlib.AbstractContextManager[object],
) -> None:
    """validate_registration() is side-effect free: it creates neither the data dir nor the plist."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "data"))
    svc = LaunchdSearchService()
    plist = tmp_path / "LaunchAgents" / "com.archon.search.plist"
    with patch.object(type(svc), "_plist_path", property(lambda self: plist)), expectation:
        svc.validate_registration(config_path=config_path)
    assert not any(tmp_path.iterdir()), list(tmp_path.iterdir())


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_register_refuses_value_plist_cannot_carry_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    """register() is public, so it refuses the same values itself — ``--dry-run`` too — and writes nothing."""
    from archon_search.platform.macos import LaunchdSearchService
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    svc = LaunchdSearchService()
    plist = tmp_path / "LaunchAgents" / "com.archon.search.plist"
    bad = _REFUSED_CONFIG_PATH
    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        pytest.raises(RuntimeError, match=_plist_refusal("config path", bad, _C0_CAUSE)),
    ):
        svc.register(dry_run=dry_run, config_path=bad)
    assert not plist.exists()
    assert not (data_dir / "run-server.sh").exists()
    assert not data_dir.exists()


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
@pytest.mark.parametrize(
    ("data_name", "python", "refusal"),
    [
        ("bad\x01data", sys.executable, f"launchd plist cannot hold a data directory {_C0_CAUSE}"),
        ("data\t/", sys.executable, "archon-search would strip a data directory with leading or trailing whitespace"),
        ("data", "/opt/py\x00thon/bin/python3", "wrapper script cannot hold an interpreter path containing NUL"),
    ],
    ids=["data-dir-plist", "data-dir-whitespace", "interpreter"],
)
def test_register_refuses_data_dir_or_interpreter_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, data_name: str, python: str, refusal: str
) -> None:
    """register() refuses a data dir or interpreter path itself, ``--dry-run`` too, and writes nothing anywhere."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", f"{tmp_path}/{data_name}")
    monkeypatch.setattr(sys, "executable", python)
    svc = LaunchdSearchService()
    plist = tmp_path / "LaunchAgents" / "com.archon.search.plist"
    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        pytest.raises(RuntimeError, match=f"^{re.escape(refusal)}: "),
    ):
        svc.register(dry_run=dry_run, config_path=_CONFIG_PATH)
    assert not any(tmp_path.iterdir()), list(tmp_path.iterdir())


def test_replace_registration_refuses_before_stopping_running_service() -> None:
    """A refused value raises before pre_activate_cleanup() stops the running service or register() runs."""
    from archon_search.platform.macos import LaunchdSearchService
    svc = LaunchdSearchService()
    bad = _REFUSED_CONFIG_PATH
    with (
        patch.object(svc, "pre_activate_cleanup") as cleanup,
        patch.object(svc, "register") as register,
        pytest.raises(RuntimeError, match=_plist_refusal("config path", bad, _C0_CAUSE)),
    ):
        svc.replace_registration(config_path=bad)
    cleanup.assert_not_called()
    register.assert_not_called()


def test_register_accepts_and_round_trips_tab_newline_del_and_non_ascii(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Values the plist and wrapper carry intact are not over-refused: tab, newline, DEL, a C1 control and non-ASCII.

    The plist reads them back exactly, and the wrapper, run under sh, sources the secrets file and execs the
    interpreter at those exact paths.
    """
    import plistlib
    data_dir = tmp_path / f"data {_PLIST_REPRESENTABLE_NAME}"
    config = tmp_path / f"conf {_PLIST_REPRESENTABLE_NAME}" / "archon-search.toml"
    record = tmp_path / "record.txt"
    _install_recording_interpreter(monkeypatch, tmp_path / f"py {_PLIST_REPRESENTABLE_NAME}" / "python3", record)
    plist = _register_isolated(tmp_path, monkeypatch, data_dir, config)
    assert plistlib.loads(plist.read_bytes()) == _expected_job(data_dir, config)

    wrapper = data_dir / "run-server.sh"
    syntax = _sh_syntax_check(wrapper)
    assert syntax.returncode == 0, f"sh -n failed: {syntax.stderr}\n{wrapper.read_text()}"
    _write_test_secret()
    result = _run_wrapper(wrapper, tmp_path)
    assert _recorded(record) == _EXPECTED_RECORD, (
        f"rc={result.returncode} stderr={result.stderr!r}\n{wrapper.read_text()}"
    )


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("plutil") is None, reason="needs macOS plutil (CoreFoundation)"
)
def test_corefoundation_reads_awkward_accepted_paths_back_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CoreFoundation, the parser launchd uses, reads every accepted awkward value back exactly as plistlib wrote it.

    Backs the ``_plist_round_trips`` claim: tab, newline, DEL, a C1 control, ``&<>'"$`\\``, space and non-ASCII.
    This is macOS-local evidence only: it is skipped wherever plutil is absent, Linux CI included.
    """
    data_dir = tmp_path / f"data {_PLIST_REPRESENTABLE_NAME} {_XML_AWKWARD_NAME}"
    config = tmp_path / f"conf>{_SHELL_AWKWARD_DATA_NAME}" / "archon-search.toml"
    plist = _register_isolated(tmp_path, monkeypatch, data_dir, config)

    lint = subprocess.run(
        ["plutil", "-lint", str(plist)], capture_output=True, text=True, check=False, timeout=_WRAPPER_RUN_TIMEOUT_S
    )
    assert lint.returncode == 0, lint.stdout + lint.stderr
    converted = subprocess.run(
        ["plutil", "-convert", "json", "-o", "-", str(plist)],
        capture_output=True,
        check=False,
        timeout=_WRAPPER_RUN_TIMEOUT_S,
    )
    assert converted.returncode == 0, converted.stderr

    parsed = json.loads(converted.stdout)
    assert parsed["WorkingDirectory"] == str(data_dir)
    assert parsed["EnvironmentVariables"] == {
        "ARCHON_SEARCH_CONFIG": str(config),
        "ARCHON_SEARCH_DATA_DIR": str(data_dir),
    }
    assert parsed == _expected_job(data_dir, config)


def test_register_writes_non_ascii_paths_as_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper is UTF-8, like the plist, even when the locale's default encoding is not.

    ``write_text`` falls back to latin-1 here when no encoding is passed, as it would under a non-UTF-8 locale.
    """
    import plistlib
    original_write_text = Path.write_text

    def write_text_latin1_locale(self: Path, data: str, encoding: str | None = None, **kwargs: Any) -> int:
        return original_write_text(self, data, encoding=encoding or "latin-1", **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text_latin1_locale)
    data_dir = tmp_path / "données"
    config = data_dir / "archon-search.toml"
    plist = _register_isolated(tmp_path, monkeypatch, data_dir, config)

    wrapper = (data_dir / "run-server.sh").read_bytes().decode("utf-8")
    assert f". {shlex.quote(str(data_dir / '.secrets.env'))};" in wrapper, wrapper
    assert plistlib.loads(plist.read_bytes()) == _expected_job(data_dir, config)


@pytest.mark.parametrize(
    ("method", "error", "message", "wrapper_written"),
    [
        (
            "write_text",
            PermissionError(errno.EACCES, "Permission denied"),
            "Permission denied writing service files in {data_dir}",
            False,
        ),
        (
            "write_text",
            OSError(errno.EROFS, "Read-only file system"),
            "Could not write service files in {data_dir}",
            False,
        ),
        (
            "chmod",
            PermissionError(errno.EPERM, "Operation not permitted"),
            "Permission denied writing service files in {data_dir}",
            True,
        ),
        ("write_bytes", PermissionError(errno.EACCES, "Permission denied"), "Permission denied writing {plist}", True),
        ("write_bytes", OSError(errno.EROFS, "Read-only file system"), "Could not write {plist}", True),
    ],
    ids=["wrapper-permission", "wrapper-os-error", "wrapper-chmod-permission", "plist-permission", "plist-os-error"],
)
def test_register_wraps_write_errors_naming_the_path_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, error: OSError, message: str, wrapper_written: bool
) -> None:
    """A failed write is a clean RuntimeError naming where it failed: the data dir (wrapper) or the plist.

    A failed wrapper write or chmod stops register() before the plist, so launchd is never pointed at a missing
    or non-executable wrapper; a plist failure comes after the wrapper is written.
    """
    from archon_search.platform.macos import LaunchdSearchService
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    svc = LaunchdSearchService()
    plist = tmp_path / "LaunchAgents" / "com.archon.search.plist"
    expected = re.escape(message.format(data_dir=data_dir, plist=plist))
    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        patch(f"pathlib.Path.{method}", side_effect=error),
        pytest.raises(RuntimeError, match=f"^{expected}$") as excinfo,
    ):
        svc.register(config_path=_CONFIG_PATH)
    assert excinfo.value.__cause__ is error
    assert (data_dir / "run-server.sh").exists() is wrapper_written
    assert not plist.exists()


def test_register_wraps_launch_agents_mkdir_permission_error_naming_the_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LaunchAgents dir that cannot be created is a RuntimeError naming the plist, chained to the PermissionError."""
    from archon_search.platform.macos import LaunchdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "data"))
    svc = LaunchdSearchService()
    plist = tmp_path / "LaunchAgents" / "com.archon.search.plist"
    denied = PermissionError(errno.EACCES, "Permission denied")
    original_mkdir = Path.mkdir

    def mkdir_denied_for_launch_agents(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == plist.parent:
            raise denied
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir_denied_for_launch_agents)
    expected = re.escape(f"Permission denied writing {plist}")
    with (
        patch.object(type(svc), "_plist_path", property(lambda self: plist)),
        pytest.raises(RuntimeError, match=f"^{expected}$") as excinfo,
    ):
        svc.register(config_path=_CONFIG_PATH)
    assert excinfo.value.__cause__ is denied
    assert not plist.exists()
