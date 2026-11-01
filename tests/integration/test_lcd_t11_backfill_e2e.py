"""T-11 (collection-descriptions plan) — the startup backfill end to end (S33, S34, S35, S86).

Every test runs the real app (lifespan, real LanceDB, real ingest, real startup sync) over stores written by an earlier
boot with ``[description]`` off, the way an operator who turns descriptions on meets existing collections. The model is a
loopback ``FakeModelServer`` (a recording subclass) or the real-subprocess fake ``claude`` of ``tests/_fake_claude.py``.
The BE-35 integration file pins the task's mechanics with patched seams; these tests add what an operator sees: several
namespaces (visited in an order that differs from name order), a real startup sync, a real RUNNING ingest job left by a
dead process, REST reads with each namespace's own key, and a real child process.

* S33: only empty collections without a failed status are filled, one at a time, namespace by namespace, in the
  background, after the sync and the probe, skipping one a describe job is already generating. The sync end state
  covered here is DONE (BE-35's file covers a collection-less boot).
* S34: with the provider down the server boots, the unreachable finding is one WARNING and no model call is made. The
  scenario's ``/status`` clause is not asserted: the ``/status`` description block arrives with BE-37, whose e2e is T-12;
  the verdict is read from ``app.state.description_reachability``.
* S86: a suppressed startup sync skips the backfill and sends neither a prompt nor the probe (the client's own
  context-window lookup at startup is the one request the provider may see); after a clean ``POST /sync`` the next start
  backfills.
* S35: a shutdown mid-backfill cancels the run, kills the ``claude`` child before the stores disconnect (its descendants
  by the end of the shutdown), never goes on to another collection, and leaves no description. The cancel lands during
  the model call, so the guarded write itself is never interrupted here (the lifecycle's unit tests cover a cancel
  during the write).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import (
    DESCRIPTION_SOURCE_GENERATED,
    DESCRIPTION_SOURCE_MANUAL,
    DESCRIPTION_STATUS_FAILED,
    DESCRIPTION_STATUS_OK,
    DESCRIPTION_STATUS_PENDING,
    CollectionMeta,
)
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.description_write import (
    UNCONDITIONAL,
    DescriptionFields,
    FieldChange,
)
from archon_search.jobs.store import JobStore
from archon_search.server import app as app_module
from archon_search.server import description_backfill
from archon_search.server.schemas import StartupSyncResult
from archon_search.sync_suppression import get_sync_suppressed_file
from archon_search.types import JobStatus
from tests import _fake_claude
from tests._fake_model_server import (
    FakeModelServer,
    RecordedRequest,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests.integration.conftest import (
    JOBS_FILE_NAME,
    ingest_doc,
    make_real_app,
)

pytestmark = pytest.mark.integration

_TEAM_A = "team-a"
_TEAM_B = "team-b"
_MODEL = "description-model"
_CONFIGURED_WINDOW = 8192
_CHAT_PATH = "/api/chat"
_TAGS_PATH = "/api/tags"
_SHOW_PATH = "/api/show"
_REPLY = "Notes written by the startup backfill"
_EXISTING_TEXT = "Existing generated text."
_MANUAL_TEXT = "Hand written note."
_WAIT_SECONDS = 60.0
_POLL_SECONDS = 0.01
_PROVIDER_TIMEOUT_SECONDS = 120
#: How long the recording server takes over one chat answer: two overlapping calls would show in the timeline.
_CALL_SECONDS = 0.2
#: How long the patched startup sync stays open after the probe has answered and the window lookup ended.
_SYNC_MARGIN_SECONDS = 0.5
_FAKE_CLAUDE_HANG_SECONDS = float(_PROVIDER_TIMEOUT_SECONDS)
_PROCESS_GONE_SECONDS = 15.0
_CLAUDE_MODEL = "claude-fake"
_ANSWERS_503 = "answers-503"
_NO_ANSWER = "no-answer"
_NAMESPACE_TOKEN_BYTES = 32
_NOTE_REPEATS = 5
_HTTP_OK = 200
_HTTP_ACCEPTED = 202
_HTTP_UNAVAILABLE = 503
_ACTIVE_JOB_STATUSES = frozenset({JobStatus.PENDING.value, JobStatus.QUEUED.value, JobStatus.RUNNING.value})

_YANKEE = "yankee_empty"  # the sync names a collection after its directory, with hyphens turned into underscores
_PAPA = "papa-empty"
_KILO = "kilo-empty"
_ALPHA = "alpha-empty"
_MIKE = "mike-empty"
_DESCRIBED = "delta-described"
_FAILED = "echo-failed"
_MANUAL = "foxtrot-manual"

#: (namespace, name) of every seeded collection.
_COLLECTIONS = (
    (DEFAULT_NAMESPACE, _YANKEE),
    (DEFAULT_NAMESPACE, _PAPA),
    (DEFAULT_NAMESPACE, _DESCRIBED),
    (_TEAM_A, _ALPHA),
    (_TEAM_A, _KILO),
    (_TEAM_A, _FAILED),
    (_TEAM_B, _MIKE),
    (_TEAM_B, _MANUAL),
)
#: The order the backfill visits the empty ones: namespace first, then name (name-only order would differ).
_EMPTY_IN_ORDER = (
    (DEFAULT_NAMESPACE, _PAPA),
    (DEFAULT_NAMESPACE, _YANKEE),
    (_TEAM_A, _ALPHA),
    (_TEAM_A, _KILO),
    (_TEAM_B, _MIKE),
)
_DISABLED_TOML = "[description]\nenabled = false\n"


class _Described(NamedTuple):
    """What REST reports of a collection's description."""

    description: str
    source: str | None
    status: str | None


