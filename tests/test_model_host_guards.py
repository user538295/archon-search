"""BE-9 (collection-descriptions plan, C1): the session guard keeps the default suite off real model providers.

A test that enables a feature on any provider must not reach a real one, whatever the developer's shell holds (an API key, a
running Ollama or llama-server, a logged-in ``claude``). The guard of ``tests/conftest.py`` (``tests/_model_host_guard.py``)
refuses:

* the construction of an ``openai`` or ``ollama`` SDK client unless it is aimed at a ``FakeModelServer`` or (``openai``) is bound
  to an injected in-process client (the ``anthropic`` client is covered by ``tests/test_anthropic_key_guards.py``);
* a real-transport HTTP request (``httpx`` and ``httpx2``) to a non-loopback host, or to a loopback default model-server port;
* a ``claude`` spawn from an unresolved path (a bare name left to the OS lookup), of a binary that is not a fake installed by
  ``tests/_fake_claude.py``, or of the resolved path of the real binary found on ``PATH`` when the session started.

Tests marked with a live lane marker (``live``, ``live_eval``, ...) opt out: reaching the real thing is their purpose.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import socket
import stat
import subprocess
import sys
import tomllib
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from archon_search.config import LLAMA_CPP_BASE_URL_DEFAULT, OLLAMA_BASE_URL_DEFAULT
from archon_search.llm_client.claude_cli_client import ClaudeCliClient
from archon_search.llm_client.protocol import LlmCallRequest
from tests import _fake_claude
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests._model_host_guard import LIVE_LANE_MARKERS

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is an sh launcher")

_REPLY = "reply from the fake claude"
_PROMPT = "describe this collection in one line"
_MAX_OUTPUT_TOKENS = 1024
_WINDOW = 8192
_TIMEOUT_SECONDS = 20.0
_API_KEY = "sk-test-not-a-real-key"
_EXTERNAL_HOST = "models.test.invalid"  # ``.invalid`` never resolves (RFC 6761)
_EXTERNAL_MODEL_HOST_URL = f"https://{_EXTERNAL_HOST}/v1/models"
_OLLAMA_PORT = urlsplit(OLLAMA_BASE_URL_DEFAULT).port
_LLAMA_CPP_PORT = urlsplit(LLAMA_CPP_BASE_URL_DEFAULT).port
_OLLAMA_DEFAULT_URL = f"http://127.0.0.1:{_OLLAMA_PORT}/api/tags"
_LLAMA_CPP_DEFAULT_URL = f"{LLAMA_CPP_BASE_URL_DEFAULT}/props"
_GUARD_MESSAGE = "Test suite attempted"
_HTTP_REFUSAL = f"{_GUARD_MESSAGE} a real HTTP call"
_UNRESOLVED_REFUSAL = f"{_GUARD_MESSAGE} to spawn 'claude' from an unresolved path"
_REAL_BINARY_REFUSAL = f"{_GUARD_MESSAGE} to spawn the real claude binary"
_NOT_A_FAKE_REFUSAL = f"{_GUARD_MESSAGE} to spawn a claude binary that is not a fake"
_SHELL_REFUSAL = f"{_GUARD_MESSAGE} to spawn a shell command line that names claude"
_HUB_REPO = "models-test/never-downloaded"


def _request() -> LlmCallRequest:
    return LlmCallRequest(prompt=_PROMPT, max_output_tokens=_MAX_OUTPUT_TOKENS, timeout_seconds=_TIMEOUT_SECONDS)


def _sdk(module: str) -> Any:
    """The SDK module; its client classes are read off it at call time, because the session guard replaces them."""
    return importlib.import_module(module)


def _unused_loopback_port() -> int:
    """A loopback port nothing listens on (it was free a moment ago): no ``FakeModelServer`` owns it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def fake(tmp_path: Path) -> _fake_claude.FakeClaude:
    return _fake_claude.install(tmp_path / "bin", reply=_REPLY)


def _path_with(fake: _fake_claude.FakeClaude) -> str:
    """``PATH`` with the fake ``claude`` first; the launcher is an ``sh`` script that still needs ``dirname``."""
    return f"{fake.path.parent}{os.pathsep}{os.environ['PATH']}"


