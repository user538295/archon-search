"""BE-25 (collection-descriptions plan, C5, S5, S17, S19, S35, S67, S85): the server builds, hosts and drains the description lifecycle.

A real app (``make_real_app``) boots with ``[description]`` on a loopback fake model server. ``create_app`` builds the
client, the lifecycle and the embedder cache it writes with (the lifecycle goes to the pipeline and to the MCP app); the
lifespan resolves the client's window and, at shutdown, closes the lifecycle before the other background tasks are
cancelled and drains it before the stores disconnect. With the feature off nothing is built.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.constants import (
    ANSWER_RESERVE_TOKENS,
    ASSUMED_CONTEXT_TOKENS,
    DEFAULT_NAMESPACE,
)
from archon_search.description_generator import DescriptionGenerator, DescriptionOutcome
from archon_search.description_lifecycle import (
    GENERATION_TASK_NAME_PREFIX,
    DescriptionLifecycle,
    GenerationKind,
    RequestOutcome,
)
from archon_search.llm_client.protocol import LlmCallRequest, LlmClient
from archon_search.server import llm_composition
from archon_search.store import SearchStore
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "wired-collection"
_DESCRIPTION_MODEL = "description-model"
_HYDE_MODEL = "hyde-model"
_GRAPH_MODEL = "graph-model"
_DESCRIPTION_REPLY = "A short collection about description wiring."
_HYDE_REPLY = "hyde reply"
_GRAPH_REPLY = "graph reply"
_CONFIGURED_WINDOW = 8192
_PROMPT = "write a short passage"
_TIMEOUT_SECONDS = 10.0
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_DOCUMENT_REPEATS = 20
_COMPOSITION_LOGGER = llm_composition.__name__

Boot = Callable[[str], tuple[TestClient, SearchConfig, str]]


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests."""
    keep_loopback_out_of_proxies(monkeypatch)


@pytest.fixture
def dead_url() -> Iterator[str]:
    """A loopback URL nothing listens on: the provider of a test that never reaches it."""
    with unreachable_port() as port:
        yield f"http://127.0.0.1:{port}"


def _description_toml(
    *, base_url: str, enabled: bool = True, context: int | None = _CONFIGURED_WINDOW, model: str = _DESCRIPTION_MODEL
) -> str:
    lines = ["[description]", f"enabled = {str(enabled).lower()}", 'provider = "ollama"', f'model = "{model}"', f'ollama_base_url = "{base_url}"']
    if context is not None:
        lines.append(f"ollama_context_tokens = {context}")
    return "\n".join(lines) + "\n"


def _await_window_resolution(client: TestClient) -> None:
    """Block until the lifespan's background window resolution has finished (it runs on the portal thread's loop)."""
    task = client.app.state._llm_window_task
    deadline = time.monotonic() + _WAIT_SECONDS
    while not task.done():
        if time.monotonic() > deadline:
            raise TimeoutError(f"context windows were not resolved within {_WAIT_SECONDS}s")
        time.sleep(_POLL_SECONDS)


@pytest.fixture
def boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Boot]:
    """Boot a real app on a TOML, wait for the window resolution, and close the app at the end of the test."""
    with contextlib.ExitStack() as stack:

        def _boot(toml: str) -> tuple[TestClient, SearchConfig, str]:
            client, config, api_key = stack.enter_context(make_real_app(tmp_path, monkeypatch, toml_content=toml))
            _await_window_resolution(client)
            return client, config, api_key

        yield _boot


def _write_document(tmp_path: Path) -> Path:
    document = tmp_path / "corpus" / "notes.txt"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text("Archon search keeps collections of local documents searchable.\n" * _DOCUMENT_REPEATS, encoding="utf-8")
    return document


def _ingest_corpus(client: TestClient, tmp_path: Path, api_key: str) -> None:
    """Ingest the corpus DIRECTORY: only a batch ingest finalises the collection and asks for a description."""
    ingest_file_via_path(client, _COLLECTION, str(_write_document(tmp_path).parent), api_key=api_key)


def _call(client: LlmClient) -> str:
    request = LlmCallRequest(prompt=_PROMPT, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=_TIMEOUT_SECONDS)
    return asyncio.run(client.call(request)).text


def _wait_for(condition: Callable[[], bool], what: str, timeout: float = _WAIT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} did not happen within {timeout}s")
        time.sleep(_POLL_SECONDS)


def _generation_tasks(client: TestClient) -> list[str]:
    async def names() -> list[str]:
        return [task.get_name() for task in asyncio.all_tasks() if task.get_name().startswith(GENERATION_TASK_NAME_PREFIX)]

    return client.portal.call(names)  # type: ignore[union-attr,return-value]


# ---------------------------------------------------------------------------
# S19: disabled means nothing is built and nothing is sent
# ---------------------------------------------------------------------------