_UNDESCRIBED = _Described("", None, None)


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _key(namespace: str, name: str) -> str:
    return f"{namespace}/{name}"


def _marker(namespace: str, name: str) -> str:
    """A word that only this collection's documents carry, so a prompt shows which collection it asks about."""
    return f"zz{namespace}zz{name}zz".replace("-", "")


def _reply_for(namespace: str, name: str) -> str:
    """The answer the recording server gives about one collection (its own, so a swapped write would show)."""
    return f"{_REPLY} for {_key(namespace, name)}."


class _RecordingServer(FakeModelServer):
    """A ``FakeModelServer`` that keeps a timeline: the probe, and for every model call when it arrived (naming the
    collection it asks about, found by the marker its excerpts carry) and when it was answered. Each collection gets
    its own answer text."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.timeline: list[tuple[str, str]] = []
        self._collection_of = {_marker(namespace, name): (namespace, name) for namespace, name in _COLLECTIONS}

    def _asked_about(self, request: RecordedRequest) -> tuple[str, str] | None:
        text = json.dumps(request.body)
        found = [pair for marker, pair in self._collection_of.items() if marker in text]
        if len(found) != 1:  # reported in the test thread: an assertion here would only drop the request
            self.timeline.append(("unattributable", f"{len(found)} collections named in one prompt"))
            return None
        return found[0]

    def record(self, request: RecordedRequest) -> None:
        super().record(request)
        if request.path == _TAGS_PATH:
            self.timeline.append(("probe", ""))
        elif request.path == _CHAT_PATH:
            asked = self._asked_about(request)
            self.timeline.append(("start", _key(*asked) if asked else ""))

    def respond(self, request: RecordedRequest) -> tuple[int, dict[str, Any]]:
        if request.path != _CHAT_PATH:
            return super().respond(request)
        asked = self._asked_about(request)
        time.sleep(_CALL_SECONDS)
        self.timeline.append(("end", _key(*asked) if asked else ""))
        status, body = super().respond(request)
        if asked is not None and status == _HTTP_OK:
            body["message"]["content"] = _reply_for(*asked)  # its own text per collection, with no state shared between calls
        return status, body

    def asked_in_order(self) -> list[str]:
        return [detail for kind, detail in self.timeline if kind == "start"]

    def assert_every_prompt_was_attributed(self) -> None:
        assert not [event for event in self.timeline if event[0] == "unattributable"], self.timeline


def _toml(base_url: str, *, collections: list[Path] | None = None) -> str:
    text = (
        "[description]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"ollama_context_tokens = {_CONFIGURED_WINDOW}\n"
        f"timeout_seconds = {_PROVIDER_TIMEOUT_SECONDS}\n"
        "backfill_on_startup = true\n"
    )
    return text + _collections_toml(collections)


def _claude_toml() -> str:
    return (
        "[description]\n"
        "enabled = true\n"
        'provider = "claude_cli"\n'
        f'model = "{_CLAUDE_MODEL}"\n'
        f"timeout_seconds = {_PROVIDER_TIMEOUT_SECONDS}\n"
        "backfill_on_startup = true\n"
    )


def _collections_toml(collections: list[Path] | None) -> str:
    if not collections:
        return ""
    return "[collections]\ncollections = [" + ", ".join(f'"{path}"' for path in collections) + "]\n"


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"{what} did not happen within {_WAIT_SECONDS}s"
        time.sleep(_POLL_SECONDS)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
        status=FieldChange.set_to(DESCRIPTION_STATUS_OK),
        last_described=FieldChange.set_to(datetime.now(UTC)),
        description_embedding=FieldChange.clear(),
    )


@dataclass
class _World:
    """What the boots of one test share: the namespace tokens and the directory the startup sync is configured with."""

    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch
    namespaces: dict[str, str]
    synced_directory: Path

    def token(self, namespace: str, admin_key: str) -> str:
        if namespace == DEFAULT_NAMESPACE:
            return admin_key
        return next(token for token, name in self.namespaces.items() if name == namespace)

    @contextlib.contextmanager
    def boot(self, toml: str) -> Iterator[tuple[TestClient, str]]:
        with make_real_app(self.tmp_path, self.monkeypatch, toml_content=toml, namespaces=self.namespaces) as (
            client,
            _config,
            admin_key,
        ):
            yield client, admin_key


def _all_metas(client: TestClient) -> dict[tuple[str, str], CollectionMeta]:
    store = client.app.state.search_store
    metas = client.portal.call(store.get_all_collections_meta)  # type: ignore[union-attr]
    return {(meta.namespace, meta.name): meta for meta in metas}


def _read_one(client: TestClient, token: str, name: str) -> _Described:
    response = client.get(f"/collections/{name}", headers=_headers(token))
    assert response.status_code == _HTTP_OK, f"{name}: {response.text}"
    detail = response.json()
    return _Described(detail["description"], detail["description_source"], detail["description_status"])


def _read_state(client: TestClient, world: _World, admin_key: str) -> dict[tuple[str, str], _Described]:
    """Every seeded collection as REST reports it to that collection's own namespace."""
    return {
        (namespace, name): _read_one(client, world.token(namespace, admin_key), name)
        for namespace, name in _COLLECTIONS
    }


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
    """Eight collections in three namespaces, written by a boot with descriptions off: five empty (one of them is the
    directory the next boots sync), one generated, one failed, one manual."""
    namespaces = {secrets.token_hex(_NAMESPACE_TOKEN_BYTES): _TEAM_A, secrets.token_hex(_NAMESPACE_TOKEN_BYTES): _TEAM_B}
    synced_directory = tmp_path / "corpora" / _YANKEE
    synced_directory.mkdir(parents=True)
    survey = f"{_marker(DEFAULT_NAMESPACE, _YANKEE)} survey notes.\n" * _NOTE_REPEATS
    (synced_directory / "notes.txt").write_text(survey, encoding="utf-8")
    created = _World(tmp_path, monkeypatch, namespaces, synced_directory)
    # The first boot's own startup sync creates the synced collection, so the sync state records it and a later sync of the
    # unchanged directory ingests nothing (an ingest would ask for a description itself, which is not the backfill).
    with created.boot(_DISABLED_TOML + _collections_toml([synced_directory])) as (client, admin_key):
        for namespace, name in _COLLECTIONS:
            if name != _YANKEE:
                text = f"{_marker(namespace, name)} field notes about how the survey was carried out.\n" * _NOTE_REPEATS
                ingest_doc(client, name, text, f"/docs/{name}.md", api_key=created.token(namespace, admin_key))
        store = client.app.state.search_store
        for namespace, name, fields in (
            (DEFAULT_NAMESPACE, _DESCRIBED, _text_fields(_EXISTING_TEXT, DESCRIPTION_SOURCE_GENERATED)),
            (_TEAM_B, _MANUAL, _text_fields(_MANUAL_TEXT, DESCRIPTION_SOURCE_MANUAL)),
            (_TEAM_A, _FAILED, _fields(status=FieldChange.set_to(DESCRIPTION_STATUS_FAILED))),
        ):
            outcome = client.portal.call(  # type: ignore[union-attr]
                lambda name=name, namespace=namespace, fields=fields: store.write_description(
                    name, namespace, fields, UNCONDITIONAL
                )
            )
            assert outcome == "written"
        empties = {key for key, meta in _all_metas(client).items() if not meta.description and meta.description_status is None}
        assert empties == set(_EMPTY_IN_ORDER)
    return created


