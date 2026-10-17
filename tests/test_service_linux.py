"""Tests for — SystemdSearchService (Linux)."""
from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from archon_search.platform.linux import SystemdSearchService

_CONFIG_PATH = "/etc/archon-search/archon-search.toml"


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
    """stop() calls systemctl --user stop archon-search.

    ``_wait_until_stopped`` is patched out so this test asserts only the stop
    command (the S04 wait is verified separately in
    ``test_stop_waits_until_stopped``).
    """
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    with (
        patch.object(svc, "_run", side_effect=mock_run),
        patch.object(svc, "_wait_until_stopped", return_value=True),
    ):
        svc.stop()

    assert ["systemctl", "--user", "stop", "archon-search"] in calls


def test_stop_waits_until_stopped() -> None:
    """stop() calls _wait_until_stopped() after a successful systemctl stop (S04)."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()

    with (
        patch.object(svc, "_run", return_value=_ok()),
        patch.object(svc, "_wait_until_stopped", return_value=True) as mock_wait,
    ):
        svc.stop()

    mock_wait.assert_called_once()


def test_stop_warns_and_returns_1_when_not_stopped_in_time() -> None:
    """stop() logs a WARNING and returns 1 when the wait times out (S04)."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()

    with (
        patch.object(svc, "_run", return_value=_ok()),
        patch.object(svc, "_wait_until_stopped", return_value=False),
        patch("archon_search.platform.linux.log") as mock_log,
    ):
        rc = svc.stop()

    assert rc == 1
    mock_log.warning.assert_called_once()


