"""BE-35 (collection-descriptions plan, C5, S33, S34, S35, S86): the startup reachability probe and the backfill task.

A real app boots twice on the same data: a first boot with descriptions off ingests collections into several namespaces
and seeds their description state (described, manual, failed) through the store; the second boot has ``[description]`` on
with ``backfill_on_startup = true`` against a loopback ``FakeModelServer``. The backfill and the probe run on the
TestClient's portal-thread loop, so the tests poll their ``.done()`` like ``make_real_app`` does for the startup sync.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.description_lifecycle import DescriptionLifecycle, GenerationKind
from archon_search.description_write import (
    UNCONDITIONAL,
    DescriptionFields,
    FieldChange,
)
from archon_search.server import app as app_module
from archon_search.server import description_backfill
from archon_search.server.schemas import StartupSyncResult
from archon_search.sync_suppression import write_sync_suppressed_sentinel
from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies
from tests.integration.conftest import (
    REAL_APP_DATA_SUBDIR,
    ingest_file_via_path,
    make_real_app,
)

pytestmark = pytest.mark.integration

_TEAM_A = "team-a"
_TEAM_B = "team-b"
_MODEL = "description-model"
_CONFIGURED_WINDOW = 8192
_CHAT_PATH = "/api/chat"
_TAGS_PATH = "/api/tags"
_REPLY = "Notes written by the startup backfill."
_EXISTING_TEXT = "Existing generated text."
_MANUAL_TEXT = "Hand written note."
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_STAY_STILL_SECONDS = 0.5
_SYNC_SECONDS = 0.5
_PROVIDER_TIMEOUT_SECONDS = 120

_EMPTY_DEFAULT = "alpha-empty"
_EMPTY_A = "bravo-empty"
_EMPTY_B = "charlie-empty"
_DESCRIBED = "delta-described"
_FAILED = "echo-failed"
_MANUAL = "foxtrot-manual"
_NAMESPACE_OF = {
    _EMPTY_DEFAULT: DEFAULT_NAMESPACE,
    _EMPTY_A: _TEAM_A,
    _EMPTY_B: _TEAM_B,
    _DESCRIBED: DEFAULT_NAMESPACE,
    _FAILED: _TEAM_A,
    _MANUAL: _TEAM_B,
}
_EMPTY_IN_ORDER = [_EMPTY_DEFAULT, _EMPTY_A, _EMPTY_B]  # sorted by (namespace, name)
_DISABLED_TOML = "[description]\nenabled = false\n"


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _toml(base_url: str, *, backfill: bool = True, collections: list[Path] | None = None) -> str:
    text = (
        "[description]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"ollama_context_tokens = {_CONFIGURED_WINDOW}\n"
        f"timeout_seconds = {_PROVIDER_TIMEOUT_SECONDS}\n"
        f"backfill_on_startup = {str(backfill).lower()}\n"
    )
    if collections:
        text += "[collections]\ncollections = [" + ", ".join(f'"{path}"' for path in collections) + "]\n"
    return text


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"{what} did not happen within {_WAIT_SECONDS}s"
        time.sleep(_POLL_SECONDS)


def _fields(**changes: Any) -> DescriptionFields:
    keep: FieldChange[Any] = FieldChange.keep()
    base: dict[str, Any] = {
        "description": keep,
        "source": keep,
        "status": keep,
        "last_described": keep,
        "described_at_doc_count": keep,
        "description_embedding": keep,
    }
    base.update(changes)
    return DescriptionFields(**base)


def _text_fields(text: str, source: str) -> DescriptionFields:
    return _fields(
        description=FieldChange.set_to(text),
        source=FieldChange.set_to(source),
        status=FieldChange.set_to("ok"),
        last_described=FieldChange.set_to(datetime.now(UTC)),
        description_embedding=FieldChange.clear(),
    )


def _metas(client: TestClient) -> dict[str, CollectionMeta]:
    store = client.app.state.search_store
    return {meta.name: meta for meta in client.portal.call(store.get_all_collections_meta)}  # type: ignore[union-attr]


class _Tokens:
    """The namespace tokens of both boots (the second boot must accept the keys the first one used)."""

    def __init__(self) -> None:
        self.namespaces = {secrets.token_hex(32): _TEAM_A, secrets.token_hex(32): _TEAM_B}
        self.by_namespace = {namespace: token for token, namespace in self.namespaces.items()}


@pytest.fixture
def tokens() -> _Tokens:
    return _Tokens()


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    directory = tmp_path / "corpus"
    directory.mkdir()
    (directory / "notes.txt").write_text("Archon search keeps collections of local documents searchable.\n" * 20, encoding="utf-8")
    return directory


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, corpus: Path) -> None:
    """Six collections in three namespaces: three empty, one generated, one failed, one manual (descriptions off)."""
    with make_real_app(tmp_path, monkeypatch, toml_content=_DISABLED_TOML, namespaces=tokens.namespaces) as (client, _config, admin_key):
        for name, namespace in _NAMESPACE_OF.items():
            key = admin_key if namespace == DEFAULT_NAMESPACE else tokens.by_namespace[namespace]
            ingest_file_via_path(client, name, str(corpus), api_key=key)
        store = client.app.state.search_store
        for name, fields in (
            (_DESCRIBED, _text_fields(_EXISTING_TEXT, "generated")),
            (_MANUAL, _text_fields(_MANUAL_TEXT, "manual")),
            (_FAILED, _fields(status=FieldChange.set_to("failed"))),
        ):
            outcome = client.portal.call(  # type: ignore[union-attr]
                lambda name=name, fields=fields: store.write_description(name, _NAMESPACE_OF[name], fields, UNCONDITIONAL)
            )
            assert outcome == "written"
        assert {name for name, meta in _metas(client).items() if not meta.description} == {*_EMPTY_IN_ORDER, _FAILED}


@contextlib.contextmanager
def _second_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, toml: str
) -> Iterator[tuple[TestClient, str]]:
    with make_real_app(tmp_path, monkeypatch, toml_content=toml, namespaces=tokens.namespaces) as (client, _config, admin_key):
        yield client, admin_key


def _backfill_task(client: TestClient) -> asyncio.Task[None]:
    task = client.app.state._description_backfill_task
    assert task is not None
    return task


def _chat_count(server: FakeModelServer) -> int:
    return len(server.requests_to(_CHAT_PATH))


def _record_requests(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Every ``request_generation`` call of any lifecycle lands in *events* as ``request:<collection>:<kind>``."""
    original = DescriptionLifecycle.request_generation

    async def recording(self: DescriptionLifecycle, collection: str, namespace: str, kind: GenerationKind) -> Any:
        events.append(f"request:{collection}:{kind.value}")
        return await original(self, collection, namespace, kind)

    monkeypatch.setattr(DescriptionLifecycle, "request_generation", recording)


