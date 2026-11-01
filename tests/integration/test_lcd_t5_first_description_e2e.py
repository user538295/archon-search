"""T-5 (collection-descriptions plan, S17, S19, S23, S40, S55) — the first description appears after the first ingest.

``make_real_app`` boots the real lifespan from a ``[description]`` TOML section (provider ``ollama``) with a real LanceDB
store; only the model host is faked (``tests/_fake_model_server.py``, a real HTTP server the real ``ollama`` SDK talks to).
What a consumer sees is asserted: the ingest job is DONE before the model answers, the status reads ``pending`` while the
model call is in flight and ``ok`` / source ``generated`` after it, and REST, MCP and ``collection info`` (through
``tests/_cli_testclient_bridge.py``) agree. The prompt the model received is the wire-level evidence for S55: chunks that
carry an ACL never leave the machine.

Run with:
    uv run pytest tests/integration/test_lcd_t5_first_description_e2e.py --no-cov -q
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.cli.collection import collection as collection_command
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.server.schemas import WarmupResult
from tests._cli_testclient_bridge import cli_against_app
from tests._fake_model_server import (
    FakeModelServer,
    RecordedRequest,
    keep_loopback_out_of_proxies,
)
from tests.integration.conftest import ingest_file_via_path, make_real_app
from tests.integration.test_mcp_roundtrip_t2 import (
    _extract_tool_text,
    _mcp_call_tool,
    _mcp_initialize,
)

pytestmark = pytest.mark.integration

_COLLECTION = "described-e2e"
_MODEL = "description-model"
_FIRST_REPLY = "Notes about the first ingest of this collection."
_SECOND_REPLY = "Notes about the collection after it grew."
_CHAT_PATH = "/api/chat"
_CONFIGURED_WINDOW = 8192
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_OPEN_MARKERS = ("kestrel-4471", "marmot-9082")
_RESTRICTED_MARKERS = ("walrus-5513", "ibis-7720", "tapir-8836")
_FIRST_DOCUMENT_MARKER = "heron-3305"
_GROWTH_MARKERS = ("lynx-6641", "otter-2298")
_RESTRICTED_SIDECAR_WORDS = ("some-other-team", "team-only", "deny-all")
_ACL_CORPUS_DOCUMENTS = len(_OPEN_MARKERS) + len(_RESTRICTED_MARKERS)
_DESCRIBED_DOCUMENTS = 5
_PROVIDER_TIMEOUT_SECONDS = 120  # far above any hold: a slow runner must not turn `pending` into `failed`


def _description_toml(base_url: str | None, *, enabled: bool = True) -> str:
    """The ``[description]`` section; ``None`` leaves the section out altogether."""
    if base_url is None:
        return ""
    return (
        "[description]\n"
        f"enabled = {str(enabled).lower()}\n"
        'provider = "ollama"\n'
        f'model = "{_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"ollama_context_tokens = {_CONFIGURED_WINDOW}\n"
        f"timeout_seconds = {_PROVIDER_TIMEOUT_SECONDS}\n"
    )


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests."""
    keep_loopback_out_of_proxies(monkeypatch)


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"{what} did not happen within {_WAIT_SECONDS}s"
        time.sleep(_POLL_SECONDS)


def _await_ready(client: TestClient) -> None:
    """Wait for the warm-up and the window resolution: a generation that starts earlier sizes itself on the assumed window."""
    _wait_for(
        lambda: getattr(client.app.state, "warmup_result", None) not in (None, WarmupResult.PENDING),
        "the lifespan warm-up",
    )
    assert client.app.state.warmup_result == WarmupResult.DONE
    _wait_for(client.app.state._llm_window_task.done, "the context-window resolution")


@contextlib.contextmanager
def _booted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str, *, mcp: bool = False
) -> Iterator[tuple[TestClient, str]]:
    with make_real_app(tmp_path, monkeypatch, toml_content=toml, mcp_enabled=mcp) as (client, _config, api_key):
        _await_ready(client)
        yield client, api_key