def test_stop_returns_0_when_confirmed_stopped() -> None:
    """stop() returns 0 when the wait confirms the service is down (S04)."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()

    with (
        patch.object(svc, "_run", return_value=_ok()),
        patch.object(svc, "_wait_until_stopped", return_value=True),
    ):
        assert svc.stop() == 0


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
        svc.register(config_path=_CONFIG_PATH)

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
            svc.register(config_path=_CONFIG_PATH)


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
        svc.register(config_path=_CONFIG_PATH)

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
            svc.register(config_path=_CONFIG_PATH)

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
        svc.register(config_path=_CONFIG_PATH)

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


# ── — path migration ──────────────────────────────────────────────────

def test_service_name_unchanged() -> None:
    """Service name archon-search must remain unchanged."""
    from archon_search.platform.linux import _SERVICE_NAME
    assert _SERVICE_NAME == "archon-search"


def test_working_directory_is_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """register() must use ARCHON_SEARCH_DATA_DIR as WorkingDirectory."""
    data_dir = tmp_path / ".archon-search"
    content = _register_with_data_dir(tmp_path, monkeypatch, data_dir)
    assert _unit_values(content, "WorkingDirectory") == [str(data_dir)], content


_UNIT_FILENAME = "archon-search.service"


@contextmanager
def _isolated_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> Iterator[tuple[SystemdSearchService, Path, MagicMock]]:
    """Yield a service whose unit file lands in ``tmp_path`` and whose ``_run`` is a mock.

    ARCHON_SEARCH_DATA_DIR points at ``data_dir``.
    """
    from archon_search.platform.linux import SystemdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    svc = SystemdSearchService()
    unit = tmp_path / _UNIT_FILENAME
    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch.object(svc, "_run", return_value=_ok()) as run,
    ):
        yield svc, unit, run


def _register_with_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    config_path: Path | None = None,
) -> str:
    """Point ARCHON_SEARCH_DATA_DIR at ``data_dir``, run register(), return the unit text.

    ``config_path`` defaults to ``<data_dir>/archon-search.toml``.
    """
    with _isolated_service(tmp_path, monkeypatch, data_dir) as (svc, unit, _):
        svc.register(config_path=str(config_path or data_dir / "archon-search.toml"))
    return unit.read_text()


def _refused_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    config_path: Path | None = None,
) -> RuntimeError:
    """Run register() expecting a refusal; assert it wrote no unit and ran no systemctl; return the error."""
    with _isolated_service(tmp_path, monkeypatch, data_dir) as (svc, unit, run):
        with pytest.raises(RuntimeError) as excinfo:
            svc.register(config_path=str(config_path or data_dir / "archon-search.toml"))
        assert not unit.exists()
        run.assert_not_called()
    return excinfo.value


def test_register_writes_requested_config_path_into_unit(tmp_path: Path) -> None:
    """register(config_path=X) writes X into the unit's ARCHON_SEARCH_CONFIG (S206).

    The wizard's --config flag must reach the service it starts: the generated
    systemd unit's Environment=ARCHON_SEARCH_CONFIG must point at the requested config.
    """
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"
    requested = tmp_path / "custom" / "archon-search.toml"
    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch.object(svc, "_run", return_value=_ok()),
    ):
        svc.register(config_path=str(requested))
    assert f'Environment="ARCHON_SEARCH_CONFIG={requested}"' in unit.read_text()


def test_register_unit_includes_data_dir_when_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register() must include ARCHON_SEARCH_DATA_DIR in unit when env is set (S194/S201).

    When ARCHON_SEARCH_DATA_DIR is overridden (e.g. by the smoke test harness),
    the service process must inherit it so lid.176.ftz and logs are found at the
    same location the wizard used, not at the hardcoded ~/.archon-search default.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"
    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch.object(svc, "_run", return_value=_ok()),
    ):
        svc.register(config_path=str(tmp_path / "archon-search.toml"))
    content = unit.read_text()
    assert "ARCHON_SEARCH_DATA_DIR" in content, (
        "unit file must declare ARCHON_SEARCH_DATA_DIR so the service finds "
        "models/logs under the same data dir the wizard used"
    )
    assert f'Environment="ARCHON_SEARCH_DATA_DIR={tmp_path}"' in content.splitlines(), (
        f"unit file must propagate the overridden data dir {tmp_path}"
    )


@pytest.mark.parametrize(
    ("dir_name", "expected_suffix"),
    [
        # The wizard creates the secrets file under the data dir, so a hardcoded
        # %h/.archon-search/.secrets.env would be silently ignored on a custom
        # ARCHON_SEARCH_DATA_DIR.
        ("custom-data", "/custom-data/.secrets.env"),
        # systemd takes the whole value as one path without unquoting, so a space
        # stays verbatim and unquoted (added quotes would fail the absolute-path
        # check). The other directives' rules differ; see
        # test_register_escapes_each_directive_value.
        ("my data", "/my data/.secrets.env"),
        # systemd expands % specifiers in EnvironmentFile=, so an unescaped %h
        # would expand to the home dir and load the wrong file.
        ("100%h-data", "/100%%h-data/.secrets.env"),
        # systemd ignores a non-normalized path (one containing ..) with a warning,
        # and get_data_dir() keeps .., so such a path is resolved before rendering.
        ("a/../d", "/d/.secrets.env"),
    ],
    ids=["plain", "space", "percent", "dotdot"],
)
def test_register_environment_file_follows_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_name: str, expected_suffix: str
) -> None:
    """register() renders exactly one optional (``-``-prefixed) EnvironmentFile=-<data-dir>/.secrets.env line systemd accepts."""
    content = _register_with_data_dir(tmp_path, monkeypatch, tmp_path / dir_name)
    assert f"EnvironmentFile=-{tmp_path}{expected_suffix}\n" in content, content
    assert content.count("EnvironmentFile=") == 1, content


def test_register_environment_file_resolves_dotdot_through_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``..`` after a symlinked component resolves against the link's target, as the kernel does.

    With ``a`` -> ``elsewhere/sub``, ``a/../d`` is ``elsewhere/d`` on disk; a lexical
    ``os.path.normpath`` would wrongly render ``<tmp>/d``.
    """
    target = tmp_path / "elsewhere" / "sub"
    target.mkdir(parents=True)
    (tmp_path / "a").symlink_to(target)
    content = _register_with_data_dir(tmp_path, monkeypatch, tmp_path / "a" / ".." / "d")
    assert f"EnvironmentFile=-{tmp_path}/elsewhere/d/.secrets.env\n" in content, content
    assert content.count("EnvironmentFile=") == 1, content


