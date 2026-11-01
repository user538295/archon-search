"""One outage of a ``llama_cpp`` server is one WARNING per server and process (S8, S34, S51).

Every path that asks a ``llama_cpp`` server whether it is up at boot reports a down one through the same WARNING, keyed on the
server's ``host:port`` label: the context-window lookup of every ``llama_cpp`` feature (S8), the startup reachability probe of
``validate_models_async`` (``GET /v1/models``, S7) and the description startup probe (S34). The one line says both
consequences, whichever path reports first. Every test runs the real app (lifespan, real LanceDB) against a port nothing
answers on (or a live stand-in server for the "other" server) and reads what an operator reads: the log and ``GET /status``.

The ``[graph]`` cases run the real ``_probe_extraction_model`` and the real ``warn_once`` with the extraction engine stubbed
(``install_graph_engine_stub``), as the other graph e2e tests do.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.constants import ASSUMED_CONTEXT_TOKENS
from archon_search.model_validation import EXTRACTION_MODEL_PROBE_ERROR_WARNING
from tests._fake_model_server import keep_loopback_out_of_proxies, unreachable_port
from tests._graph_engine_stub import install_graph_engine_stub
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_LOOPBACK = "127.0.0.1"
_MODEL = "outage-model"
_WAIT_SECONDS = 60.0
_POLL_SECONDS = 0.02
_HTTP_OK = 200
_STARTUP_TASKS = ("_llm_window_task", "_description_probe_task")
_UNREACHABLE = "is not reachable"
_LIVE_WINDOW = 8192
_SHARED_LOGGER = "archon_search.llm_client.llama_cpp_client"
_CHAT_PATH = "/v1/chat/completions"


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


class _LiveLlamaServer(ThreadingHTTPServer):
    """A llama-server stand-in that is up: ``/v1/models``, ``/props`` and a chat answer, recording every request path."""

    def __init__(self) -> None:
        super().__init__((_LOOPBACK, 0), _LiveLlamaHandler)
        self.paths: list[str] = []

    @property
    def address(self) -> str:
        return f"{_LOOPBACK}:{self.server_address[1]}"


class _LiveLlamaHandler(BaseHTTPRequestHandler):
    server: _LiveLlamaServer

    def _answer(self, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(_HTTP_OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self.server.paths.append(self.path)
        if self.path == "/props":
            self._answer({"default_generation_settings": {"n_ctx": _LIVE_WINDOW}})
        else:
            self._answer({"data": [{"id": _MODEL}]})

    def do_POST(self) -> None:
        self.server.paths.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._answer({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]})

    def log_message(self, format: str, *args: object) -> None:
        """Silence the per-request stderr line."""


@contextmanager
def _live_llama_server() -> Iterator[_LiveLlamaServer]:
    server = _LiveLlamaServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _section(name: str, server: str, *, enabled: bool = True, extra: str = "") -> str:
    return (
        f"[{name}]\nenabled = {str(enabled).lower()}\nprovider = \"llama_cpp\"\nmodel = \"{_MODEL}\"\n"
        f"llama_cpp_base_url = \"http://{server}\"\n{extra}"
    )


def _graph_section(server: str) -> str:
    return f'[graph]\nenabled = true\nprovider = "llama_cpp"\nextraction_model = "{_MODEL}"\nllama_cpp_base_url = "http://{server}"\n'


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


def _status(client: TestClient, admin_key: str) -> dict[str, Any]:
    response = client.get("/status", headers={"Authorization": f"Bearer {admin_key}"})
    assert response.status_code == _HTTP_OK, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _description_block(client: TestClient, admin_key: str) -> dict[str, Any]:
    block = _status(client, admin_key)["description"]
    assert isinstance(block, dict)
    return block


def _assert_the_one_shared_line(message: str, server: str) -> None:
    assert message.startswith(f"llama_cpp: the server at {server} is not reachable (")
    assert "requests to it fail until it answers" in message
    assert f"where its context window could not be read, {ASSUMED_CONTEXT_TOKENS} tokens are assumed" in message


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
    _assert_the_one_shared_line(message, server)
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
    _assert_the_one_shared_line(message, server)


def test_e2e_two_down_servers_are_one_warning_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with ExitStack() as stack, caplog.at_level(logging.INFO):
        servers = [f"{_LOOPBACK}:{stack.enter_context(unreachable_port())}" for _ in range(2)]
        toml = _section("hyde", servers[0]) + _section("description", servers[1])
        _boot_and_settle(tmp_path, monkeypatch, toml)

    for server in servers:
        [message] = _warnings_naming(caplog, server)
        _assert_the_one_shared_line(message, server)


def test_e2e_one_down_server_spelled_two_ways_is_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``HTTP://127.0.0.1:<port>/v1/`` and ``http://127.0.0.1:<port>`` normalise to one ``host:port`` label."""
    with unreachable_port() as port, caplog.at_level(logging.INFO):
        server = f"{_LOOPBACK}:{port}"
        toml = _section("hyde", server) + (
            f'[description]\nenabled = true\nprovider = "llama_cpp"\nmodel = "{_MODEL}"\n'
            f'llama_cpp_base_url = "HTTP://{server}/v1/"\n'
        )
        _boot_and_settle(tmp_path, monkeypatch, toml)

    [message] = _warnings_naming(caplog, server)
    _assert_the_one_shared_line(message, server)
    [record] = [r for r in caplog.records if r.levelno >= logging.WARNING and server in r.getMessage()]
    assert record.name == _SHARED_LOGGER