def test_backfill_waits_for_sync_and_probe_then_fills_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, corpus: Path, seeded: None
) -> None:
    events: list[str] = []

    async def delayed_sync(job_store: Any, app_state: Any, collections: list[str]) -> None:
        await asyncio.sleep(_SYNC_SECONDS)  # long after the probe (one local request) has answered
        events.append("sync-end")
        app_state.sync_result = StartupSyncResult.DONE

    real_probe = description_backfill.probe_description_reachability

    async def recording_probe(*args: Any, **kwargs: Any) -> Any:
        result = await real_probe(*args, **kwargs)
        events.append("probe-end")
        return result

    monkeypatch.setattr(app_module, "_run_startup_sync", delayed_sync)
    monkeypatch.setattr(description_backfill, "probe_description_reachability", recording_probe)
    _record_requests(monkeypatch, events)

    with FakeModelServer(reply_text=_REPLY, hold_chat=True) as server:
        toml = _toml(server.base_url, collections=[corpus])
        with _second_boot(tmp_path, monkeypatch, tokens, toml) as (client, _admin_key):
            _wait_for(lambda: _chat_count(server) >= 1, "the first generation request")
            time.sleep(_STAY_STILL_SECONDS)
            # One at a time: the first generation is held at the provider, so no second request has been asked for.
            assert _chat_count(server) == 1
            assert [event for event in events if event.startswith("request:")] == [f"request:{_EMPTY_IN_ORDER[0]}:backfill"]
            assert events.index("sync-end") < events.index(f"request:{_EMPTY_IN_ORDER[0]}:backfill")
            assert events.index("probe-end") < events.index(f"request:{_EMPTY_IN_ORDER[0]}:backfill")
            assert not _backfill_task(client).done()
            reachability = client.app.state.description_reachability
            assert reachability is not None and reachability.reachable is True
            assert len(server.requests_to(_TAGS_PATH)) == 1  # probed once, never refreshed

            server.release()
            _wait_for(_backfill_task(client).done, "the backfill")
            metas = _metas(client)

    assert [event for event in events if event.startswith("request:")] == [f"request:{name}:backfill" for name in _EMPTY_IN_ORDER]
    for name in _EMPTY_IN_ORDER:
        assert metas[name].description == _REPLY
        assert metas[name].description_source == "generated"
        assert metas[name].description_status == "ok"
    assert metas[_DESCRIBED].description == _EXISTING_TEXT
    assert metas[_MANUAL].description == _MANUAL_TEXT and metas[_MANUAL].description_source == "manual"
    assert not metas[_FAILED].description and metas[_FAILED].description_status == "failed"
    assert _chat_count(server) == len(_EMPTY_IN_ORDER)  # nothing described, manual or failed was asked for


