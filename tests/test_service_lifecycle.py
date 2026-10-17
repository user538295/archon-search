"""Tests for SearchServiceLifecycle ABC."""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.platform.service import (
    SearchServiceLifecycle,
    ServiceStatus,
    get_secrets_env_file,
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