def test_register_environment_file_keeps_symlinked_data_dir_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked data dir renders as the link, not its install-time target.

    Environment=ARCHON_SEARCH_DATA_DIR and the macOS wrapper keep the link, so
    retargeting it later must move the secrets file with the rest of the data dir.
    """
    target = tmp_path / "real-data"
    target.mkdir()
    link = tmp_path / "linked-data"
    link.symlink_to(target)
    content = _register_with_data_dir(tmp_path, monkeypatch, link)
    assert f"EnvironmentFile=-{tmp_path}/linked-data/.secrets.env\n" in content, content
    assert "real-data" not in content, content


# ── systemd quoting / specifier escaping of path-valued directives ─────────────
# Verified against systemd's man pages and src/core/load-fragment.c:
# - WorkingDirectory= (config_parse_working_directory): no unquoting, only %
#   specifier expansion (unit_path_printf), so a space stays verbatim and % is %%.
# - Environment= (config_parse_environ): each line is split into words with
#   EXTRACT_UNQUOTE|EXTRACT_CUNESCAPE, then % specifiers are expanded; $ has no
#   meaning. A value with a space needs the whole assignment double-quoted.
# - ExecStart= (config_parse_exec): split the same way, then % specifiers are
#   expanded (unit_path_printf for the executable, unit_full_printf for args), so
#   an executable path with a space is double-quoted and % is %%.
# - Every line (src/shared/conf-parser.c): a line ending in an unpaired backslash is
#   joined with the next one.

_SYSTEMD_ESCAPES = {"\\": "\\", '"': '"', "'": "'", "s": " "}
_WORD_SEPARATORS = " \t"
_QUOTES = "\"'"


def _systemd_unescape(value: str, backslash_at: int) -> str:
    """Return the character the escape starting at ``backslash_at`` stands for."""
    escaped = value[backslash_at + 1 : backslash_at + 2]
    if escaped not in _SYSTEMD_ESCAPES:
        raise ValueError(f"unsupported escape in {value!r}")
    return _SYSTEMD_ESCAPES[escaped]


def _read_systemd_word(value: str, start: int) -> tuple[str, int]:
    """Read the word starting at ``start``; return it and the index just past it."""
    quote = value[start] if value[start] in _QUOTES else None
    i = start + 1 if quote else start
    word: list[str] = []
    while i < len(value):
        c = value[i]
        if c == quote:
            i += 1
            if i < len(value) and value[i] not in _WORD_SEPARATORS:
                raise ValueError(f"closing quote not followed by whitespace: {value!r}")
            return "".join(word), i
        if not quote and c in _WORD_SEPARATORS:
            break
        if c == "\\":
            word.append(_systemd_unescape(value, i))
            i += 2
            continue
        word.append(c)
        i += 1
    if quote:
        raise ValueError(f"unterminated quote: {value!r}")
    return "".join(word), i


def _systemd_split_words(value: str) -> list[str]:
    """Split ``value`` into words per systemd.syntax(7) "Quoting" (quotes plus simple C escapes)."""
    words: list[str] = []
    i = 0
    while i < len(value):
        if value[i] in _WORD_SEPARATORS:
            i += 1
            continue
        word, i = _read_systemd_word(value, i)
        words.append(word)
    return words


def _systemd_joins_next_line(line: str) -> bool:
    """Whether systemd's config parser would join ``line`` with the next (unpaired trailing backslash)."""
    escaped = False
    for c in line:
        escaped = not escaped if c == "\\" else False
    return escaped


def _systemd_expand_specifiers(value: str) -> str:
    """Resolve ``%%`` to ``%``; fail on any other ``%x``, which systemd would expand."""
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "%":
            if value[i + 1 : i + 2] != "%":
                raise ValueError(f"systemd would expand specifier %{value[i + 1 : i + 2]} in {value!r}")
            out.append("%")
            i += 2
            continue
        out.append(value[i])
        i += 1
    return "".join(out)


@pytest.mark.parametrize(
    ("value", "words"),
    [
        ('"a b" c', ["a b", "c"]),
        ('"x\\"y"', ['x"y']),
        ("'it''s'", None),
        ("a\\sb", ["a b"]),
    ],
    ids=["quoted-space", "escaped-dquote", "quote-not-followed-by-space", "escaped-space"],
)
def test_systemd_split_words_oracle(value: str, words: list[str] | None) -> None:
    """Pinned parses keep the test-side oracle honest: the round-trip test is only as good as this parser."""
    if words is None:
        with pytest.raises(ValueError, match="closing quote"):
            _systemd_split_words(value)
    else:
        assert _systemd_split_words(value) == words