def _write_documents(directory: Path, documents: dict[str, str], *, sidecars: dict[str, str] | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in documents.items():
        (directory / name).write_text(text, encoding="utf-8")
    for name, content in (sidecars or {}).items():
        (directory / f"{name}.acl").write_text(content, encoding="utf-8")


def _document(marker: str) -> str:
    return f"Field notes about {marker} and how the survey was carried out across the valley.\n" * 3


def _ingest(client: TestClient, api_key: str, directory: Path) -> None:
    """Ingest the corpus DIRECTORY through ``POST /ingest`` (one job, one description request for the whole batch)."""
    ingest_file_via_path(client, _COLLECTION, str(directory), api_key=api_key)


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _detail(client: TestClient, api_key: str) -> dict[str, Any]:
    response = client.get(f"/collections/{_COLLECTION}", headers=_headers(api_key))
    assert response.status_code == 200, response.text
    return response.json()


def _listed(client: TestClient, api_key: str) -> dict[str, Any]:
    response = client.get("/collections/", headers=_headers(api_key))
    assert response.status_code == 200, response.text
    return next(item for item in response.json() if item["name"] == _COLLECTION)


def _await_chat_requests(server: FakeModelServer, count: int) -> None:
    _wait_for(lambda: len(server.requests_to(_CHAT_PATH)) >= count, f"chat request number {count}")


def _await_description(client: TestClient, api_key: str, text: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}

    def described() -> bool:
        latest.update(_detail(client, api_key))
        return latest["description"] == text and latest["description_status"] == "ok"

    _wait_for(described, f"the description {text!r} with status ok")
    return latest


def _filler_documents(count: int, *, start: int = 0) -> dict[str, str]:
    return {f"filler-{index}.txt": _document(f"filler-{index}") for index in range(start, start + count)}


def _mcp_tool(client: TestClient, api_key: str, session: str, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    return _extract_tool_text(_mcp_call_tool(client, api_key, session, tool, arguments or {}), tool)


def _mcp_collection_meta(client: TestClient, api_key: str, session: str) -> dict[str, Any]:
    return _mcp_tool(client, api_key, session, "get_collection_meta", {"name": _COLLECTION})



def _entry(listed: Any) -> dict[str, Any]:
    return next(item for item in listed if item["name"] == _COLLECTION)


def _prompt_of(request: RecordedRequest) -> str:
    assert request.body is not None
    [message] = request.body["messages"]
    return message["content"]


def test_e2e_first_ingest_returns_then_description_appears_generated_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "corpus"
    _write_documents(corpus, {"first.txt": _document(_FIRST_DOCUMENT_MARKER)})
    with (
        FakeModelServer(reply_text=_FIRST_REPLY, hold_chat=True) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url), mcp=True) as (client, api_key),
        cli_against_app(client, api_key, monkeypatch) as cli,
    ):
        _ingest(client, api_key, corpus)  # returns only at job DONE, while the model call below is still held

        _await_chat_requests(server, 1)
        held = _detail(client, api_key)
        assert (held["description"], held["description_source"], held["description_status"]) == ("", None, "pending")
        assert _listed(client, api_key)["description_status"] == "pending"
        pending_info = cli.invoke(collection_command, ["info", _COLLECTION])
        assert pending_info.exit_code == 0, pending_info.output
        assert "description_status: pending" in pending_info.output
        session = _mcp_initialize(client, api_key)
        assert _mcp_collection_meta(client, api_key, session)["description_status"] == "pending"
        assert _entry(_mcp_tool(client, api_key, session, "list_collections"))["description_status"] == "pending"
        assert _entry(_mcp_tool(client, api_key, session, "get_collections_meta"))["description_status"] == "pending"

        server.release()
        described = _await_description(client, api_key, _FIRST_REPLY)

    assert described["description_source"] == "generated"
    assert described["description_status"] == "ok"
    assert len(server.requests_to(_CHAT_PATH)) == 1
    assert _FIRST_DOCUMENT_MARKER in _prompt_of(server.requests_to(_CHAT_PATH)[0])


def test_e2e_description_read_through_rest_mcp_and_cli_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = tmp_path / "corpus"
    _write_documents(corpus, {"first.txt": _document(_FIRST_DOCUMENT_MARKER)})
    with (
        FakeModelServer(reply_text=_FIRST_REPLY) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url), mcp=True) as (client, api_key),
        cli_against_app(client, api_key, monkeypatch) as cli,
    ):
        _ingest(client, api_key, corpus)
        _await_description(client, api_key, _FIRST_REPLY)

        session = _mcp_initialize(client, api_key)
        mcp_meta = _extract_tool_text(
            _mcp_call_tool(client, api_key, session, "get_collection_meta", {"name": _COLLECTION}),
            "get_collection_meta",
        )
        mcp_listed = _entry(
            _extract_tool_text(_mcp_call_tool(client, api_key, session, "list_collections", {}), "list_collections")
        )
        mcp_all_meta = _entry(
            _extract_tool_text(_mcp_call_tool(client, api_key, session, "get_collections_meta", {}), "get_collections_meta")
        )
        cli_result = cli.invoke(collection_command, ["info", _COLLECTION])
        rest_views = [_listed(client, api_key), _detail(client, api_key)]

    for view in [*rest_views, mcp_meta, mcp_listed, mcp_all_meta]:
        assert view["description"] == _FIRST_REPLY
        assert view["description_source"] == "generated"
        assert view["description_status"] == "ok"
    assert cli_result.exit_code == 0, cli_result.output
    assert f"description: {_FIRST_REPLY}" in cli_result.output
    assert "description_source: generated" in cli_result.output
    assert "description_status: ok" in cli_result.output


