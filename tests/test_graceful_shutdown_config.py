"""L4: uvicorn is given a graceful-shutdown budget so an in-flight ingest is not killed mid-write.

Without ``timeout_graceful_shutdown`` uvicorn waits for open connections forever, and Docker's SIGKILL
(after ``docker stop``'s 30 s grace) then cuts an ingest mid-write. The knob is ``[server].graceful_shutdown_seconds``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import ConfigError, SearchConfig, load_config
from archon_search.constants import DEFAULT_GRACEFUL_SHUTDOWN_SECONDS
from archon_search.server import app as app_module

#: ``docker stop`` waits this long before SIGKILL; the budget must end before it.
_DOCKER_STOP_GRACE_SECONDS = 30


def test_the_default_budget_ends_before_dockers_stop_grace_period() -> None:
    assert 0 < DEFAULT_GRACEFUL_SHUTDOWN_SECONDS < _DOCKER_STOP_GRACE_SECONDS
    assert SearchConfig().graceful_shutdown_seconds == DEFAULT_GRACEFUL_SHUTDOWN_SECONDS


def test_the_budget_is_read_from_the_server_table(tmp_path: Path) -> None:
    path = tmp_path / "archon-search.toml"
    path.write_text("[server]\ngraceful_shutdown_seconds = 12\n", encoding="utf-8")

    assert load_config(path).graceful_shutdown_seconds == 12


@pytest.mark.parametrize("value", ["0", "-1", '"soon"', "1.5"])
def test_an_invalid_budget_is_refused(tmp_path: Path, value: str) -> None:
    path = tmp_path / "archon-search.toml"
    path.write_text(f"[server]\ngraceful_shutdown_seconds = {value}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="graceful_shutdown_seconds"):
        load_config(path)


def test_run_server_hands_the_budget_to_uvicorn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(tmp_path / "archon-search.toml"))
    monkeypatch.setattr(app_module.uvicorn, "run", lambda _app, **kwargs: captured.update(kwargs))
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.graceful_shutdown_seconds = 17

    app_module.run_server(config)

    assert captured["timeout_graceful_shutdown"] == 17