def test_backfill_waits_for_a_slow_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, seeded: None
) -> None:
    events: list[str] = []
    probe_gate = threading.Event()
    real_probe = description_backfill.probe_description_reachability

    async def held_probe(*args: Any, **kwargs: Any) -> Any:
        while not probe_gate.is_set():
            await asyncio.sleep(_POLL_SECONDS)
        result = await real_probe(*args, **kwargs)
        events.append("probe-end")
        return result

    monkeypatch.setattr(description_backfill, "probe_description_reachability", held_probe)
    _record_requests(monkeypatch, events)

    with (
        FakeModelServer(reply_text=_REPLY) as server,
        _second_boot(tmp_path, monkeypatch, tokens, _toml(server.base_url)) as (client, _admin_key),
    ):
        try:
            time.sleep(_STAY_STILL_SECONDS)
            assert events == [] and _chat_count(server) == 0
            assert not _backfill_task(client).done()
            assert client.app.state.description_reachability is None
        finally:
            probe_gate.set()
        _wait_for(_backfill_task(client).done, "the backfill")
        metas = _metas(client)

    assert events[0] == "probe-end"
    assert [event for event in events[1:]] == [f"request:{name}:backfill" for name in _EMPTY_IN_ORDER]
    assert all(metas[name].description == _REPLY for name in _EMPTY_IN_ORDER)


def test_backfill_skipped_with_one_info_when_startup_sync_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, corpus: Path, seeded: None, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / REAL_APP_DATA_SUBDIR))
    write_sync_suppressed_sentinel()

    with FakeModelServer(reply_text=_REPLY) as server, caplog.at_level(logging.INFO):
        toml = _toml(server.base_url, collections=[corpus])
        with _second_boot(tmp_path, monkeypatch, tokens, toml) as (client, _admin_key):
            assert client.app.state.sync_result == StartupSyncResult.SUPPRESSED
            _wait_for(_backfill_task(client).done, "the backfill task")
            metas = _metas(client)
            reachability = client.app.state.description_reachability

        assert _chat_count(server) == 0
        assert server.requests_to(_TAGS_PATH) == []  # the probe is skipped too: no provider is called at all

    assert reachability is None
    assert not any(metas[name].description for name in _EMPTY_IN_ORDER)
    infos = [r for r in caplog.records if r.name == description_backfill.__name__ and r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "suppressed" in infos[0].getMessage().lower()


def test_backfill_cancelled_cleanly_at_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, seeded: None
) -> None:
    with FakeModelServer(reply_text=_REPLY, hold_chat=True) as server:
        with _second_boot(tmp_path, monkeypatch, tokens, _toml(server.base_url)) as (client, _admin_key):
            _wait_for(lambda: _chat_count(server) >= 1, "the first generation request")
            backfill = _backfill_task(client)
            lifecycle = client.app.state.description_lifecycle
            assert lifecycle.is_pending(_EMPTY_IN_ORDER[0])
            assert not backfill.done()
        # The app is shut down with the model call still held: the backfill and the generation were cancelled and awaited.
        assert backfill.cancelled()
        assert not lifecycle.is_pending(_EMPTY_IN_ORDER[0])
        server.release()
        time.sleep(_STAY_STILL_SECONDS)
        assert _chat_count(server) == 1  # the loop never went on to a second collection

        with _second_boot(tmp_path, monkeypatch, tokens, _DISABLED_TOML) as (client, _admin_key):
            metas = _metas(client)

    for name in _EMPTY_IN_ORDER:
        assert not metas[name].description  # no half-written description
        assert metas[name].description_status is None
    assert metas[_DESCRIBED].description == _EXISTING_TEXT


