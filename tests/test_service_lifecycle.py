"""Tests for SearchServiceLifecycle ABC."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from archon_search.platform.service import (
    SearchServiceLifecycle,
    ServiceStatus,
    check_interpreter_determined,
    check_survives_server_strip,
    get_secrets_env_file,
    write_errors_naming,
)


class ConcreteService(SearchServiceLifecycle):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self, dry_run: bool = False) -> int:
        self.calls.append("start")
        return 0

    def stop(self, dry_run: bool = False) -> int:
        self.calls.append("stop")
        return 0

    def status(self) -> ServiceStatus:
        return ServiceStatus(running=True, pid=1, uptime_seconds=0.0)

    def register(self, dry_run: bool = False, *, config_path: str) -> None:
        self.calls.append("register")

    def unregister(self, dry_run: bool = False) -> None:
        self.calls.append("unregister")


def test_abc_cannot_instantiate() -> None:
    with pytest.raises(TypeError):
        SearchServiceLifecycle()  # type: ignore[abstract]


def test_restart_default_calls_stop_then_start() -> None:
    svc = ConcreteService()
    svc.restart()
    assert svc.calls == ["stop", "start"]


# ── _wait_until_stopped (S04) ───────────────────────────────────────────────────

def test_wait_until_stopped_returns_true_when_status_flips(monkeypatch) -> None:
    """Polls status() until it reports not-running, then returns True."""
    svc = ConcreteService()
    poll_count = {"n": 0}

    def scripted_status() -> ServiceStatus:
        poll_count["n"] += 1
        # Report running for the first two polls, stopped on the third.
        return ServiceStatus(running=poll_count["n"] < 3, pid=1, uptime_seconds=0.0)

    monkeypatch.setattr(svc, "status", scripted_status)
    monkeypatch.setattr("archon_search.platform.service.time.sleep", lambda *_: None)

    assert svc._wait_until_stopped(timeout=5.0) is True
    assert poll_count["n"] == 3  # polled until not-running, no earlier


def test_wait_until_stopped_returns_false_on_timeout(monkeypatch) -> None:
    """Returns False (does not hang) when status() never reports not-running.

    ``ConcreteService.status()`` always reports running=True. A scripted clock
    drives the deadline past on the fourth poll so the loop exits deterministically.
    """
    svc = ConcreteService()
    poll_count = {"n": 0}
    real_status = svc.status

    def counting_status() -> ServiceStatus:
        poll_count["n"] += 1
        return real_status()

    monkeypatch.setattr(svc, "status", counting_status)
    monkeypatch.setattr("archon_search.platform.service.time.sleep", lambda *_: None)

    ticks = [0.0, 0.1, 0.2, 0.3]  # entry reads 0.0 → deadline 1.0; then three polls

    def fake_monotonic() -> float:
        return ticks.pop(0) if ticks else 1000.0

    monkeypatch.setattr("archon_search.platform.service.time.monotonic", fake_monotonic)

    assert svc._wait_until_stopped(timeout=1.0) is False
    assert poll_count["n"] == 3  # polled each iteration until the deadline passed


def test_get_secrets_env_file_lives_under_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_secrets_env_file() is <ARCHON_SEARCH_DATA_DIR>/.secrets.env, resolved at call time."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "first"))
    assert get_secrets_env_file() == tmp_path / "first" / ".secrets.env"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "second"))
    assert get_secrets_env_file() == tmp_path / "second" / ".secrets.env"


# ── validate_registration / replace_registration ───────────────────────────────

def test_validate_registration_default_accepts_any_config_path() -> None:
    """The base hook accepts everything and has no side effects; only platforms that validate override it."""
    svc = ConcreteService()
    assert svc.validate_registration(config_path="bad\n\udcff path\\") is None
    assert svc.calls == []


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_replace_registration_validates_then_stops_then_registers(
    monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    """replace_registration() validates, then stops, then registers, passing ``dry_run`` to both side-effecting steps.

    Dropping ``dry_run`` would make ``install --dry-run`` stop the real service.
    """
    svc = ConcreteService()
    steps: list[tuple[str, object]] = []
    monkeypatch.setattr(svc, "validate_registration", lambda *, config_path: steps.append(("validate", config_path)))
    monkeypatch.setattr(svc, "stop", lambda dry_run=False: steps.append(("stop", dry_run)) or 0)
    monkeypatch.setattr(
        svc, "register", lambda dry_run=False, *, config_path: steps.append(("register", (dry_run, config_path)))
    )

    svc.replace_registration(dry_run=dry_run, config_path="/etc/a.toml")

    assert steps == [
        ("validate", "/etc/a.toml"),
        ("stop", dry_run),
        ("register", (dry_run, "/etc/a.toml")),
    ]


def test_replace_registration_refused_leaves_running_service_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registration validate_registration() refuses raises before anything is stopped or registered."""
    svc = ConcreteService()

    def refuse(*, config_path: str) -> None:
        raise RuntimeError("unit cannot hold this path")

    monkeypatch.setattr(svc, "validate_registration", refuse)

    with pytest.raises(RuntimeError, match="unit cannot hold this path"):
        svc.replace_registration(config_path="/etc/a.toml")

    assert svc.calls == []