def _assert_untouched(state: dict[tuple[str, str], _Described]) -> None:
    """The described, manual and failed collections read exactly as seeded."""
    assert state[(DEFAULT_NAMESPACE, _DESCRIBED)] == (_EXISTING_TEXT, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
    assert state[(_TEAM_B, _MANUAL)] == (_MANUAL_TEXT, DESCRIPTION_SOURCE_MANUAL, DESCRIPTION_STATUS_OK)
    assert state[(_TEAM_A, _FAILED)].description == ""
    assert state[(_TEAM_A, _FAILED)].status == DESCRIPTION_STATUS_FAILED


def _assert_empties_filled_each_with_its_own_text(state: dict[tuple[str, str], _Described]) -> None:
    for namespace, name in _EMPTY_IN_ORDER:
        assert state[(namespace, name)] == (
            _reply_for(namespace, name),
            DESCRIPTION_SOURCE_GENERATED,
            DESCRIPTION_STATUS_OK,
        )


def _backfill_infos(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == description_backfill.__name__ and record.levelno == logging.INFO
    ]


def _unreachable_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == description_backfill.__name__
        and record.levelno >= logging.WARNING
        and "not reachable" in record.getMessage()
    ]


def _backfill_done(client: TestClient) -> bool:
    task = client.app.state._description_backfill_task
    assert task is not None
    return task.done()