def test_disabled_description_sends_nothing_and_starts_no_task(boot: Boot, tmp_path: Path) -> None:
    """The default (off) builds no client, no lifecycle and no task; an ingest touches no provider."""
    with FakeModelServer(reply_text=_DESCRIPTION_REPLY) as server:
        client, _config, api_key = boot(_description_toml(base_url=server.base_url, enabled=False))
        _ingest_corpus(client, tmp_path, api_key)
        state = client.app.state

        assert state.description_client is None
        assert state.description_lifecycle is None
        assert state.pipeline._description_lifecycle is None
        assert _generation_tasks(client) == []

    assert server.requests == []


# ---------------------------------------------------------------------------
# Enabled: the lifecycle is built, shared, and generates after an ingest (S17)
# ---------------------------------------------------------------------------


def test_shutdown_runs_close_then_cancel_then_drain_then_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dead_url: str
) -> None:
    """Phase order of C5/S35/S85: a background task is cancelled after ``close`` and before ``drain``, and the store
    disconnects last."""
    order: list[str] = []
    real_close, real_drain, real_disconnect = DescriptionLifecycle.close, DescriptionLifecycle.drain, SearchStore.disconnect

    def recording_close(self: DescriptionLifecycle) -> None:
        if "drain" not in order:  # ``drain`` closes the service again itself; only the shutdown's own ``close`` call counts
            order.append("close")
        real_close(self)

    async def recording_drain(self: DescriptionLifecycle) -> None:
        order.append("drain")
        await real_drain(self)

    async def recording_disconnect(self: SearchStore) -> None:
        order.append("disconnect")
        await real_disconnect(self)

    monkeypatch.setattr(DescriptionLifecycle, "close", recording_close)
    monkeypatch.setattr(DescriptionLifecycle, "drain", recording_drain)
    monkeypatch.setattr(SearchStore, "disconnect", recording_disconnect)
    with make_real_app(tmp_path, monkeypatch, toml_content=_description_toml(base_url=dead_url)) as (client, _config, _api_key):

        async def register_parked_task() -> None:
            async def parked() -> None:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    order.append("background_task_cancelled")
                    raise

            client.app.state._background_tasks.add(asyncio.create_task(parked()))
            await asyncio.sleep(0)  # let it reach its wait

        client.portal.call(register_parked_task)  # type: ignore[union-attr]

    assert order == ["close", "background_task_cancelled", "drain", "disconnect"]


def test_enabled_description_builds_one_lifecycle_for_the_pipeline_and_state(boot: Boot, dead_url: str) -> None:
    client, _config, _api_key = boot(_description_toml(base_url=dead_url))
    state = client.app.state

    assert isinstance(state.description_lifecycle, DescriptionLifecycle)
    assert state.pipeline._description_lifecycle is state.description_lifecycle
    assert state.description_client is not None
    assert state.description_lifecycle._writer._embedders is state.embedder_cache


def test_enabled_description_is_generated_in_the_background_after_an_ingest(boot: Boot, tmp_path: Path) -> None:
    """S17: after the ingest the description appears as ``generated`` / ``ok`` (the ingest returning before the model
    answers is pinned by ``tests/pipeline/test_ingest_description_request.py``)."""
    with FakeModelServer(reply_text=_DESCRIPTION_REPLY) as server:
        client, _config, api_key = boot(_description_toml(base_url=server.base_url))
        _ingest_corpus(client, tmp_path, api_key)

        def description_written() -> bool:
            meta = client.portal.call(client.app.state.search_store.get_collection_meta, _COLLECTION)  # type: ignore[union-attr]
            return meta is not None and meta.description == _DESCRIPTION_REPLY

        _wait_for(description_written, "the generated description")
        meta = client.portal.call(client.app.state.search_store.get_collection_meta, _COLLECTION)  # type: ignore[union-attr]

    assert meta is not None
    assert (meta.description_source, meta.description_status) == ("generated", "ok")
    [chat] = server.requests_to("/api/chat")
    assert chat.body is not None
    assert chat.body["model"] == _DESCRIPTION_MODEL
    assert chat.body["options"]["num_ctx"] == _CONFIGURED_WINDOW


# ---------------------------------------------------------------------------
# S67: the unset Ollama window WARNING covers the description feature
# ---------------------------------------------------------------------------


