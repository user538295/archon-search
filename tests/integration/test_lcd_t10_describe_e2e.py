"""T-10 (collection-descriptions plan, S27, S29, S30, S31, S32, S58, S59, S80, S85, S87) — ``describe`` and ``describe --all`` end to end.

Real app lifespan, real LanceDB and a ``FakeModelServer`` as the provider. REST goes through the TestClient; the CLI
(``collection describe``) goes through the Click runner bridged to the app (``tests/_cli_testclient_bridge.py``), so the
exit codes and the printed text are the ones a user sees. A model call is parked with ``FakeModelServer.hold()`` wherever
a test must act while a generation is in flight (a second caller, a manual set, a delete, the shutdown).

Run with: ``uv run pytest tests/integration/test_lcd_t10_describe_e2e.py -m integration``.

Two things to know when reading a failure here: the bridged CLI runs on the caller's thread while the app runs on the
TestClient's portal thread, so a CLI call that waits for a held model call is run on a worker thread; and the
``unreachable_port()`` of a "provider down" test is a socket that never listens: on macOS a connection attempt to it is
dropped instead of refused, so each failing call lasts about 8 s (measured) and those arms keep the number of failing
calls small.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import secrets
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from click.testing import Result
from fastapi.testclient import TestClient

from archon_search.cli import _helpers as cli_helpers
from archon_search.cli.collection import collection as collection_command
from archon_search.collection_meta import (
    DESCRIPTION_SOURCE_GENERATED,
    DESCRIPTION_SOURCE_MANUAL,
    DESCRIPTION_STATUS_FAILED,
    DESCRIPTION_STATUS_OK,
)
from archon_search.constants import (
    DEFAULT_NAMESPACE,
    DESCRIBE_MANUAL_DETAIL,
    DESCRIBE_NOT_CONFIGURED_DETAIL,
)
from archon_search.description_lifecycle import DescriptionLifecycle
from archon_search.server import routes_collections
from archon_search.server.schemas import WarmupResult
from archon_search.store import SearchStore
from archon_search.sync import path_to_collection_name
from archon_search.types import JobStatus
from tests._cli_testclient_bridge import CliBridge, cli_against_app
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "describe-e2e"
_MODEL = "description-model"
_CHAT_PATH = "/api/chat"
_CONFIGURED_WINDOW = 8192
_PROVIDER_TIMEOUT_SECONDS = 120  # longer than any wait of these tests: a slow runner must not turn a held call into `failed`
_DOWN_PROVIDER_TIMEOUT_SECONDS = 10  # the first call imports the `ollama` package inside the call timeout
_WAIT_SECONDS = 30.0
_THREAD_WAIT_SECONDS = 15.0  # a CLI call on a worker thread that should end quickly; shorter than _WAIT_SECONDS so a hang fails first
_STAY_STILL_SECONDS = 0.5  # how long "no second model call arrives" is watched for
_POLL_SECONDS = 0.01
_CLI_POLL_SECONDS = 0.05  # the CLI waits 2 s between job polls; the tests do not need that
_JOB_LIST_LIMIT = 100
_INGEST_REPLY = "Notes written when the collection was first ingested."
_FIRST_REPLY = "Notes written by the first describe run."
_SECOND_REPLY = "Notes written by the second describe run."
_THIRD_REPLY = "Notes written by the third describe run."
_FOURTH_REPLY = "Notes written by the fourth describe run."
_MANUAL_TEXT = "Hand written note that generation must never replace."
_TEAM = "team-a"
_FOREIGN_TEAM = "team-b"
_EMPTY_TEAM = "team-empty"
_NOT_FOUND = "not found"
_SHUTTING_DOWN = "the server is shutting down"
_OUTCOME_GENERATED = "generated"
_OUTCOME_FAILED = "failed"
_OUTCOME_ALREADY_RUNNING = "alreadyRunning"
_OUTCOME_REFUSED_MANUAL = "refusedManual"
_OUTCOME_SUPERSEDED = "superseded"
_STATUS_PENDING = "pending"
_UNKNOWN_COLLECTION = "no-such-collection"
_DESCRIBE_JOB_TYPE = "description"
_RUNNING = JobStatus.RUNNING.value
_DONE = JobStatus.DONE.value
_FAILED = JobStatus.FAILED.value
_ACTIVE_JOB_STATUSES = frozenset({JobStatus.PENDING.value, JobStatus.QUEUED.value, JobStatus.RUNNING.value})
_SKIP_MANUAL = "manual description"
_SKIP_DESCRIBED = "already described (status is not failed)"
_SKIP_NO_DOCUMENTS = "no documents"
_NOTHING_STORED = ("", None, None)


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


@pytest.fixture(autouse=True)
def _quick_cli_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_helpers, "_POLL_INTERVAL_SECONDS", _CLI_POLL_SECONDS)


def _description_toml(base_url: str, *, timeout_seconds: int = _PROVIDER_TIMEOUT_SECONDS, enabled: bool = True) -> str:
    return (
        "[description]\n"
        f"enabled = {str(enabled).lower()}\n"
        'provider = "ollama"\n'
        f'model = "{_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"ollama_context_tokens = {_CONFIGURED_WINDOW}\n"
        f"timeout_seconds = {timeout_seconds}\n"
    )


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"{what} did not happen within {_WAIT_SECONDS}s"
        time.sleep(_POLL_SECONDS)


def _await_ready(client: TestClient) -> None:
    """The warm-up and the window resolution are over: a generation that starts earlier sizes itself on the assumed window."""
    _wait_for(
        lambda: getattr(client.app.state, "warmup_result", None) not in (None, WarmupResult.PENDING),
        "the lifespan warm-up",
    )
    assert client.app.state.warmup_result == WarmupResult.DONE
    _wait_for(client.app.state._llm_window_task.done, "the context-window resolution")


@contextlib.contextmanager
def _booted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    toml: str,
    *,
    namespaces: dict[str, str] | None = None,
) -> Iterator[tuple[TestClient, str]]:
    """The real app, ready; yields the client and the admin key."""
    with make_real_app(tmp_path, monkeypatch, toml_content=toml, namespaces=namespaces) as (client, _config, admin_key):
        _await_ready(client)
        yield client, admin_key


@contextlib.contextmanager
def _releasing(server: FakeModelServer) -> Iterator[None]:
    """Open the gate of a held model call when the block ends, so a worker thread still waiting on it can finish."""
    try:
        yield
    finally:
        server.release()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _document(marker: str) -> str:
    return f"Field notes about {marker} and how the survey was carried out across the valley.\n" * 3


def _write_corpus(directory: Path, marker: str, count: int = 3) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"{marker}-{index}.txt").write_text(_document(f"{marker}-{index}"), encoding="utf-8")
    return directory


def _detail(client: TestClient, token: str, name: str = _COLLECTION) -> dict[str, Any]:
    response = client.get(f"/collections/{name}", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def _state(client: TestClient, token: str, name: str = _COLLECTION) -> tuple[str, str | None, str | None]:
    """The description, its source and its status, as the REST detail reports them."""
    detail = _detail(client, token, name)
    return detail["description"], detail["description_source"], detail["description_status"]


def _status(client: TestClient, token: str, name: str = _COLLECTION) -> str | None:
    return _state(client, token, name)[2]


def _chat_count(server: FakeModelServer) -> int:
    return len(server.requests_to(_CHAT_PATH))


def _await_chats(server: FakeModelServer, count: int) -> None:
    _wait_for(lambda: _chat_count(server) >= count, f"chat request number {count}")


def _assert_no_chat_beyond(server: FakeModelServer, count: int, why: str) -> None:
    """Watch for a while that no further model request arrives (a negative can only be bounded in time)."""
    deadline = time.monotonic() + _STAY_STILL_SECONDS
    while time.monotonic() < deadline:
        assert _chat_count(server) == count, why
        time.sleep(_POLL_SECONDS)


def _lifecycle(client: TestClient) -> DescriptionLifecycle:
    return client.app.state.description_lifecycle


def _seed(client: TestClient, token: str, directory: Path, name: str) -> None:
    """Ingest a corpus directory under *name* and wait until the generation the ingest asked for has stored its verdict."""
    ingest_file_via_path(client, name, str(directory), api_key=token)
    _wait_for(
        lambda: _status(client, token, name) in (DESCRIPTION_STATUS_OK, DESCRIPTION_STATUS_FAILED),
        f"the stored description status of {name}",
    )


def _clear(client: TestClient, token: str, name: str = _COLLECTION) -> None:
    response = client.delete(f"/collections/{name}/description", headers=_headers(token))
    assert response.status_code == 200, response.text


def _set_manual(client: TestClient, token: str, name: str = _COLLECTION, text: str = _MANUAL_TEXT) -> None:
    response = client.put(f"/collections/{name}/description", json={"description": text}, headers=_headers(token))
    assert response.status_code == 200, response.text


def _job(client: TestClient, token: str, job_id: str) -> dict[str, Any]:
    response = client.get(f"/jobs/{job_id}", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def _terminal_job(client: TestClient, token: str, job_id: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}

    def terminal() -> bool:
        latest.update(_job(client, token, job_id))
        return latest["status"] not in _ACTIVE_JOB_STATUSES

    _wait_for(terminal, f"job {job_id} to end")
    return latest


def _post_describe(client: TestClient, token: str, name: str = _COLLECTION) -> Any:
    return client.post(f"/collections/{name}/describe", headers=_headers(token))


def _start_describe(client: TestClient, token: str, name: str = _COLLECTION) -> str:
    response = _post_describe(client, token, name)
    assert response.status_code == 202, response.text
    job = response.json()
    assert (job["job_type"], job["status"], job["collection"]) == (_DESCRIBE_JOB_TYPE, _RUNNING, name)
    return job["job_id"]


def _describe_jobs(client: TestClient, token: str) -> list[dict[str, Any]]:
    response = client.get("/jobs", params={"kind": _DESCRIBE_JOB_TYPE, "limit": _JOB_LIST_LIMIT}, headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _outcome(job: dict[str, Any]) -> tuple[str, str]:
    """The ``(outcome, description_status)`` of a describe job that ended ``DONE``."""
    assert job["status"] == _DONE, job
    result = job["result"]
    assert isinstance(result, dict), job
    return result["outcome"], result["description_status"]


def _cli(cli: CliBridge, *args: str, token: str | None = None) -> Result:
    return cli.invoke(collection_command, list(args), token=token)


def _info_fields(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, value = line.partition(": ")
        if separator:
            fields[name] = value
    return fields


def _skipped_line(name: str, reason: str) -> str:
    return f"Skipped '{name}': {reason}."


def test_e2e_describe_generates_then_forces_fresh_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S29, S58: a first run fills an empty description; the next runs replace the generated one although the collection
    did not change, so the 20% rule would never qualify them (two through the CLI, one with --wait and one without, and
    one through REST)."""
    corpus = _write_corpus(tmp_path / "corpus", "ridge")
    with (
        FakeModelServer(reply_text=_INGEST_REPLY) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, key),
        cli_against_app(client, key, monkeypatch) as cli,
    ):
        _seed(client, key, corpus, _COLLECTION)
        assert _state(client, key) == (_INGEST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
        _clear(client, key)
        assert _state(client, key) == _NOTHING_STORED
        chats_after_seed = _chat_count(server)

        server.reply_text = _FIRST_REPLY
        first = _cli(cli, "describe", _COLLECTION, "--wait")
        assert first.exit_code == 0, first.output
        assert f"outcome {_OUTCOME_GENERATED}" in first.output
        assert f"description: {_FIRST_REPLY}" in first.output
        assert f"description_source: {DESCRIPTION_SOURCE_GENERATED}" in first.output
        assert _state(client, key) == (_FIRST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
        assert _chat_count(server) == chats_after_seed + 1

        server.reply_text = _SECOND_REPLY
        second = _cli(cli, "describe", _COLLECTION, "--wait")
        assert second.exit_code == 0, second.output
        assert f"description: {_SECOND_REPLY}" in second.output
        assert _state(client, key) == (_SECOND_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
        assert _chat_count(server) == chats_after_seed + 2

        server.reply_text = _THIRD_REPLY
        third_job = _start_describe(client, key)
        assert _outcome(_terminal_job(client, key, third_job)) == (_OUTCOME_GENERATED, DESCRIPTION_STATUS_OK)
        assert _state(client, key) == (_THIRD_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
        assert _chat_count(server) == chats_after_seed + 3

        # Without --wait the command returns at once with the job id (exit 0); the job still ends DONE and fills the text.
        server.reply_text = _FOURTH_REPLY
        started = _cli(cli, "describe", _COLLECTION)
        assert started.exit_code == 0, started.output
        assert "Describe job started: " in started.output
        _wait_for(lambda: _state(client, key)[0] == _FOURTH_REPLY, "the fourth text, from the job the command started")

        _wait_for(
            lambda: {job["status"] for job in _describe_jobs(client, key)} == {_DONE}, "the describe jobs to end DONE"
        )
        assert [job["collection"] for job in _describe_jobs(client, key)] == [_COLLECTION] * 4
        info = _cli(cli, "info", _COLLECTION)
        assert info.exit_code == 0, info.output
        assert _info_fields(info.output)["description"] == _FOURTH_REPLY


def test_e2e_describe_all_on_mixed_states_fills_only_empty_or_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S31, S58, S59: manual, described-ok, failed (with an old text), empty and no-documents collections; ``--all`` fills
    exactly the failed and the empty one, lists the other three as skipped and exits 0."""
    empty_directory = tmp_path / "no-documents"
    empty_directory.mkdir()
    toml_collections = f"[collections]\ncollections = {json.dumps([str(empty_directory)])}\n"
    nodocs = path_to_collection_name(str(empty_directory))
    manual, described, failed, empty = "manual-c", "ok-c", "failed-c", "empty-c"
    with FakeModelServer(reply_text=_INGEST_REPLY) as server:
        toml = toml_collections + _description_toml(server.base_url)
        with (
            _booted(tmp_path, monkeypatch, toml) as (client, key),
            cli_against_app(client, key, monkeypatch) as cli,
        ):
            for name in (manual, described, failed, empty):
                _seed(client, key, _write_corpus(tmp_path / f"corpus-{name}", name), name)
            _set_manual(client, key, manual)
            _clear(client, key, empty)
            server.reply_text = ""  # nothing usable: the describe run fails and stores status `failed`, the old text stays
            failed_job = _start_describe(client, key, failed)
            assert _outcome(_terminal_job(client, key, failed_job)) == (_OUTCOME_FAILED, DESCRIPTION_STATUS_FAILED)

            assert _state(client, key, manual) == (_MANUAL_TEXT, DESCRIPTION_SOURCE_MANUAL, DESCRIPTION_STATUS_OK)
            assert _state(client, key, described) == (_INGEST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
            assert _state(client, key, failed) == (_INGEST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_FAILED)
            assert _state(client, key, empty) == _NOTHING_STORED
            listed = {item["name"]: item for item in client.get("/collections/", headers=_headers(key)).json()}
            assert listed[nodocs]["doc_count"] == 0, "the no-documents collection is listed with no documents"

            server.reply_text = _FIRST_REPLY
            jobs_before = {job["job_id"] for job in _describe_jobs(client, key)}
            chats_before = _chat_count(server)
            result = _cli(cli, "describe", "--all")

            assert result.exit_code == 0, result.output
            assert _skipped_line(manual, _SKIP_MANUAL) in result.output
            assert _skipped_line(described, _SKIP_DESCRIBED) in result.output
            assert _skipped_line(nodocs, _SKIP_NO_DOCUMENTS) in result.output
            assert "Summary: filled 2, already running 0, skipped 3, failed 0." in result.output
            assert _chat_count(server) == chats_before + 2, "one model call for each filled collection and none for the rest"

            assert _state(client, key, failed) == (_FIRST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
            assert _state(client, key, empty) == (_FIRST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
            assert _state(client, key, manual) == (_MANUAL_TEXT, DESCRIPTION_SOURCE_MANUAL, DESCRIPTION_STATUS_OK)
            assert _state(client, key, described) == (_INGEST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
            new_jobs = [job for job in _describe_jobs(client, key) if job["job_id"] not in jobs_before]
            assert sorted(job["collection"] for job in new_jobs) == sorted([failed, empty])
            assert {job["status"] for job in new_jobs} == {_DONE}
            for name in (failed, empty):
                assert f"Describing '{name}' (job " in result.output

            # A second run has nothing left to fill: no job, no model call, exit 0.
            chats_before = _chat_count(server)
            again = _cli(cli, "describe", "--all")
            assert again.exit_code == 0, again.output
            assert "Summary: filled 0, already running 0, skipped 5, failed 0." in again.output
            assert _chat_count(server) == chats_before


def test_e2e_describe_not_configured_409_and_provider_down_job_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S32, S58: descriptions not configured (section absent, or ``enabled = false``) answer 409 with a setup message and
    create no job, and ``describe --all`` stops at the first such 409 (two collections, the message once, no job); a
    configured provider that is down gives a job that ends DONE with outcome ``failed`` and an exit code of 1, not a 409.
    The three boots share one store, so the same two collections are addressed each time."""
    corpus = _write_corpus(tmp_path / "corpus", "ridge")
    second_corpus = _write_corpus(tmp_path / "corpus-second", "dune")
    second_collection = "describe-e2e-second"
    with FakeModelServer() as unused_server:
        arms = {"section absent": "", "disabled": _description_toml(unused_server.base_url, enabled=False)}
        for label, toml in arms.items():
            with (
                _booted(tmp_path, monkeypatch, toml) as (client, key),
                cli_against_app(client, key, monkeypatch) as cli,
            ):
                ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=key)
                ingest_file_via_path(client, second_collection, str(second_corpus), api_key=key)
                response = _post_describe(client, key)
                assert response.status_code == 409, f"{label}: {response.text}"
                assert response.json() == {"detail": DESCRIBE_NOT_CONFIGURED_DETAIL}, label
                assert _describe_jobs(client, key) == [], f"{label}: a 409 creates no job"

                single = _cli(cli, "describe", _COLLECTION, "--wait")
                assert single.exit_code == 1, f"{label}: {single.output}"
                assert DESCRIBE_NOT_CONFIGURED_DETAIL in single.output
                assert "clear-description" not in single.output, "the setup message is not the manual-description message"
                everything = _cli(cli, "describe", "--all")
                assert everything.exit_code == 1, f"{label}: {everything.output}"
                assert everything.output.count(DESCRIBE_NOT_CONFIGURED_DETAIL) == 1, "the setup message is printed once"
                assert _describe_jobs(client, key) == [], f"{label}: the CLI created no job either"
        assert unused_server.requests == [], "a server that was never configured was never asked"

    with (
        unreachable_port() as dead_port,
        _booted(
            tmp_path,
            monkeypatch,
            _description_toml(f"http://127.0.0.1:{dead_port}", timeout_seconds=_DOWN_PROVIDER_TIMEOUT_SECONDS),
        ) as (client, key),
        cli_against_app(client, key, monkeypatch) as cli,
    ):
        for name in (_COLLECTION, second_collection):
            assert _state(client, key, name) == _NOTHING_STORED, "no description, and the provider is down"

        everything = _cli(cli, "describe", "--all")  # both are filled, and both fail
        assert everything.exit_code == 1, everything.output
        assert everything.output.count(f"outcome {_OUTCOME_FAILED}") == 2
        assert "Summary: filled 0, already running 0, skipped 0, failed 2." in everything.output
        assert DESCRIBE_NOT_CONFIGURED_DETAIL not in everything.output
        jobs = _describe_jobs(client, key)
        assert sorted(job["collection"] for job in jobs) == sorted([_COLLECTION, second_collection])
        for job in jobs:  # the job itself did not break: DONE, not FAILED
            assert _outcome(job) == (_OUTCOME_FAILED, DESCRIPTION_STATUS_FAILED)
        for name in (_COLLECTION, second_collection):
            assert _state(client, key, name) == ("", None, DESCRIPTION_STATUS_FAILED)


def test_e2e_two_callers_one_generation_and_manual_set_between_409_check_and_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S80, S59: a second caller of a collection whose generation is in flight settles ``alreadyRunning`` (exit 0) and
    asks the model nothing; a second collection waits for the process slot; a manual description set after the 409 check
    passed, while the model call is in flight, makes the job settle ``refusedManual`` (exit 1) and survive."""
    first_key, second_key = secrets.token_hex(32), secrets.token_hex(32)
    namespaces = {first_key: _TEAM, second_key: _TEAM}  # two callers of one namespace
    other_collection = "describe-e2e-second"
    with (
        FakeModelServer(reply_text=_INGEST_REPLY) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url), namespaces=namespaces) as (client, _admin),
        cli_against_app(client, first_key, monkeypatch) as cli,
        ThreadPoolExecutor(max_workers=2) as executor,  # inside the bridge, outside the guard: joined while the CLI still works
        _releasing(server),
    ):
        _seed(client, first_key, _write_corpus(tmp_path / "corpus-one", "ridge"), _COLLECTION)
        _seed(client, first_key, _write_corpus(tmp_path / "corpus-two", "dune"), other_collection)
        seeded = _chat_count(server)
        lifecycle = _lifecycle(client)

        server.reply_text = _FIRST_REPLY
        server.hold()
        first_job = _start_describe(client, first_key)
        _await_chats(server, seeded + 1)
        assert _status(client, first_key) == _STATUS_PENDING

        second_caller = executor.submit(lambda: _cli(cli, "describe", _COLLECTION, "--wait", token=second_key))
        second = second_caller.result(timeout=_THREAD_WAIT_SECONDS)  # a second generation would wait for the held slot
        assert second.exit_code == 0, second.output
        assert f"outcome {_OUTCOME_ALREADY_RUNNING}" in second.output
        assert "description_source:" not in second.output, "nothing was generated for the second caller"
        assert _chat_count(server) == seeded + 1

        third_job = _start_describe(client, first_key, other_collection)
        _wait_for(lambda: lifecycle.is_pending(other_collection), "the second collection to be admitted")
        _assert_no_chat_beyond(server, seeded + 1, "one generation at a time in the process: the second waits for the slot")
        assert _job(client, first_key, third_job)["status"] == _RUNNING

        server.release()
        assert _outcome(_terminal_job(client, first_key, first_job)) == (_OUTCOME_GENERATED, DESCRIPTION_STATUS_OK)
        assert _outcome(_terminal_job(client, first_key, third_job)) == (_OUTCOME_GENERATED, DESCRIPTION_STATUS_OK)
        assert _chat_count(server) == seeded + 2, "two collections, two model calls: the second caller added none"
        assert _state(client, first_key) == (_FIRST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
        finished = [_terminal_job(client, first_key, item["job_id"]) for item in _describe_jobs(client, first_key)]
        assert sorted(_outcome(job)[0] for job in finished) == sorted(
            [_OUTCOME_GENERATED, _OUTCOME_GENERATED, _OUTCOME_ALREADY_RUNNING]
        )

        # A manual description lands after the 409 check passed and while the model call is in flight.
        server.reply_text = _SECOND_REPLY
        server.hold()
        waiting = executor.submit(lambda: _cli(cli, "describe", _COLLECTION, "--wait", token=first_key))
        _await_chats(server, seeded + 3)
        _set_manual(client, second_key)
        server.release()
        refused = waiting.result(timeout=_THREAD_WAIT_SECONDS)

        assert refused.exit_code == 1, refused.output
        assert f"outcome {_OUTCOME_REFUSED_MANUAL}" in refused.output
        assert _state(client, first_key) == (_MANUAL_TEXT, DESCRIPTION_SOURCE_MANUAL, DESCRIPTION_STATUS_OK)  # S30: unchanged
        refused_jobs = [
            job
            for job in _describe_jobs(client, first_key)
            if job["status"] == _DONE and job["result"]["outcome"] == _OUTCOME_REFUSED_MANUAL
        ]
        assert len(refused_jobs) == 1, "the job is DONE with the refusal as its outcome, not FAILED"

        # From now on the 409 check itself refuses it, with the message that says to clear the description first.
        conflict = _post_describe(client, first_key)
        assert (conflict.status_code, conflict.json()) == (409, {"detail": DESCRIBE_MANUAL_DETAIL})
        message = _cli(cli, "describe", _COLLECTION, token=first_key)
        assert message.exit_code == 1, message.output
        assert f"collection clear-description {_COLLECTION}" in message.output
        assert _chat_count(server) == seeded + 3
        assert _state(client, first_key) == (_MANUAL_TEXT, DESCRIPTION_SOURCE_MANUAL, DESCRIPTION_STATUS_OK)  # S30: unchanged


def test_e2e_running_describe_job_cannot_be_deleted_and_collection_delete_is_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S87: while a describe job runs its owner's ``DELETE /jobs/{id}`` answers 409 and the generation goes on; a
    ``DELETE /collections/{name}`` is not refused, and the generation then writes nothing and recreates nothing."""
    corpus = _write_corpus(tmp_path / "corpus", "ridge")
    with (
        FakeModelServer(reply_text=_INGEST_REPLY) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, key),
    ):
        _seed(client, key, corpus, _COLLECTION)
        seeded = _chat_count(server)
        server.reply_text = _FIRST_REPLY
        server.hold()
        job_id = _start_describe(client, key)
        _await_chats(server, seeded + 1)

        refused = client.delete(f"/jobs/{job_id}", headers=_headers(key))
        assert refused.status_code == 409, refused.text
        assert _job(client, key, job_id)["status"] == _RUNNING, "the generation goes on"

        removed = client.delete(f"/collections/{_COLLECTION}", headers=_headers(key))
        assert removed.status_code == 200, removed.text
        server.release()
        job = _terminal_job(client, key, job_id)

        assert _outcome(job)[0] == _OUTCOME_SUPERSEDED
        assert client.get(f"/collections/{_COLLECTION}", headers=_headers(key)).status_code == 404
        assert _COLLECTION not in [item["name"] for item in client.get("/collections/", headers=_headers(key)).json()]
        stored = client.portal.call(  # type: ignore[union-attr]
            lambda: client.app.state.search_store.get_collection_meta(_COLLECTION, namespace=DEFAULT_NAMESPACE)
        )
        assert stored is None, "the generation did not recreate the collection"


class _StoreCall(NamedTuple):
    method: str
    task_body: str  # qualified name of the coroutine of the asyncio task that made the call
    lifecycle_closed: bool
    store_disconnected: bool


_GENERATION_TASK_BODY = DescriptionLifecycle._run.__qualname__
_DESCRIBE_TASK_BODY = routes_collections._describe_task.__qualname__


def _record_store_calls(monkeypatch: pytest.MonkeyPatch, calls: list[_StoreCall], phase: dict[str, bool]) -> None:
    """Wrap the public coroutines of ``SearchStore`` (but ``disconnect``; the async context managers such as
    ``locked_collection`` are not coroutine functions and show up through the calls inside them) so each call notes the
    task that made it and the shutdown phase it ran in."""

    def recording(name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        async def recorded(self: SearchStore, *args: Any, **kwargs: Any) -> Any:
            task = asyncio.current_task()
            body = task.get_coro().__qualname__ if task is not None else ""
            calls.append(_StoreCall(name, body, phase["closed"], phase["disconnected"]))
            return await method(self, *args, **kwargs)

        return recorded

    for name, method in inspect.getmembers(SearchStore, inspect.iscoroutinefunction):
        if not name.startswith("_") and name != "disconnect":
            monkeypatch.setattr(SearchStore, name, recording(name, method))


def test_e2e_describe_request_during_shutdown_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S85 (describe arm): a describe job whose generation is in flight when the lifespan shuts down ends FAILED with the
    shutting-down text; so does a describe job that the route spawns after the lifecycle closed (a request that raced the
    shutdown: the task starts once the lifespan has taken its snapshot of the tasks to cancel). No second model call is
    made, no store method is called by a describe task or a generation after the lifecycle closed, and no store method
    at all after the store disconnected. The late request is spawned through the route's own ``_spawn_describe_task``
    from a task registered for the purpose: nothing public can place a request exactly there."""
    corpus = _write_corpus(tmp_path / "corpus", "ridge")
    calls: list[_StoreCall] = []
    phase = {"closed": False, "disconnected": False}
    real_close, real_disconnect = DescriptionLifecycle.close, SearchStore.disconnect

    def closing(self: DescriptionLifecycle) -> None:
        phase["closed"] = True
        real_close(self)

    async def disconnecting(self: SearchStore) -> None:
        phase["disconnected"] = True
        await real_disconnect(self)

    with FakeModelServer(reply_text=_INGEST_REPLY) as server:
        with _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, key):
            _seed(client, key, corpus, _COLLECTION)
            seeded = _chat_count(server)
            monkeypatch.setattr(DescriptionLifecycle, "close", closing)
            monkeypatch.setattr(SearchStore, "disconnect", disconnecting)
            _record_store_calls(monkeypatch, calls, phase)

            server.hold()
            in_flight = _start_describe(client, key)
            _await_chats(server, seeded + 1)
            assert any(
                call.method == "list_chunk_ids" and call.task_body == _GENERATION_TASK_BODY for call in calls
            ), "the recorder sees the store calls of the running generation"
            state = client.app.state
            job_store, lifecycle = state.job_store, state.description_lifecycle
            late: dict[str, str] = {}

            async def register_late_asker() -> None:
                async def asks_while_being_cancelled() -> None:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        job = job_store.create_description(_COLLECTION, namespace=DEFAULT_NAMESPACE)
                        late["job_id"] = job.job_id
                        routes_collections._spawn_describe_task(state, job, job_store, lifecycle)
                        raise

                task = asyncio.create_task(asks_while_being_cancelled())
                state._background_tasks.add(task)
                await asyncio.sleep(0)  # let it reach its wait

            client.portal.call(register_late_asker)  # type: ignore[union-attr]

        # The lifespan is over: the stores are disconnected.
        assert phase["closed"] and phase["disconnected"]
        for job_id in (in_flight, late["job_id"]):
            ended = job_store.get(job_id)
            assert ended is not None
            assert (ended.status.value, ended.error) == (_FAILED, routes_collections.DESCRIBE_SHUTTING_DOWN_DETAIL), job_id
        assert _chat_count(server) == seeded + 1, "no model call was made after the one held at the shutdown"
        assert not [call for call in calls if call.store_disconnected], "a store call after the disconnect"
        after_close = [
            call for call in calls if call.lifecycle_closed and call.task_body in (_GENERATION_TASK_BODY, _DESCRIBE_TASK_BODY)
        ]
        assert not after_close, f"a describe task or a generation used the store after the lifecycle closed: {after_close}"
        assert not lifecycle.is_pending(_COLLECTION)


def test_e2e_foreign_namespace_describe_is_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S27 (describe arm): a key of another namespace gets the 404 of an unknown collection, over REST and the CLI, whether
    the collection is empty or manual, and starts nothing; its ``describe --all`` neither lists nor touches the other
    namespace's collections."""
    owner_key, foreign_key, empty_key = (secrets.token_hex(32) for _ in range(3))
    namespaces = {owner_key: _TEAM, foreign_key: _FOREIGN_TEAM, empty_key: _EMPTY_TEAM}
    foreign_collection = "foreign-c"
    with (
        FakeModelServer(reply_text=_INGEST_REPLY) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url), namespaces=namespaces) as (client, _admin),
        cli_against_app(client, owner_key, monkeypatch) as cli,
    ):
        _seed(client, owner_key, _write_corpus(tmp_path / "corpus-owner", "owner-ridge"), _COLLECTION)
        _seed(client, foreign_key, _write_corpus(tmp_path / "corpus-foreign", "foreign-dune"), foreign_collection)
        _clear(client, owner_key)
        _clear(client, foreign_key, foreign_collection)  # both are empty, so any `describe --all` would fill them
        seeded = _chat_count(server)
        server.reply_text = _FIRST_REPLY

        def assert_foreign_refusals(owner_state: tuple[str, str | None, str | None]) -> None:
            """REST and CLI: the foreign key's describe of the owner's collection reads exactly like an unknown name."""
            answers = {name: _post_describe(client, foreign_key, name) for name in (_COLLECTION, _UNKNOWN_COLLECTION)}
            assert {answer.status_code for answer in answers.values()} == {404}
            bodies = {name: answer.text.replace(name, "<name>") for name, answer in answers.items()}
            assert bodies[_COLLECTION] == bodies[_UNKNOWN_COLLECTION]
            assert _NOT_FOUND in answers[_COLLECTION].text.lower()
            refusals = {
                name: _cli(cli, "describe", name, "--wait", token=foreign_key) for name in (_COLLECTION, _UNKNOWN_COLLECTION)
            }
            assert {result.exit_code for result in refusals.values()} == {1}
            texts = {name: result.output.replace(name, "<name>") for name, result in refusals.items()}
            assert texts[_COLLECTION] == texts[_UNKNOWN_COLLECTION]
            assert _NOT_FOUND in refusals[_COLLECTION].output.lower()
            assert _describe_jobs(client, owner_key) == [] and _describe_jobs(client, foreign_key) == []
            assert not _lifecycle(client).is_pending(_COLLECTION)
            assert _chat_count(server) == seeded
            assert _state(client, owner_key) == owner_state

        assert_foreign_refusals(_NOTHING_STORED)  # an empty collection: the foreign key must not be told a job could start
        _set_manual(client, owner_key)
        assert_foreign_refusals((_MANUAL_TEXT, DESCRIPTION_SOURCE_MANUAL, DESCRIPTION_STATUS_OK))  # nor that it is manual (409)
        _clear(client, owner_key)

        # `describe --all` as the foreign key: only its own collection is listed or filled.
        everything = _cli(cli, "describe", "--all", token=foreign_key)
        assert everything.exit_code == 0, everything.output
        assert _COLLECTION not in everything.output
        assert f"Describing '{foreign_collection}'" in everything.output
        assert "Summary: filled 1, already running 0, skipped 0, failed 0." in everything.output
        assert _state(client, foreign_key, foreign_collection) == (
            _FIRST_REPLY,
            DESCRIPTION_SOURCE_GENERATED,
            DESCRIPTION_STATUS_OK,
        )
        assert _state(client, owner_key) == _NOTHING_STORED, "the owner's collection was not touched"
        assert _chat_count(server) == seeded + 1
        sent = json.dumps([request.body for request in server.requests_to(_CHAT_PATH)[seeded:]])
        assert "foreign-dune" in sent and "owner-ridge" not in sent, "only the foreign namespace's text reached the model"
        assert [job["collection"] for job in _describe_jobs(client, foreign_key)] == [foreign_collection]
        assert _describe_jobs(client, owner_key) == []

        # A namespace with no collection has nothing to describe.
        nothing = _cli(cli, "describe", "--all", token=empty_key)
        assert nothing.exit_code == 0, nothing.output
        assert "No collections found." in nothing.output
        assert _chat_count(server) == seeded + 1