def test_e2e_a_disabled_llama_cpp_feature_names_no_server_and_reports_no_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``[hyde]`` that is off asks nobody: no lookup, no probe, no WARNING, and ``llama_cpp_ok`` stays ``null``."""
    states: list[object] = []
    with unreachable_port() as port, caplog.at_level(logging.INFO):
        server = f"{_LOOPBACK}:{port}"
        _boot_and_settle(
            tmp_path,
            monkeypatch,
            _section("hyde", server, enabled=False),
            after_boot=lambda client, key: states.append(_status(client, key)["model_validation"]["llama_cpp_ok"]),
        )

    assert _warnings_naming(caplog, server) == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING and _UNREACHABLE in r.getMessage()] == []
    assert states == [None]


def test_e2e_a_down_graph_server_is_one_warning_and_the_graph_probe_adds_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``[graph]`` on a down llama-server: the window lookup or the startup probe reports it once; the extraction probe
    (real ``_probe_extraction_model``) does not call the dead server, so no second WARNING and no traceback."""
    install_graph_engine_stub(monkeypatch)
    flags: list[object] = []
    with unreachable_port() as port, caplog.at_level(logging.DEBUG):
        server = f"{_LOOPBACK}:{port}"
        _boot_and_settle(
            tmp_path,
            monkeypatch,
            _graph_section(server),
            after_boot=lambda client, key: flags.append(_status(client, key)["model_validation"]),
        )

    [message] = _warnings_naming(caplog, server)
    _assert_the_one_shared_line(message, server)
    [validation] = flags
    assert isinstance(validation, dict)
    assert validation["llama_cpp_ok"] is False
    assert EXTRACTION_MODEL_PROBE_ERROR_WARNING not in validation["provider_warnings"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING and r.exc_info] == []


def test_e2e_a_down_hyde_server_and_a_live_graph_server_log_one_warning_and_probe_the_graph_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The flag reflects every server (hyde's is down), yet the live graph server is still probed by the extraction probe."""
    install_graph_engine_stub(monkeypatch)
    flags: list[object] = []
    with unreachable_port() as port, _live_llama_server() as live, caplog.at_level(logging.INFO):
        down = f"{_LOOPBACK}:{port}"
        _boot_and_settle(
            tmp_path,
            monkeypatch,
            _section("hyde", down) + _graph_section(live.address),
            after_boot=lambda client, key: flags.append(_status(client, key)["model_validation"]),
        )

    [message] = _warnings_naming(caplog, down)
    _assert_the_one_shared_line(message, down)
    assert _warnings_naming(caplog, live.address) == []
    [validation] = flags
    assert isinstance(validation, dict)
    assert validation["llama_cpp_ok"] is False
    assert _CHAT_PATH in live.paths  # the extraction probe's one call reached the live graph server
    assert EXTRACTION_MODEL_PROBE_ERROR_WARNING not in validation["provider_warnings"]


def test_e2e_a_live_hyde_server_and_a_down_graph_server_log_exactly_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Two servers, only the graph's is down: its one WARNING, the other server stays silent, and the extraction probe does
    not call the dead one (which would log a second WARNING with a traceback)."""
    install_graph_engine_stub(monkeypatch)
    flags: list[object] = []
    with unreachable_port() as port, _live_llama_server() as live, caplog.at_level(logging.DEBUG):
        down = f"{_LOOPBACK}:{port}"
        _boot_and_settle(
            tmp_path,
            monkeypatch,
            _section("hyde", live.address) + _graph_section(down),
            after_boot=lambda client, key: flags.append(_status(client, key)["model_validation"]),
        )

    [message] = _warnings_naming(caplog, down)
    _assert_the_one_shared_line(message, down)
    assert _warnings_naming(caplog, live.address) == []
    [validation] = flags
    assert isinstance(validation, dict)
    assert validation["llama_cpp_ok"] is False
    assert [r for r in caplog.records if r.levelno >= logging.WARNING and r.exc_info] == []
    assert EXTRACTION_MODEL_PROBE_ERROR_WARNING not in validation["provider_warnings"]