def test_systemd_expand_specifiers_oracle() -> None:
    """``%%`` is a literal ``%``; any other specifier (``%h``) is one systemd would expand, so the oracle fails."""
    assert _systemd_expand_specifiers("100%%") == "100%"
    with pytest.raises(ValueError, match="specifier %h"):
        _systemd_expand_specifiers("%h/data")


def _unit_values(content: str, directive: str) -> list[str]:
    """Return the raw right-hand side of every ``directive=`` line in ``content``."""
    prefix = f"{directive}="
    return [line[len(prefix):] for line in content.splitlines() if line.startswith(prefix)]


@pytest.fixture
def awkward_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Data dir, config path and patched interpreter path, each containing a space and a ``%``.

    The data dir and config path also contain ``"`` and a mid-path backslash; the interpreter
    path cannot (systemd refuses both in an executable path).
    """
    data_dir = tmp_path / 'my "100%" da\\ta'
    config = tmp_path / 'conf "50%" d\\ir' / "archon-search.toml"
    python = tmp_path / "py 7% env" / "bin" / "python3"
    monkeypatch.setattr(sys, "executable", str(python))
    return data_dir, config, python


def test_register_escapes_each_directive_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, awkward_paths: tuple[Path, Path, Path]
) -> None:
    """Each path-valued directive is rendered with that directive's own systemd escaping.

    Unescaped, a space splits ExecStart=/Environment= into extra words, a ``%`` is read as a
    specifier, and a ``"`` or backslash ends or escapes a quoted word, so the service runs the
    wrong binary or gets wrong paths.
    """
    data_dir, config, _ = awkward_paths
    content = _register_with_data_dir(tmp_path, monkeypatch, data_dir, config_path=config)
    lines = content.splitlines()
    expected = [
        f'ExecStart="{tmp_path}/py 7%% env/bin/python3" -m archon_search.server',
        f'WorkingDirectory={tmp_path}/my "100%%" da\\ta',
        f'Environment="ARCHON_SEARCH_CONFIG={tmp_path}/conf \\"50%%\\" d\\\\ir/archon-search.toml"',
        f'Environment="ARCHON_SEARCH_DATA_DIR={tmp_path}/my \\"100%%\\" da\\\\ta"',
        f'EnvironmentFile=-{tmp_path}/my "100%%" da\\ta/.secrets.env',
    ]
    missing = [line for line in expected if line not in lines]
    assert not missing, f"missing {missing!r} in:\n{content}"


def test_register_unit_values_round_trip_through_systemd_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, awkward_paths: tuple[Path, Path, Path]
) -> None:
    """Parsing the rendered lines the way systemd does yields the original paths."""
    data_dir, config, python = awkward_paths
    content = _register_with_data_dir(tmp_path, monkeypatch, data_dir, config_path=config)

    joined = [line for line in content.splitlines() if _systemd_joins_next_line(line)]
    assert not joined, f"systemd would join these lines with the next: {joined!r}"

    (exec_start,) = _unit_values(content, "ExecStart")
    argv = [_systemd_expand_specifiers(w) for w in _systemd_split_words(exec_start)]
    assert argv == [str(python), "-m", "archon_search.server"]

    (working_dir,) = _unit_values(content, "WorkingDirectory")
    assert _systemd_expand_specifiers(working_dir) == str(data_dir)

    (env_file,) = _unit_values(content, "EnvironmentFile")
    assert _systemd_expand_specifiers(env_file.removeprefix("-")) == str(data_dir / ".secrets.env")

    env: dict[str, str] = {}
    for value in _unit_values(content, "Environment"):
        for word in _systemd_split_words(value):
            name, _, val = _systemd_expand_specifiers(word).partition("=")
            env[name] = val
    assert env == {"ARCHON_SEARCH_CONFIG": str(config), "ARCHON_SEARCH_DATA_DIR": str(data_dir)}


def test_register_working_directory_resolves_dotdot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """systemd rejects a WorkingDirectory= containing ``..`` as not normalized, failing the unit.

    get_data_dir() keeps ``..``, so such a cwd is resolved before rendering, like EnvironmentFile=.
    """
    content = _register_with_data_dir(tmp_path, monkeypatch, tmp_path / "a" / ".." / "d")
    assert _unit_values(content, "WorkingDirectory") == [f"{tmp_path}/d"], content


@pytest.mark.parametrize(
    "char",
    ['"', "'", "\\", "$", "*", "?", "["],
    ids=["dquote", "squote", "backslash", "dollar", "star", "question", "bracket"],
)
def test_register_refuses_interpreter_path_systemd_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, char: str
) -> None:
    """An interpreter path with a character ExecStart= cannot express fails register() and writes no unit.

    systemd rejects quotes and backslashes (and, since v261, glob characters) in the executable
    path, and ``$`` cannot stay literal in both argv[0] and the executed path, so the unit would
    never start.
    """
    monkeypatch.setattr(sys, "executable", str(tmp_path / f"py{char}env" / "bin" / "python3"))
    exc = _refused_registration(tmp_path, monkeypatch, tmp_path / "data")
    assert f"interpreter path containing quotes, backslashes, $ or *?[: {sys.executable!r}" in str(exc)


def test_register_dry_run_refuses_interpreter_path_systemd_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` fails exactly where the real run would, instead of passing."""
    from archon_search.platform.linux import SystemdSearchService
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "py$env" / "bin" / "python3"))
    with pytest.raises(RuntimeError, match="interpreter path"):
        SystemdSearchService().register(dry_run=True, config_path=_CONFIG_PATH)


