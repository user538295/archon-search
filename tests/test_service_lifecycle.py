"""Tests for SearchServiceLifecycle ABC."""
from __future__ import annotations

import pytest

from archon_search.platform.service import SearchServiceLifecycle, ServiceStatus


class ConcreteService(SearchServiceLifecycle):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def status(self) -> ServiceStatus:
        return ServiceStatus(running=True, pid=1, uptime_seconds=0.0)

    def register(self) -> None:
        self.calls.append("register")

    def unregister(self) -> None:
        self.calls.append("unregister")


def test_abc_cannot_instantiate() -> None:
    with pytest.raises(TypeError):
        SearchServiceLifecycle()  # type: ignore[abstract]


def test_restart_default_calls_stop_then_start() -> None:
    svc = ConcreteService()
    svc.restart()
    assert svc.calls == ["stop", "start"]
