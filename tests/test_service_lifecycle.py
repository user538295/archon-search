"""Tests for SearchServiceLifecycle ABC."""
from __future__ import annotations

import pytest

from archon_search.platform.service import SearchServiceLifecycle, ServiceStatus


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

    def register(self, dry_run: bool = False) -> None:
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