# The data dir reaches the unit twice; WorkingDirectory= is checked first, so its label names a bad data dir.
_VALUE_LABELS = ["interpreter path", "working directory", "config path", "secrets file path"]


def _with_bad_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, bad: str
) -> tuple[Path, Path]:
    """Put the path component ``bad`` into the unit value ``label``; return (data dir, config path) for register().

    The data directory is patched where linux.py reads it, since some values (a NUL) cannot pass through the env.
    """
    data_dir = tmp_path / "data"
    config = data_dir / "archon-search.toml"
    if label == "interpreter path":
        monkeypatch.setattr(sys, "executable", str(tmp_path / bad / "python3"))
    elif label == "working directory":
        monkeypatch.setattr("archon_search.platform.linux.get_data_dir", lambda: tmp_path / bad)
    elif label == "config path":
        config = tmp_path / bad / "archon-search.toml"
    else:
        monkeypatch.setattr(
            "archon_search.platform.linux.get_secrets_env_file", lambda: tmp_path / bad / ".secrets.env"
        )
    return data_dir, config


@pytest.mark.parametrize(
    "char",
    ["\n", "\r", "\t", "\x00", "\x1f", "\x7f"],
    ids=["newline", "cr", "tab", "nul", "unit-sep", "del"],
)
@pytest.mark.parametrize("label", _VALUE_LABELS)
def test_register_refuses_control_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, char: str
) -> None:
    """A control character in any unit value fails register() naming that value, and writes no unit.

    A newline would end the directive and inject the rest of the value as an extra unit line.
    """
    data_dir, config = _with_bad_value(tmp_path, monkeypatch, label, f"bad{char}Environment=INJECTED=1")
    exc = _refused_registration(tmp_path, monkeypatch, data_dir, config_path=config)
    assert f"{label} containing control characters" in str(exc)


@pytest.mark.parametrize("label", _VALUE_LABELS)
def test_register_refuses_value_not_encodable_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """A non-UTF-8 path (a surrogate-escaped byte from os.fsdecode) fails register() instead of the unit write.

    Unchecked, write_text() would raise UnicodeEncodeError only after install had stopped the running service.
    """
    data_dir, config = _with_bad_value(tmp_path, monkeypatch, label, "bad\udcffbyte")
    exc = _refused_registration(tmp_path, monkeypatch, data_dir, config_path=config)
    assert f"{label} that is not valid UTF-8" in str(exc)


@pytest.mark.parametrize("backslashes", [1, 3], ids=["one", "three"])
def test_register_refuses_data_dir_ending_in_unpaired_backslash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backslashes: int
) -> None:
    """An odd trailing backslash run on the unquoted WorkingDirectory= line would join it with the next unit line."""
    exc = _refused_registration(tmp_path, monkeypatch, tmp_path / ("data" + "\\" * backslashes))
    assert "working directory ending in a backslash" in str(exc)


