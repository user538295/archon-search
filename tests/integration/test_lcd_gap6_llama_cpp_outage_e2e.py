"""One outage of a ``llama_cpp`` server is one WARNING per server and process (S8, S34, S51).

The context-window lookup of every ``llama_cpp`` feature (S8) and the description startup probe (S34) both ask the same
server the same question at boot, so a down server used to be logged twice: once as "cannot read the context window ...
assuming 4096 tokens" and once as "Collection descriptions: provider llama_cpp is not reachable". The two findings are now
one WARNING per server that says both things, whichever of the two paths reports first. Every test runs the real app
(lifespan, real LanceDB) against a port nothing answers on and reads what an operator reads: the log and ``GET /status``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.constants import ASSUMED_CONTEXT_TOKENS
from tests._fake_model_server import keep_loopback_out_of_proxies, unreachable_port
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_LOOPBACK = "127.0.0.1"
_MODEL = "outage-model"
_WAIT_SECONDS = 60.0
_POLL_SECONDS = 0.02
_HTTP_OK = 200
_STARTUP_TASKS = ("_llm_window_task", "_description_probe_task")


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _section(name: str, server: str, *, extra: str = "") -> str:
    return (
        f"[{name}]\nenabled = true\nprovider = \"llama_cpp\"\nmodel = \"{_MODEL}\"\n"
        f"llama_cpp_base_url = \"http://{server}\"\n{extra}"
    )


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"{what} did not happen within {_WAIT_SECONDS}s"
        time.sleep(_POLL_SECONDS)


def _boot_and_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str, *, after_boot: Callable[[TestClient, str], None] | None = None
) -> None:
    """Boot the app, let the window lookup and the probe end, optionally inspect the live app, shut down."""
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _config, admin_key):
        for name in _STARTUP_TASKS:
            task = getattr(client.app.state, name, None)
            if task is not None:
                _wait_for(task.done, f"the {name} to end")
        _wait_for(lambda: client.get("/ready").status_code == _HTTP_OK, "readiness")
        if after_boot is not None:
            after_boot(client, admin_key)


def _warnings_naming(caplog: pytest.LogCaptureFixture, server: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and server in r.getMessage()]


def _description_block(client: TestClient, admin_key: str) -> dict[str, Any]:
    response = client.get("/status", headers={"Authorization": f"Bearer {admin_key}"})
    assert response.status_code == _HTTP_OK, response.text
    block = response.json()["description"]
    assert isinstance(block, dict)
    return block


def test_e2e_a_down_description_server_is_one_warning_that_says_unreachable_and_assumed_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    blocks: list[dict[str, Any]] = []
    with unreachable_port() as port, caplog.at_level(logging.INFO):
        server = f"{_LOOPBACK}:{port}"
        _boot_and_settle(
            tmp_path,
            monkeypatch,
            _section("description", server),
            after_boot=lambda client, key: blocks.append(_description_block(client, key)),
        )

    [message] = _warnings_naming(caplog, server)
    assert "not reachable" in message
    assert str(ASSUMED_CONTEXT_TOKENS) in message
    [block] = blocks
    assert block["reachable"] is False
    assert (block["effective_context_tokens"], block["context_window_source"]) == (ASSUMED_CONTEXT_TOKENS, "assumed")


def test_e2e_three_features_on_one_down_server_are_still_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with unreachable_port() as port, caplog.at_level(logging.INFO):
        server = f"{_LOOPBACK}:{port}"
        toml = "".join(_section(name, server) for name in ("hyde", "rag_fusion", "description"))
        _boot_and_settle(tmp_path, monkeypatch, toml)

    [message] = _warnings_naming(caplog, server)
    assert "not reachable" in message
    assert str(ASSUMED_CONTEXT_TOKENS) in message


def test_e2e_two_down_servers_are_one_warning_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with ExitStack() as stack, caplog.at_level(logging.INFO):
        servers = [f"{_LOOPBACK}:{stack.enter_context(unreachable_port())}" for _ in range(2)]
        toml = _section("hyde", servers[0]) + _section("description", servers[1])
        _boot_and_settle(tmp_path, monkeypatch, toml)

    for server in servers:
        [message] = _warnings_naming(caplog, server)
        assert "not reachable" in message, server
        assert str(ASSUMED_CONTEXT_TOKENS) in message, server