def test_unset_description_ollama_window_warns_naming_its_own_section(boot: Boot, caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer() as server, caplog.at_level(logging.WARNING):
        boot(_description_toml(base_url=server.base_url, context=None))

    [warning] = [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == _COMPOSITION_LOGGER]
    message = warning.getMessage()
    assert "[description].ollama_context_tokens" in message
    assert str(ASSUMED_CONTEXT_TOKENS) in message


def test_set_description_ollama_window_warns_nothing(boot: Boot, caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer(ollama_context_length=_CONFIGURED_WINDOW * 2) as server, caplog.at_level(logging.WARNING):
        boot(_description_toml(base_url=server.base_url))

    assert [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == _COMPOSITION_LOGGER] == []


# ---------------------------------------------------------------------------
# S5: each feature keeps its own provider, model and host (the description arm)
# ---------------------------------------------------------------------------


def test_description_feature_uses_its_own_provider_model_and_host(boot: Boot) -> None:
    """HyDE and description on Ollama (two hosts, two models), graph on llama_cpp: each call reaches only its own server."""
    with (
        FakeModelServer(reply_text=_HYDE_REPLY) as hyde_host,
        FakeModelServer(reply_text=_DESCRIPTION_REPLY) as description_host,
        FakeModelServer(reply_text=_GRAPH_REPLY) as graph_host,
    ):
        toml = (
            f'[hyde]\nenabled = true\nprovider = "ollama"\nmodel = "{_HYDE_MODEL}"\nollama_base_url = "{hyde_host.base_url}"\n'
            f"ollama_context_tokens = {_CONFIGURED_WINDOW}\n"
            + _description_toml(base_url=description_host.base_url)
            + f'[graph]\nprovider = "llama_cpp"\nextraction_model = "{_GRAPH_MODEL}"\nllama_cpp_base_url = "{graph_host.base_url}"\n'
        )
        client, _config, _api_key = boot(toml)

        replies = (_call(client.app.state.hyde_client), _call(client.app.state.description_client), _call(client.app.state.graph_client))

    assert replies == (_HYDE_REPLY, _DESCRIPTION_REPLY, _GRAPH_REPLY)
    # Besides its chat call each Ollama host got the window check's `/api/show`, and the description host the startup
    # reachability probe's `/api/tags` (BE-35); the graph host the model validation probe.
    assert [r.path for r in hyde_host.requests if r.path != "/api/show"] == ["/api/chat"]
    assert [r.path for r in description_host.requests if r.path not in ("/api/show", "/api/tags")] == ["/api/chat"]
    assert [r.path for r in description_host.requests if r.path == "/api/tags"] == ["/api/tags"]
    assert [r.path for r in graph_host.requests if r.path != "/v1/models"] == ["/v1/chat/completions"]
    [hyde_request] = hyde_host.requests_to("/api/chat")
    [description_request] = description_host.requests_to("/api/chat")
    [graph_request] = graph_host.requests_to("/v1/chat/completions")
    assert (hyde_request.body or {})["model"] == _HYDE_MODEL
    assert (description_request.body or {})["model"] == _DESCRIPTION_MODEL
    assert (graph_request.body or {})["model"] == _GRAPH_MODEL


# ---------------------------------------------------------------------------
# S35 / S85: shutdown order
# ---------------------------------------------------------------------------


def test_shutdown_cancels_in_flight_generation_before_stores_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dead_url: str
) -> None:
    """Order oracle: a generation parked in the provider is cancelled (and its task awaited) before the store disconnects."""
    order: list[str] = []
    started = threading.Event()

    async def parked_generate(self: DescriptionGenerator, *args: object, **kwargs: object) -> DescriptionOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            order.append("generation_cancelled")
            raise
        raise AssertionError("unreachable")

    real_disconnect = SearchStore.disconnect

    async def recording_disconnect(self: SearchStore) -> None:
        order.append("disconnect")
        await real_disconnect(self)

    monkeypatch.setattr(DescriptionGenerator, "generate", parked_generate)
    monkeypatch.setattr(SearchStore, "disconnect", recording_disconnect)
    with make_real_app(tmp_path, monkeypatch, toml_content=_description_toml(base_url=dead_url)) as (client, _config, api_key):
        _ingest_corpus(client, tmp_path, api_key)
        _wait_for(started.is_set, "the generation to start")
        lifecycle = client.app.state.description_lifecycle
        assert lifecycle.is_pending(_COLLECTION)

    assert order == ["generation_cancelled", "disconnect"]
    assert not lifecycle.is_pending(_COLLECTION)


def test_request_after_shutdown_began_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dead_url: str) -> None:
    """An ingest task being cancelled asks for a generation as its last act: the lifecycle is already closed, so the
    request is refused and no generation task starts. Proves ``close`` runs before the background tasks are cancelled."""
    outcomes: list[RequestOutcome] = []
    started_generations: list[str] = []

    async def counting_generate(self: DescriptionGenerator, *args: object, **kwargs: object) -> DescriptionOutcome:
        started_generations.append("started")
        raise AssertionError("no generation may start after shutdown began")

    monkeypatch.setattr(DescriptionGenerator, "generate", counting_generate)
    with make_real_app(tmp_path, monkeypatch, toml_content=_description_toml(base_url=dead_url)) as (client, _config, _api_key):
        state = client.app.state

        async def register_late_asker() -> None:
            async def asks_while_being_cancelled() -> None:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    ticket = await state.description_lifecycle.request_generation(_COLLECTION, DEFAULT_NAMESPACE, GenerationKind.INGEST)
                    outcomes.append(ticket.outcome)
                    raise

            task = asyncio.create_task(asks_while_being_cancelled())
            state._background_tasks.add(task)
            await asyncio.sleep(0)  # let it reach its wait

        client.portal.call(register_late_asker)  # type: ignore[union-attr]

    assert outcomes == [RequestOutcome.CLOSED]
    assert started_generations == []