def test_e2e_backfill_fills_only_unfailed_empty_collections_across_namespaces(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    """S33: after a real startup sync and the probe, the empty collections of every namespace are described one call at a
    time, namespace then name; the described, manual and failed ones are not asked about, and each namespace reads its own."""
    real_sync = app_module._run_startup_sync

    with _RecordingServer(reply_text=_REPLY) as server, caplog.at_level(logging.INFO):

        async def recording_sync(*args: Any, **kwargs: Any) -> None:
            await real_sync(*args, **kwargs)
            # Stay open until the probe has answered and the client's window lookup has ended, then a margin more: a
            # backfill that did not wait for the sync would have started its first call by then.
            app_state = args[1]  # (job_store, app_state, collections)
            while ("probe", "") not in server.timeline or not app_state._llm_window_task.done():
                await asyncio.sleep(_POLL_SECONDS)
            await asyncio.sleep(_SYNC_MARGIN_SECONDS)
            server.timeline.append(("sync-end", ""))

        world.monkeypatch.setattr(app_module, "_run_startup_sync", recording_sync)
        with world.boot(_toml(server.base_url, collections=[world.synced_directory])) as (client, admin_key):
            _wait_for(lambda: _backfill_done(client), "the backfill")
            assert client.app.state.sync_result == StartupSyncResult.DONE
            state = _read_state(client, world, admin_key)
        timeline = list(server.timeline)
        asked = server.asked_in_order()
        server.assert_every_prompt_was_attributed()

    kinds = [kind for kind, _ in timeline]
    expected = [_key(namespace, name) for namespace, name in _EMPTY_IN_ORDER]
    assert asked == expected
    # After the sync and the probe: both ended before the first model call.
    assert kinds.count("sync-end") == 1 and kinds.count("probe") == 1
    first_call = kinds.index("start")
    assert kinds.index("sync-end") < first_call and kinds.index("probe") < first_call
    # One at a time: every call was answered before the next one arrived.
    assert kinds[first_call:] == ["start", "end"] * len(expected)
    _assert_empties_filled_each_with_its_own_text(state)
    _assert_untouched(state)
    assert f"{len(expected)} generated, 0 failed" in _backfill_infos(caplog)[-1]


def test_e2e_backfill_runs_in_the_background_and_skips_a_collection_being_described(world: _World) -> None:
    """S33: the server answers while the first call is still held (the backfill runs in the background and the
    collection shows ``pending``); a describe job started meanwhile for a collection the backfill has not reached yet
    leaves that collection asked about exactly once (the lifecycle's single-flight skip, backed by its re-read of the
    row before a queued generation starts)."""
    with _RecordingServer(reply_text=_REPLY, hold_chat=True) as server:
        with world.boot(_toml(server.base_url)) as (client, admin_key):
            _wait_for(lambda: len(server.asked_in_order()) >= 1, "the first model call")
            first_namespace, first_name = _EMPTY_IN_ORDER[0]
            assert client.get("/health").status_code == _HTTP_OK
            assert not _backfill_done(client)
            assert _read_one(client, admin_key, first_name).status == DESCRIPTION_STATUS_PENDING
            response = client.post(f"/collections/{_YANKEE}/describe", headers=_headers(admin_key))
            assert response.status_code == _HTTP_ACCEPTED, response.text
            describe_job = response.json()["job_id"]
            _wait_for(
                lambda: _read_one(client, admin_key, _YANKEE).status == DESCRIPTION_STATUS_PENDING,
                "the describe request to be admitted",
            )
            server.release()
            _wait_for(lambda: _backfill_done(client), "the backfill")
            describe_final: dict[str, Any] = {}

            def describe_ended() -> bool:
                describe_final.update(client.get(f"/jobs/{describe_job}", headers=_headers(admin_key)).json())
                return describe_final["status"] not in _ACTIVE_JOB_STATUSES

            _wait_for(describe_ended, "the describe job")
            assert describe_final["status"] == JobStatus.DONE.value, describe_final
            state = _read_state(client, world, admin_key)
        asked = server.asked_in_order()
        server.assert_every_prompt_was_attributed()

    assert asked[0] == _key(first_namespace, first_name)
    assert sorted(asked) == sorted(_key(namespace, name) for namespace, name in _EMPTY_IN_ORDER)  # each exactly once
    _assert_empties_filled_each_with_its_own_text(state)
    _assert_untouched(state)


@contextlib.contextmanager
def _provider_down(failure: str) -> Iterator[tuple[str, FakeModelServer | None]]:
    """A base URL whose server is down: it answers 503 to the probe's request, or nothing answers there at all (on macOS
    the unlistened loopback port drops the connection attempt, so the probe runs into its own time limit)."""
    if failure == _ANSWERS_503:
        with FakeModelServer(reply_text=_REPLY, status_overrides={_TAGS_PATH: _HTTP_UNAVAILABLE}) as server:
            yield server.base_url, server
    else:
        with unreachable_port() as port:
            yield f"http://127.0.0.1:{port}", None


@pytest.mark.parametrize("failure", [_ANSWERS_503, _NO_ANSWER])
def test_e2e_backfill_with_provider_down_boots_and_skips(
    world: _World, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    """S34: a provider that answers 503 and one that does not answer both leave the server up (``/health``, REST reads),
    the finding is one WARNING, and the backfill ends with its one INFO without a single model call."""
    with _provider_down(failure) as (base_url, server), caplog.at_level(logging.INFO):
        with world.boot(_toml(base_url)) as (client, admin_key):
            assert client.get("/health").status_code == _HTTP_OK
            _wait_for(lambda: _backfill_done(client), "the backfill")
            reachability = client.app.state.description_reachability
            state = _read_state(client, world, admin_key)
        if server is not None:
            assert not server.requests_to(_CHAT_PATH)

    assert reachability is not None and reachability.reachable is False
    assert len(_unreachable_warnings(caplog)) == 1
    infos = _backfill_infos(caplog)
    assert len(infos) == 1 and "not reachable" in infos[0] and "suppressed" not in infos[0].lower()
    # No attempt was made, so no collection carries a status either (a failed attempt would have stored ``failed``).
    assert all(state[key] == _UNDESCRIBED for key in _EMPTY_IN_ORDER)
    _assert_untouched(state)


def _seed_crashed_ingest_job(world: _World) -> None:
    """Leave what a process killed mid-ingest leaves: a RUNNING ingest job in the jobs file."""
    seeder = JobStore(path=world.tmp_path / JOBS_FILE_NAME)
    job = seeder.create(collection=_YANKEE, path=str(world.synced_directory))
    seeder.update(job.job_id, status=JobStatus.RUNNING)


def _post_sync(client: TestClient, admin_key: str) -> None:
    response = client.post("/sync", headers=_headers(admin_key))
    assert response.status_code == _HTTP_ACCEPTED, response.text
    job_id = response.json()["job_id"]
    finished: dict[str, Any] = {}

    def done() -> bool:
        finished.update(client.get(f"/jobs/{job_id}", headers=_headers(admin_key)).json())
        return finished["status"] not in _ACTIVE_JOB_STATUSES

    _wait_for(done, "the manual sync")
    assert finished["status"] == JobStatus.DONE.value, finished


def test_e2e_backfill_skipped_when_startup_sync_suppressed(world: _World, caplog: pytest.LogCaptureFixture) -> None:
    """S86: a RUNNING ingest job left by a dead process suppresses the startup sync, so the backfill is skipped with one
    INFO and no prompt and no probe reach the provider; after a clean ``POST /sync`` the next start backfills as usual."""
    _seed_crashed_ingest_job(world)

    def toml_for(server: FakeModelServer) -> str:
        return _toml(server.base_url, collections=[world.synced_directory])

    with _RecordingServer(reply_text=_REPLY) as server, caplog.at_level(logging.INFO):
        with world.boot(toml_for(server)) as (client, admin_key):
            assert client.app.state.sync_result == StartupSyncResult.SUPPRESSED
            assert get_sync_suppressed_file().exists()
            _wait_for(lambda: _backfill_done(client), "the backfill task")
            state = _read_state(client, world, admin_key)
            # The clean sync that resumes the operator: it clears the suppression, but the skipped run stays skipped.
            _post_sync(client, admin_key)
            assert client.app.state.sync_result == StartupSyncResult.DONE
            assert not get_sync_suppressed_file().exists()
        assert server.timeline == []  # no probe, no model call
        # The only thing the server saw is the context-window lookup of the client's own startup (no prompt, no probe).
        assert {request.path for request in server.requests} <= {_SHOW_PATH}
        assert all(state[key] == _UNDESCRIBED for key in _EMPTY_IN_ORDER)
        _assert_untouched(state)
        infos = _backfill_infos(caplog)
        assert len(infos) == 1 and "suppressed" in infos[0].lower()

        # The next start: the sentinel is gone and the crashed job row is terminal, so the sync and the backfill run.
        caplog.clear()
        with world.boot(toml_for(server)) as (client, admin_key):
            assert client.app.state.sync_result == StartupSyncResult.DONE
            _wait_for(lambda: _backfill_done(client), "the backfill")
            state = _read_state(client, world, admin_key)
        asked = server.asked_in_order()
        server.assert_every_prompt_was_attributed()

    assert asked == [_key(namespace, name) for namespace, name in _EMPTY_IN_ORDER]
    _assert_empties_filled_each_with_its_own_text(state)
    _assert_untouched(state)
    assert not any("suppressed" in info.lower() for info in _backfill_infos(caplog))


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # the pid exists and belongs to someone else
        return True
    return True


def _kill_leftovers(pids: list[int]) -> None:
    """Make sure a failed test leaves no fake ``claude`` (and no descendant) running for its full hang time."""
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def test_e2e_shutdown_mid_backfill_cancels_and_kills_claude_child(
    world: _World, request: pytest.FixtureRequest
) -> None:
    """S35: with ``claude_cli`` as the provider and its child held mid-call, shutting the app down cancels the backfill,
    kills and reaps the child before the stores disconnect (its descendant is gone by the end of the shutdown), never
    goes on to a second collection and leaves every description unwritten."""
    fake = _fake_claude.install(
        world.tmp_path / "bin", reply=_REPLY, hang_seconds=_FAKE_CLAUDE_HANG_SECONDS, spawn_grandchild=True
    )
    world.monkeypatch.setenv("PATH", f"{fake.path.parent}{os.pathsep}{os.environ['PATH']}")
    spawned: list[int] = []
    request.addfinalizer(lambda: _kill_leftovers(spawned))
    at_disconnect: dict[str, bool] = {}

    with world.boot(_claude_toml()) as (client, _admin_key):
        _wait_for(lambda: len(fake.invocations()) >= 1, "the claude child to start")
        invocation = fake.invocations()[0]
        spawned.append(invocation.pid)
        assert invocation.grandchild_pid is not None
        spawned.append(invocation.grandchild_pid)
        assert _process_alive(invocation.pid), "the claude child must be running before the shutdown"
        assert _process_alive(invocation.grandchild_pid), "its descendant must be running before the shutdown"
        first_namespace, first_name = _EMPTY_IN_ORDER[0]
        assert _marker(first_namespace, first_name) in invocation.stdin, "the first collection in order is asked about"
        backfill = client.app.state._description_backfill_task
        lifecycle = client.app.state.description_lifecycle
        assert not backfill.done()
        assert lifecycle.is_pending(first_name)

        store = client.app.state.search_store
        real_disconnect = store.disconnect

        async def recording_disconnect(*args: Any, **kwargs: Any) -> Any:
            at_disconnect["generation_pending"] = lifecycle.is_pending(first_name)
            at_disconnect["child_alive"] = _process_alive(invocation.pid)
            return await real_disconnect(*args, **kwargs)

        store.disconnect = recording_disconnect

    # The lifespan ended with the call still held: the run was cancelled, and the generation was cancelled and the child
    # killed and reaped before the stores disconnected.
    assert backfill.cancelled()
    assert at_disconnect == {"generation_pending": False, "child_alive": False}
    assert not _process_alive(invocation.pid)
    gone_by = time.monotonic() + _PROCESS_GONE_SECONDS
    while _process_alive(invocation.grandchild_pid):  # not the child's own: another process reaps it
        assert time.monotonic() < gone_by, "the descendant of the claude child outlived the shutdown"
        time.sleep(_POLL_SECONDS)
    spawned.clear()  # both are gone and reaped: the finalizer must not signal a recycled pid
    assert len(fake.invocations()) == 1  # the loop never went on to another collection

    with world.boot(_DISABLED_TOML) as (client, admin_key):
        state = _read_state(client, world, admin_key)
        metas = _all_metas(client)
    for key in _EMPTY_IN_ORDER:
        assert state[key] == _UNDESCRIBED, f"{key}: half-written description {state[key]}"
        assert metas[key].last_described is None and metas[key].description_embedding is None
    _assert_untouched(state)
