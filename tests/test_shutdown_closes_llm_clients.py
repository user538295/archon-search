"""Shutdown closes the HyDE, RAG Fusion, graph and description clients, each once, before the stores disconnect."""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app

_CLIENT_STATES = ("hyde_client", "rag_fusion_client", "graph_client", "description_client")


class _ClosableClient:
    """A client with an ``aclose`` that logs its close into ``events``; ``failure`` makes it raise."""

    def __init__(self, name: str, events: list[str], failure: Exception | None = None) -> None:
        self.name = name
        self.events = events
        self.failure = failure
        self.closes = 0

    async def aclose(self) -> None:
        self.closes += 1
        self.events.append(f"close:{self.name}")
        if self.failure is not None:
            raise self.failure


def _boot_and_shut_down(
    tmp_path: Path, failing: str | None
) -> tuple[dict[str, _ClosableClient], list[str]]:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, JobStore(path=tmp_path / "jobs.json"))
    events: list[str] = []
    spies = {
        name: _ClosableClient(name, events, RuntimeError("close failed") if name == failing else None)
        for name in _CLIENT_STATES
    }
    with TestClient(app):
        for name, spy in spies.items():
            setattr(app.state, name, spy)
        store = app.state.search_store
        real_disconnect = store.disconnect

        async def disconnect() -> None:
            events.append("disconnect:search_store")
            await real_disconnect()

        store.disconnect = disconnect
    return spies, events


def test_shutdown_closes_every_shared_client_once_before_the_store_disconnects(tmp_path: Path) -> None:
    spies, events = _boot_and_shut_down(tmp_path, failing=None)

    assert {name: spy.closes for name, spy in spies.items()} == dict.fromkeys(_CLIENT_STATES, 1)
    assert events.index("disconnect:search_store") == len(_CLIENT_STATES)


@pytest.mark.parametrize("failing", _CLIENT_STATES)
def test_a_client_whose_close_raises_does_not_stop_the_others(tmp_path: Path, failing: str) -> None:
    spies, events = _boot_and_shut_down(tmp_path, failing=failing)

    assert {name: spy.closes for name, spy in spies.items()} == dict.fromkeys(_CLIENT_STATES, 1)
    assert events[-1] == "disconnect:search_store"