def test_register_accepts_data_dir_ending_in_paired_backslashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """systemd's config parser (src/shared/conf-parser.c) reads an even trailing backslash run literally."""
    data_dir = tmp_path / "data\\\\"
    content = _register_with_data_dir(tmp_path, monkeypatch, data_dir)
    assert _unit_values(content, "WorkingDirectory") == [str(data_dir)], content


def test_register_refuses_dotdot_data_dir_whose_resolved_form_ends_in_backslash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check covers the resolved WorkingDirectory= value written to the unit, not the data dir as given."""
    exc = _refused_registration(tmp_path, monkeypatch, tmp_path / "data\\" / "sub" / "..")
    assert "working directory ending in a backslash" in str(exc)


def test_register_refuses_dotdot_data_dir_through_symlink_to_control_character(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving ``..`` follows symlinks, so a link whose target name has a newline must be refused, not written."""
    target = tmp_path / "real\nEnvironment=INJECTED=1"
    (target / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(target)
    exc = _refused_registration(tmp_path, monkeypatch, link / "sub" / "..")
    assert "working directory containing control characters" in str(exc)


def test_register_writes_non_ascii_path_as_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The unit is UTF-8 (what systemd reads) even when the locale's default encoding is not.

    ``write_text`` falls back to latin-1 here when no encoding is passed, as it would under a non-UTF-8 locale.
    """
    original_write_text = Path.write_text

    def write_text_latin1_locale(self: Path, data: str, encoding: str | None = None, **kwargs: object) -> int:
        return original_write_text(self, data, encoding=encoding or "latin-1", **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", write_text_latin1_locale)
    data_dir = tmp_path / "données"
    with _isolated_service(tmp_path, monkeypatch, data_dir) as (svc, unit, _):
        svc.register(config_path=str(data_dir / "archon-search.toml"))
    content = unit.read_bytes().decode("utf-8")
    assert _unit_values(content, "WorkingDirectory") == [str(data_dir)], content


def test_register_accepts_quoted_value_ending_in_unpaired_backslash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only unquoted values can end a line: a quoted word always ends in ``"``, so a config path ending in ``\\`` is fine."""
    data_dir = tmp_path / "data"
    config = tmp_path / "conf\\"
    content = _register_with_data_dir(tmp_path, monkeypatch, data_dir, config_path=config)
    env = dict(
        _systemd_expand_specifiers(word).partition("=")[::2]
        for value in _unit_values(content, "Environment")
        for word in _systemd_split_words(value)
    )
    assert env["ARCHON_SEARCH_CONFIG"] == str(config), content


def test_register_refuses_unquoted_value_with_outer_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """systemd strips whitespace around a directive value (src/shared/conf-parser.c ``strstrip``), changing the path.

    get_data_dir() strips the env value, but resolving ``..`` through a symlink can still end the path in a space.
    """
    target = tmp_path / "data "
    (target / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(target)
    exc = _refused_registration(tmp_path, monkeypatch, link / "sub" / "..")
    assert f"working directory with leading or trailing whitespace: {str(target)!r}" in str(exc)


def test_register_refuses_unquoted_value_still_containing_dotdot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If resolve() leaves a ``..`` (it gives up on a symlink loop), systemd would ignore the non-normalized path."""
    unresolved = tmp_path / "loop" / ".." / "data"
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: unresolved)
    exc = _refused_registration(tmp_path, monkeypatch, unresolved)
    assert f"working directory that is not normalized (contains ..): {str(unresolved)!r}" in str(exc)


def test_register_wraps_unit_write_os_error(tmp_path: Path) -> None:
    """A read-only or full filesystem fails with a clean RuntimeError naming the unit, not an OSError traceback."""
    from archon_search.platform.linux import SystemdSearchService
    svc = SystemdSearchService()
    unit = tmp_path / "archon-search.service"
    with (
        patch.object(type(svc), "_unit_path", property(lambda self: unit)),
        patch("pathlib.Path.write_text", side_effect=OSError(30, "Read-only file system")),
        pytest.raises(RuntimeError, match=re.escape(f"Could not write {unit}")),
    ):
        svc.register(config_path=_CONFIG_PATH)