def test_e2e_restart_loses_pending_and_next_ingest_regenerates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sizes are chosen so only a baseline that stayed at the last stored generation qualifies the final ingest: 5
    documents were described, the collection then doubled while held (10), and one more arrives after the restart (11).
    11 is +120% from 5 but only +10% from 10, so a baseline advanced by the lost request would not regenerate."""
    corpus = tmp_path / "corpus"
    _write_documents(corpus, _filler_documents(_DESCRIBED_DOCUMENTS))
    with FakeModelServer(reply_text=_FIRST_REPLY) as server:
        with _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, api_key):
            _ingest(client, api_key, corpus)
            _await_description(client, api_key, _FIRST_REPLY)

            # The collection doubles (a change of 20% or more asks for a new description) and the model call is held,
            # so the app goes down with the generation in flight.
            server.hold()
            _write_documents(corpus, _filler_documents(_DESCRIBED_DOCUMENTS, start=_DESCRIBED_DOCUMENTS))
            _ingest(client, api_key, corpus)
            _await_chat_requests(server, 2)
            pending = _detail(client, api_key)
            assert pending["description_status"] == "pending"
            assert (pending["description"], pending["description_source"]) == (_FIRST_REPLY, "generated")

        server.reply_text = _SECOND_REPLY
        server.release()
        with _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, api_key):
            reread = _detail(client, api_key)
            assert (reread["description"], reread["description_source"], reread["description_status"]) == (
                _FIRST_REPLY,
                "generated",
                "ok",
            )
            assert len(server.requests_to(_CHAT_PATH)) == 2  # the restart itself asked the model for nothing

            _write_documents(corpus, {"one-more.txt": _document(_GROWTH_MARKERS[1])})
            _ingest(client, api_key, corpus)
            regenerated = _await_description(client, api_key, _SECOND_REPLY)

    assert regenerated["description_source"] == "generated"
    chats = server.requests_to(_CHAT_PATH)
    assert len(chats) == 3
    assert _GROWTH_MARKERS[1] in _prompt_of(chats[2])  # the request that regenerated came from the last ingest


def test_e2e_only_open_chunks_reach_the_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = tmp_path / "corpus"
    _write_documents(
        corpus,
        {
            "open-a.txt": _document(_OPEN_MARKERS[0]),
            "open-b.txt": _document(_OPEN_MARKERS[1]),
            "team-only.txt": _document(_RESTRICTED_MARKERS[0]),
            "deny-all.txt": _document(_RESTRICTED_MARKERS[1]),
            "callers-own.txt": _document(_RESTRICTED_MARKERS[2]),
        },
        sidecars={
            "team-only.txt": "some-other-team\n",
            "deny-all.txt": "deny-all\n",
            "callers-own.txt": f"{DEFAULT_NAMESPACE}\n",  # the caller may read it, but it is not an open chunk
        },
    )
    with (
        FakeModelServer(reply_text=_FIRST_REPLY) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, api_key),
    ):
        _ingest(client, api_key, corpus)
        _await_description(client, api_key, _FIRST_REPLY)
        assert _detail(client, api_key)["doc_count"] == _ACL_CORPUS_DOCUMENTS  # the restricted ones were ingested too

    [chat] = server.requests_to(_CHAT_PATH)
    prompt = _prompt_of(chat)
    for marker in _OPEN_MARKERS:
        assert marker in prompt
    wire = json.dumps(chat.body)  # the whole request, not only the prompt text
    for leaked in (*_RESTRICTED_MARKERS, *_RESTRICTED_SIDECAR_WORDS):
        assert leaked not in wire


@pytest.mark.parametrize("section_present", [False, True], ids=["section-absent", "section-present-but-disabled"])
def test_e2e_disabled_description_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section_present: bool
) -> None:
    """S19. With the section absent no config names the fake server, so the empty request list proves little by itself;
    the absent lifecycle is the structural evidence there. The disabled-but-configured arm aims the provider at the fake
    server, so a stray request would be recorded. Two ingests (the second would qualify) leave time for a late request."""
    corpus = tmp_path / "corpus"
    _write_documents(corpus, {"first.txt": _document(_FIRST_DOCUMENT_MARKER)})
    with FakeModelServer(reply_text=_FIRST_REPLY) as server:
        toml = _description_toml(server.base_url, enabled=False) if section_present else _description_toml(None)
        with _booted(tmp_path, monkeypatch, toml) as (client, api_key):
            _ingest(client, api_key, corpus)
            _write_documents(corpus, {"second.txt": _document(_GROWTH_MARKERS[0])})
            _ingest(client, api_key, corpus)
            assert client.app.state.description_lifecycle is None
            assert client.app.state.description_client is None
            detail = _detail(client, api_key)

    assert server.requests == []
    assert detail["doc_count"] == 2
    assert detail["description"] == ""
    assert detail["description_status"] is None