# --- SDK client construction ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "constructor", "label", "kwargs"),
    [
        pytest.param("openai", "OpenAI", "OpenAI", {"api_key": _API_KEY}, id="openai-sync"),
        pytest.param("openai", "AsyncOpenAI", "OpenAI", {"api_key": _API_KEY}, id="openai-async"),
        pytest.param("ollama", "Client", "Ollama", {}, id="ollama-sync"),
        pytest.param("ollama", "AsyncClient", "Ollama", {}, id="ollama-async"),
    ],
)
def test_guard_blocks_openai_and_ollama_client_construction(
    module: str, constructor: str, label: str, kwargs: dict[str, str]
) -> None:
    with pytest.raises(RuntimeError, match=f"{_GUARD_MESSAGE} to instantiate the {label} client"):
        getattr(_sdk(module), constructor)(**kwargs)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: _sdk("ollama").AsyncClient(host="localhost"), id="ollama-default-port-by-name"),
        pytest.param(lambda: _sdk("ollama").Client(host=f"http://127.0.0.1:{_OLLAMA_PORT}"), id="ollama-explicit-default-port"),
        pytest.param(
            lambda: _sdk("ollama").Client(host=f"127.0.0.1:{_unused_loopback_port()}"),
            id="ollama-loopback-port-no-fake-server-owns",
        ),
        pytest.param(
            lambda: _sdk("openai").AsyncOpenAI(api_key=_API_KEY, base_url=f"{LLAMA_CPP_BASE_URL_DEFAULT}/v1"),
            id="openai-aimed-at-llama-server-default-port",
        ),
        pytest.param(
            lambda: _sdk("openai").OpenAI(api_key=_API_KEY, base_url=f"http://127.0.0.1:{_unused_loopback_port()}/v1"),
            id="openai-loopback-port-no-fake-server-owns",
        ),
        pytest.param(
            lambda: _sdk("openai").OpenAI(api_key=_API_KEY, base_url=f"https://{_EXTERNAL_HOST}/v1"),
            id="openai-external-host",
        ),
    ],
)
def test_guard_blocks_sdk_clients_aimed_at_a_real_model_server(build: Callable[[], object]) -> None:
    with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
        build()


@pytest.mark.parametrize("host", [_EXTERNAL_HOST, f"127.0.0.1:{_LLAMA_CPP_PORT}"])
def test_guard_blocks_sdk_clients_aimed_at_the_hosts_the_environment_names(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", host)
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://{host}/v1")

    with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
        _sdk("ollama").Client()
    with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
        _sdk("openai").OpenAI(api_key=_API_KEY)


async def test_guard_allows_sdk_clients_aimed_at_a_loopback_fake_server(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)
    with FakeModelServer(reply_text=_REPLY) as server:
        openai_client = _sdk("openai").AsyncOpenAI(api_key=_API_KEY, base_url=f"{server.base_url}/v1")
        async with _sdk("ollama").AsyncClient(host=server.base_url) as ollama_client:
            chat = await ollama_client.chat(model="fake-model", messages=[{"role": "user", "content": _PROMPT}])
        await openai_client.close()
    assert chat.message.content == _REPLY


async def test_guard_allows_an_sdk_client_aimed_at_an_unreachable_fake_port(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)
    with unreachable_port() as port:
        async with _sdk("ollama").AsyncClient(host=f"http://127.0.0.1:{port}") as client:
            with pytest.raises(ConnectionError):  # the SDK maps a refused connection onto the builtin
                await client.list()


async def test_guard_allows_an_ollama_client_bound_to_an_in_process_transport() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(HTTPStatus.OK, json={"models": []}))

    async with _sdk("ollama").AsyncClient(transport=transport) as client:  # the default host: refused without a transport
        listing = await client.list()

    assert listing.models == []


def test_guard_refuses_a_fake_server_client_once_the_server_is_gone() -> None:
    with FakeModelServer() as server:
        base_url = server.base_url
    with pytest.raises(RuntimeError, match=f"{_GUARD_MESSAGE} to instantiate the Ollama client"):
        _sdk("ollama").Client(host=base_url)