def test_lifecycle_is_given_to_the_mcp_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dead_url: str) -> None:
    """The MCP app is created with the lifecycle the pipeline holds. The tools use it from BE-26; this pins the hand-over."""
    captured: dict[str, object] = {}
    from archon_search.server import mcp as mcp_module

    real_factory = mcp_module.create_mcp_http_app

    def spying_factory(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_factory(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mcp_module, "create_mcp_http_app", spying_factory)
    with make_real_app(tmp_path, monkeypatch, toml_content=_description_toml(base_url=dead_url), mcp_enabled=True) as (client, _config, _api_key):
        assert captured["description_lifecycle"] is client.app.state.description_lifecycle
        assert client.app.state.description_lifecycle is not None


def test_mcp_app_gets_no_lifecycle_when_description_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    from archon_search.server import mcp as mcp_module

    real_factory = mcp_module.create_mcp_http_app

    def spying_factory(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_factory(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mcp_module, "create_mcp_http_app", spying_factory)
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True):
        pass

    assert captured["description_lifecycle"] is None


def test_shutdown_without_description_still_cancels_background_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two-phase shutdown tolerates the absent lifecycle (the feature is off): it dereferences nothing and finishes."""
    with make_real_app(tmp_path, monkeypatch) as (client, _config, _api_key):
        tasks = list(client.app.state._background_tasks)

    assert all(task.done() for task in tasks)


# ---------------------------------------------------------------------------
# Live lane: a real local model (only when `-m live` selects it)
# ---------------------------------------------------------------------------

_OLLAMA_LIVE_URL = "http://127.0.0.1:11434"
_OLLAMA_LIVE_MODEL = "llama3.1:8b"
_LIVE_PROBE_TIMEOUT_SECONDS = 2.0
_LIVE_GENERATION_TIMEOUT_SECONDS = 240.0
_LIVE_MARKER_SELECTED = re.compile(r"(?<!not )\blive\b")


def _ollama_model_is_pulled() -> bool:
    """The port answering is not enough: the model has to be in ``/api/tags``."""
    try:
        response = httpx.get(f"{_OLLAMA_LIVE_URL}/api/tags", timeout=_LIVE_PROBE_TIMEOUT_SECONDS, trust_env=False)
        names = {entry["name"] for entry in response.json()["models"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return False
    return response.status_code == httpx.codes.OK and _OLLAMA_LIVE_MODEL in names


@pytest.mark.live
def test_live_local_model_generates_description_when_model_is_pulled(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real Ollama model describes an ingested collection. Skips unless ``-m live`` selects it (the lane calls a real local
    model, so the default run leaves it out) and unless the model is pulled, not merely the port answering."""
    if not _LIVE_MARKER_SELECTED.search(request.config.getoption("-m") or ""):
        pytest.skip("live lane: select it with `-m live` (it calls a real local model)")
    if not _ollama_model_is_pulled():
        pytest.skip(f"Ollama at {_OLLAMA_LIVE_URL} has no model {_OLLAMA_LIVE_MODEL!r}: run `ollama pull {_OLLAMA_LIVE_MODEL}`")
    toml = _description_toml(base_url=_OLLAMA_LIVE_URL, model=_OLLAMA_LIVE_MODEL) + f"timeout_seconds = {_LIVE_GENERATION_TIMEOUT_SECONDS}\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _config, api_key):
        _ingest_corpus(client, tmp_path, api_key)

        def described() -> bool:
            meta = client.portal.call(client.app.state.search_store.get_collection_meta, _COLLECTION)  # type: ignore[union-attr]
            return meta is not None and bool(meta.description)

        _wait_for(described, "the live description", timeout=_LIVE_GENERATION_TIMEOUT_SECONDS)
        meta = client.portal.call(client.app.state.search_store.get_collection_meta, _COLLECTION)  # type: ignore[union-attr]

    assert meta is not None
    assert (meta.description_source, meta.description_status) == ("generated", "ok")
    assert meta.description.strip() != ""