def test_provider_down_boots_and_skips_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, seeded: None, caplog: pytest.LogCaptureFixture
) -> None:
    with FakeModelServer(reply_text=_REPLY, status_overrides={_TAGS_PATH: 503}) as server, caplog.at_level(logging.INFO):
        with _second_boot(tmp_path, monkeypatch, tokens, _toml(server.base_url)) as (client, _admin_key):
            assert client.get("/health").status_code == 200  # the server is up
            _wait_for(client.app.state._description_probe_task.done, "the probe")
            _wait_for(_backfill_task(client).done, "the backfill task")
            reachability = client.app.state.description_reachability
            metas = _metas(client)

        assert _chat_count(server) == 0

    assert reachability is not None and reachability.reachable is False
    assert not any(metas[name].description for name in _EMPTY_IN_ORDER)
    warnings = [r for r in caplog.records if r.name == description_backfill.__name__ and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "not reachable" in warnings[0].getMessage()
    infos = [r for r in caplog.records if r.name == description_backfill.__name__ and r.levelno == logging.INFO]
    assert len(infos) == 1  # the backfill's one skip notice


def test_no_probe_and_no_backfill_task_when_disabled_or_backfill_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, seeded: None
) -> None:
    toml_off_with_backfill = "[description]\nenabled = false\nbackfill_on_startup = true\n"
    with _second_boot(tmp_path, monkeypatch, tokens, toml_off_with_backfill) as (client, _admin_key):
        assert client.app.state._description_probe_task is None
        assert client.app.state._description_backfill_task is None

    with _second_boot(tmp_path, monkeypatch, tokens, _DISABLED_TOML) as (client, _admin_key):
        assert client.app.state._description_probe_task is None
        assert client.app.state._description_backfill_task is None
        assert client.app.state.description_reachability is None

    with FakeModelServer(reply_text=_REPLY) as server:
        with _second_boot(tmp_path, monkeypatch, tokens, _toml(server.base_url, backfill=False)) as (client, _admin_key):
            _wait_for(client.app.state._description_probe_task.done, "the probe")
            assert client.app.state._description_backfill_task is None
            assert client.app.state.description_reachability.reachable is True
        assert _chat_count(server) == 0


def test_probe_still_runs_under_a_suppressed_sync_when_backfill_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, corpus: Path, seeded: None
) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / REAL_APP_DATA_SUBDIR))
    write_sync_suppressed_sentinel()

    with FakeModelServer(reply_text=_REPLY) as server:
        toml = _toml(server.base_url, backfill=False, collections=[corpus])
        with _second_boot(tmp_path, monkeypatch, tokens, toml) as (client, _admin_key):
            assert client.app.state.sync_result == StartupSyncResult.SUPPRESSED
            _wait_for(client.app.state._description_probe_task.done, "the probe")
            assert client.app.state.description_reachability.reachable is True
            assert client.app.state._description_backfill_task is None
        assert _chat_count(server) == 0