async def test_guard_allows_an_openai_client_bound_to_an_in_process_transport() -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(HTTPStatus.OK, json={})))

    client = _sdk("openai").AsyncOpenAI(api_key=_API_KEY, base_url="http://test/v1", http_client=http_client)

    assert client.base_url.host == "test"
    await client.close()
    assert http_client.is_closed


_ARBITRARY_PORT = 56789


@pytest.mark.parametrize(
    "host",
    [
        None, "", "1.2.3.4", f":{_ARBITRARY_PORT}", f"1.2.3.4:{_ARBITRARY_PORT}", "http://1.2.3.4", "https://1.2.3.4",
        "example.com", f"example.com:{_ARBITRARY_PORT}", "http://example.com/path", f"https://example.com:{_ARBITRARY_PORT}",
        "[0001:002:003:0004::1]", f"http://[0001:002:003:0004::1]:{_ARBITRARY_PORT}",
    ],
)
def test_the_guard_reads_an_ollama_host_as_the_sdk_does(host: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests._model_host_guard import _ollama_target

    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    sdk_url = urlsplit(_sdk("ollama._client")._parse_host(host))

    assert _ollama_target(host) == (sdk_url.hostname, sdk_url.port)


def test_the_guard_reads_the_ollama_environment_host_as_the_sdk_does(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests._model_host_guard import _ollama_target

    monkeypatch.setenv("OLLAMA_HOST", f"example.com:{_ARBITRARY_PORT}")
    sdk_url = urlsplit(_sdk("ollama._client")._parse_host(os.environ["OLLAMA_HOST"]))

    assert _ollama_target() == (sdk_url.hostname, sdk_url.port)


# --- HTTP calls to a model host ---------------------------------------------------------------------------------------

_BLOCKED_URLS = [
    pytest.param(_EXTERNAL_MODEL_HOST_URL, id="external-host"),
    pytest.param(f"http://{_EXTERNAL_HOST}/", id="external-host-plain-http"),
    pytest.param(_OLLAMA_DEFAULT_URL, id="ollama-default-port"),
    pytest.param(_LLAMA_CPP_DEFAULT_URL, id="llama-server-default-port"),
]


@pytest.fixture(params=["httpx", "httpx2"])
def http(request: pytest.FixtureRequest) -> ModuleType:
    """Each httpx flavour a model host is reached through: ``httpx2`` is what huggingface_hub, openai and anthropic use."""
    return importlib.import_module(request.param)


@pytest.mark.parametrize("url", _BLOCKED_URLS)
async def test_guard_blocks_async_http_calls_to_a_model_host(http: ModuleType, url: str) -> None:
    async with http.AsyncClient() as client:
        with pytest.raises(RuntimeError, match=_HTTP_REFUSAL):
            await client.get(url)


@pytest.mark.parametrize("url", _BLOCKED_URLS)
def test_guard_blocks_sync_http_calls_to_a_model_host(http: ModuleType, url: str) -> None:
    with pytest.raises(RuntimeError, match=_HTTP_REFUSAL):
        http.get(url)


def test_guard_blocks_hugging_face_hub_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A model download (what ``snapshot_download`` does before it writes a byte) never leaves the machine."""
    hub = importlib.import_module("huggingface_hub")
    monkeypatch.setattr(hub.constants, "HF_HUB_OFFLINE", False)  # read at import: an offline shell would never reach HTTP

    with pytest.raises(RuntimeError, match=_HTTP_REFUSAL):
        hub.snapshot_download(repo_id=_HUB_REPO, cache_dir=tmp_path)


async def test_guard_allows_async_http_calls_to_a_loopback_fake_server(http: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)
    with FakeModelServer(llama_n_ctx=_WINDOW) as server:
        async with http.AsyncClient() as client:
            response = await client.get(f"{server.base_url}/props")

    assert response.json()["default_generation_settings"]["n_ctx"] == _WINDOW


def test_guard_allows_sync_http_calls_to_a_loopback_fake_server(http: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)
    with FakeModelServer(llama_n_ctx=_WINDOW) as server:
        response = http.get(f"{server.base_url}/props")

    assert response.json()["default_generation_settings"]["n_ctx"] == _WINDOW


async def test_guard_leaves_in_process_http_transports_alone(http: ModuleType) -> None:
    transport = http.MockTransport(lambda request: http.Response(HTTPStatus.OK, text=str(request.url)))

    async with http.AsyncClient(transport=transport) as client:
        response = await client.get(_EXTERNAL_MODEL_HOST_URL)

    assert response.text == _EXTERNAL_MODEL_HOST_URL


# --- claude spawns ----------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_standing_in_for_the_real_claude(fake: _fake_claude.FakeClaude, monkeypatch: pytest.MonkeyPatch) -> _fake_claude.FakeClaude:
    """The fake, declared to be the real binary found on ``PATH`` when the session started."""
    from tests import _model_host_guard

    monkeypatch.setattr(_model_host_guard, "REAL_CLAUDE_BINARIES", frozenset({os.path.realpath(fake.path)}))
    return fake


@pytest.fixture
def unregistered_claude(tmp_path: Path) -> Path:
    """An executable named ``claude`` that ``tests/_fake_claude.py`` did not install: what an SDK bundles, say."""
    binary = tmp_path / "_bundled" / "claude"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


@posix_only
async def test_guard_blocks_claude_spawn_for_unresolved_binary(
    fake: _fake_claude.FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", _path_with(fake))  # the OS would find the fake ``claude`` by its bare name

    with pytest.raises(RuntimeError, match=_UNRESOLVED_REFUSAL):
        await asyncio.create_subprocess_exec("claude", "-p")

    assert fake.invocations() == []


@posix_only
@pytest.mark.parametrize("spawn", [subprocess.run, subprocess.Popen], ids=["run", "popen"])
def test_guard_blocks_subprocess_spawn_for_unresolved_binary(
    spawn: Callable[..., object], fake: _fake_claude.FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", _path_with(fake))

    with pytest.raises(RuntimeError, match=_UNRESOLVED_REFUSAL):
        spawn(["claude", "-p"])

    assert fake.invocations() == []


@posix_only
async def test_guard_blocks_a_claude_binary_that_is_not_an_installed_fake(unregistered_claude: Path) -> None:
    with pytest.raises(RuntimeError, match=_NOT_A_FAKE_REFUSAL):
        await asyncio.create_subprocess_exec(str(unregistered_claude), "-p")
    with pytest.raises(RuntimeError, match=_NOT_A_FAKE_REFUSAL):
        await ClaudeCliClient(model="", binary=str(unregistered_claude)).call(_request())


@posix_only
@pytest.mark.parametrize(
    "command",
    ["claude -p", "CLAUDE -p", "'/opt/tools/claude' -p", "exec claude -p", "cd /tmp && claude -p", "env FOO=1 claude.exe -p"],
)
def test_guard_blocks_shell_command_lines_that_name_claude(command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no claude, real or not, is reachable even if the guard regressed
    with pytest.raises(RuntimeError, match=_SHELL_REFUSAL):
        subprocess.run(command, shell=True, check=False)
    with pytest.raises(RuntimeError, match=_SHELL_REFUSAL):
        subprocess.run([command], shell=True, executable="/bin/sh", check=False)


@posix_only
def test_guard_leaves_shell_command_lines_that_only_resemble_claude_alone() -> None:
    completed = subprocess.run("echo claude_cli claude-proxy ~/.claude", shell=True, capture_output=True, text=True, check=False)

    assert completed.returncode == 0
    assert "claude-proxy" in completed.stdout


@posix_only
async def test_guard_blocks_the_real_claude_binary_by_resolved_path(
    fake_standing_in_for_the_real_claude: _fake_claude.FakeClaude,
) -> None:
    real = fake_standing_in_for_the_real_claude

    with pytest.raises(RuntimeError, match=_REAL_BINARY_REFUSAL):
        await asyncio.create_subprocess_exec(str(real.path), "-p")
    with pytest.raises(RuntimeError, match=_REAL_BINARY_REFUSAL):
        await ClaudeCliClient(model="", binary=str(real.path)).call(_request())

    assert real.invocations() == []


@posix_only
def test_guard_blocks_the_real_claude_binary_under_another_name(
    fake_standing_in_for_the_real_claude: _fake_claude.FakeClaude, tmp_path: Path
) -> None:
    real = fake_standing_in_for_the_real_claude
    renamed_link = tmp_path / "not-called-claude"
    renamed_link.symlink_to(real.path)

    with pytest.raises(RuntimeError, match=_REAL_BINARY_REFUSAL):
        subprocess.run([str(renamed_link), "-p"], check=False)

    assert real.invocations() == []


@posix_only
def test_guard_resolves_a_relative_claude_path_against_the_spawn_directory(
    fake_standing_in_for_the_real_claude: _fake_claude.FakeClaude,
) -> None:
    real = fake_standing_in_for_the_real_claude

    with pytest.raises(RuntimeError, match=_REAL_BINARY_REFUSAL):
        subprocess.run(["./claude", "-p"], cwd=real.path.parent, check=False)

    assert real.invocations() == []


@posix_only
async def test_guard_allows_fake_claude_on_resolved_path(fake: _fake_claude.FakeClaude) -> None:
    result = await ClaudeCliClient(model="", binary=str(fake.path)).call(_request())

    assert result.text == _REPLY
    assert [invocation.stdin for invocation in fake.invocations()] == [_PROMPT]


@posix_only
def test_guard_allows_fake_claude_spawned_through_subprocess(fake: _fake_claude.FakeClaude) -> None:
    completed = subprocess.run([str(fake.path), "-p"], input=_PROMPT, capture_output=True, text=True, check=True)

    assert completed.stdout == _REPLY


@posix_only
def test_guard_allows_a_relative_path_to_the_fake_claude(fake: _fake_claude.FakeClaude) -> None:
    completed = subprocess.run(["./claude", "-p"], cwd=fake.path.parent, input=_PROMPT, capture_output=True, text=True, check=True)

    assert completed.stdout == _REPLY


@posix_only
async def test_guard_allows_a_fake_claude_that_the_client_resolves_through_path(
    fake: _fake_claude.FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", _path_with(fake))

    client = ClaudeCliClient(model="")
    result = await client.call(_request())

    assert client.binary == str(fake.path)
    assert result.text == _REPLY


async def test_guard_leaves_other_asyncio_spawns_alone() -> None:
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await process.wait()

    assert process.returncode == 0


def test_guard_leaves_other_subprocess_spawns_alone() -> None:
    assert subprocess.run([sys.executable, "-c", "pass"], check=False).returncode == 0


def test_guard_lets_a_malformed_spawn_fail_with_its_own_error() -> None:
    with pytest.raises(TypeError):
        subprocess.Popen()  # type: ignore[call-overload]


# --- live lanes --------------------------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_lane_marker_suspends_the_guards() -> None:
    client = _sdk("ollama").Client()  # the default host: refused in every other test of this file

    assert isinstance(client, _sdk("ollama").Client)
    client.close()


# --- the live lane markers ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("marker", sorted(LIVE_LANE_MARKERS))
def test_a_live_lane_marker_lifts_every_door(marker: str, unregistered_claude: Path) -> None:
    from tests import _model_host_guard

    _model_host_guard.suspend_for_markers(["integration", marker])
    try:
        _sdk("ollama").Client().close()  # the default host: an SDK client
        _model_host_guard._refuse_request(httpx.Request("GET", _EXTERNAL_MODEL_HOST_URL))  # a model host: HTTP
        assert subprocess.run([str(unregistered_claude)], check=False).returncode == 0  # a claude binary: a spawn
    finally:
        _model_host_guard.set_suspended(False)


def test_other_markers_leave_the_guards_on() -> None:
    from tests import _model_host_guard

    _model_host_guard.suspend_for_markers(["integration", "unit"])

    with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
        _sdk("ollama").Client()


def test_every_lane_the_default_run_excludes_is_a_live_lane_marker() -> None:
    addopts = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = set(re.findall(r"not (\w+)", addopts["tool"]["pytest"]["ini_options"]["addopts"]))

    assert excluded <= LIVE_LANE_MARKERS