def test_pre_activate_cleanup_logs_and_swallows_a_failed_stop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stop failure must not block registering the replacement; it is logged at DEBUG with its traceback.

    DEBUG, not WARNING: systemd's stop() raises when the unit is not loaded yet, as on every fresh install.
    """
    svc = ConcreteService()
    failure = RuntimeError("launchctl binary not found")

    def failing_stop(dry_run: bool = False) -> int:
        raise failure

    monkeypatch.setattr(svc, "stop", failing_stop)
    with caplog.at_level(logging.DEBUG, logger="archon_search.platform.service"):
        svc.pre_activate_cleanup()

    [record] = [r for r in caplog.records if r.name == "archon_search.platform.service"]
    assert record.levelno == logging.DEBUG
    assert record.getMessage() == "Could not stop the running service before re-registering it"
    assert record.exc_info is not None
    assert record.exc_info[1] is failure


_WRITE_TARGET = "/srv/archon/run-server.sh"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (PermissionError("denied"), f"Permission denied writing {_WRITE_TARGET}"),
        (IsADirectoryError("is a directory"), f"Could not write {_WRITE_TARGET}"),
    ],
    ids=["permission", "other-oserror"],
)
def test_write_errors_naming_wraps_oserror_naming_the_target(error: OSError, message: str) -> None:
    """An OSError becomes a RuntimeError naming the target, chained to the original error."""
    with pytest.raises(RuntimeError) as excinfo, write_errors_naming(_WRITE_TARGET):
        raise error

    assert str(excinfo.value) == message
    assert excinfo.value.__cause__ is error


def test_write_errors_naming_passes_non_oserror_through_unchanged() -> None:
    """Only write failures are translated; any other exception propagates as raised."""
    failure = ValueError("not a write error")

    with pytest.raises(ValueError) as excinfo, write_errors_naming(_WRITE_TARGET):
        raise failure

    assert excinfo.value is failure


# ── registration checks launchd and systemd share ─────────────────────────────

_LAUNCHER = "systemd"
_LABEL = "a config path"
_CLEAN_PATH = "/x/a.toml"


@pytest.mark.parametrize("python", ["", None], ids=["empty", "none"])
def test_check_interpreter_determined_refuses_undetermined_interpreter(python: str | None) -> None:
    """sys.executable is '' or None when Python cannot determine it; the launcher would then exec nothing."""
    expected = re.escape(f"{_LAUNCHER} cannot exec an empty interpreter path: {python!r}")
    with pytest.raises(RuntimeError, match=f"^{expected}$"):
        check_interpreter_determined(_LAUNCHER, python)


def test_check_interpreter_determined_accepts_an_interpreter_path() -> None:
    check_interpreter_determined(_LAUNCHER, "/usr/bin/python3")


@pytest.mark.parametrize(
    "bad",
    [
        f" {_CLEAN_PATH}",
        f"{_CLEAN_PATH} ",
        f"\t{_CLEAN_PATH}",
        f"{_CLEAN_PATH}\t",
        f"\n{_CLEAN_PATH}",
        f"{_CLEAN_PATH}\n",
    ],
    ids=["leading-space", "trailing-space", "leading-tab", "trailing-tab", "leading-newline", "trailing-newline"],
)
def test_check_survives_server_strip_refuses_outer_whitespace(bad: str) -> None:
    expected = re.escape(f"archon-search would strip {_LABEL} with leading or trailing whitespace: {bad!r}")
    with pytest.raises(RuntimeError, match=f"^{expected}$"):
        check_survives_server_strip(_LABEL, bad)


def test_check_survives_server_strip_accepts_inner_whitespace() -> None:
    check_survives_server_strip(_LABEL, "/x/my data/a.toml")


def test_server_strips_the_env_values_the_whitespace_refusal_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract behind the whitespace refusal: the server strips ARCHON_SEARCH_CONFIG and ARCHON_SEARCH_DATA_DIR.

    If either stopped stripping, the refusal would reject paths the server could use as written.
    """
    from archon_search.config import get_default_config_path
    from archon_search.paths import get_data_dir
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", " /x/a.toml ")
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", " /x/data ")
    assert get_default_config_path() == Path("/x/a.toml")
    assert get_data_dir() == Path("/x/data")
