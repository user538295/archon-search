"""BE-34 (collection-descriptions plan, C6, C10, S27, S29, S32, S58, S59, S80, S85, S87): ``POST /collections/{name}/describe``.

A real app (``make_real_app``) boots with ``[description]`` on a loopback fake model server (or on a port nothing
listens on). The route answers 202 with a RUNNING ``description`` job that a tracked background task runs through the
lifecycle's forced entry; the job ends ``DONE`` with ``{outcome, description_status}``, or ``FAILED`` only when it broke
(here: the server shutting down).
"""

from __future__ import annotations

import contextlib
import secrets
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.constants import (
    DESCRIBE_MANUAL_DETAIL,
    DESCRIBE_NOT_CONFIGURED_DETAIL,
)
from archon_search.server.routes_collections import DESCRIBE_SHUTTING_DOWN_DETAIL
from archon_search.server.routes_jobs import INTERNAL_ERROR_DETAIL
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "describe-me"
_FIRST_REPLY = "The first description the ingest wrote."
_FORCED_REPLY = "The description the describe job wrote."
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_DOCUMENT_REPEATS = 20
_MANUAL_TEXT = "Hand written note about the describe documents."
_FOREIGN_NAMESPACE = "outsider"


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _toml(base_url: str, *, enabled: bool = True) -> str:
    return (
        "[description]\n"
        f"enabled = {str(enabled).lower()}\n"
        'provider = "ollama"\n'
        'model = "description-model"\n'
        f'ollama_base_url = "{base_url}"\n'
        "ollama_context_tokens = 8192\n"
    )


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} did not happen within {_WAIT_SECONDS}s")
        time.sleep(_POLL_SECONDS)


def _await_window_resolution(client: TestClient) -> None:
    task = client.app.state._llm_window_task
    _wait_for(task.done, "the context window resolution")


def _ingest_corpus(client: TestClient, tmp_path: Path, api_key: str) -> None:
    document = tmp_path / "corpus" / "notes.txt"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text("Archon search keeps collections of local documents searchable.\n" * _DOCUMENT_REPEATS, encoding="utf-8")
    ingest_file_via_path(client, _COLLECTION, str(document.parent), api_key=api_key)


def _meta(client: TestClient, collection: str = _COLLECTION):
    return client.portal.call(client.app.state.search_store.get_collection_meta, collection)  # type: ignore[union-attr]


def _idle(client: TestClient) -> bool:
    return not client.app.state.description_lifecycle.is_pending(_COLLECTION)


def _describe(client: TestClient, api_key: str, collection: str = _COLLECTION):
    return client.post(f"/collections/{collection}/describe", headers=_auth(api_key))


def _settled_job(client: TestClient, api_key: str, job_id: str) -> dict:
    _wait_for(lambda: client.get(f"/jobs/{job_id}", headers=_auth(api_key)).json()["status"] != "RUNNING", "the describe job")
    return client.get(f"/jobs/{job_id}", headers=_auth(api_key)).json()


@pytest.fixture
def booted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., tuple[TestClient, str]]]:
    """Boot a real app on a ``[description]`` TOML, ingest the corpus and wait until the ingest's own generation is over."""
    with contextlib.ExitStack() as stack:

        def _boot(toml: str, *, ingest: bool = True, **kwargs: object) -> tuple[TestClient, str]:
            client, _config, api_key = stack.enter_context(make_real_app(tmp_path, monkeypatch, toml_content=toml, **kwargs))
            _await_window_resolution(client)
            if ingest:
                _ingest_corpus(client, tmp_path, api_key)
                if client.app.state.description_lifecycle is not None:
                    _wait_for(lambda: _idle(client), "the ingest's generation to settle")
            return client, api_key

        yield _boot


def test_describe_returns_202_and_job_settles_done_with_outcome(booted) -> None:
    """S29, S58: 202 with the sibling ``JobResponse`` of a RUNNING ``description`` job; it settles ``DONE`` with the outcome
    and the stored status, and a generated description is replaced even though the ingest rule would not fire."""
    with FakeModelServer(reply_text=_FIRST_REPLY) as server:
        client, api_key = booted(_toml(server.base_url))
        assert _meta(client).description == _FIRST_REPLY
        server.reply_text = _FORCED_REPLY

        response = _describe(client, api_key)

        assert response.status_code == 202
        accepted = response.json()
        assert (accepted["status"], accepted["job_type"], accepted["kind"], accepted["collection"]) == (
            "RUNNING",
            "description",
            None,
            _COLLECTION,
        )
        job = _settled_job(client, api_key, accepted["job_id"])
        listed = client.get("/jobs", params={"kind": "description"}, headers=_auth(api_key)).json()

    assert (job["status"], job["result"], job["error"]) == ("DONE", {"outcome": "generated", "description_status": "ok"}, None)
    assert [item["job_id"] for item in listed["items"]] == [accepted["job_id"]]
    meta = _meta(client)
    assert (meta.description, meta.description_source, meta.description_status) == (_FORCED_REPLY, "generated", "ok")


def test_describe_not_configured_returns_409_setup_message_and_creates_no_job(booted) -> None:
    """S32: descriptions off (``enabled = false``): 409 whose ``detail`` is the setup message; no job exists."""
    with FakeModelServer() as server:
        client, api_key = booted(_toml(server.base_url, enabled=False))
        assert client.app.state.description_lifecycle is None

        response = _describe(client, api_key)

        jobs_after = client.get("/jobs", params={"kind": "description"}, headers=_auth(api_key)).json()

    assert (response.status_code, response.json()) == (409, {"detail": DESCRIBE_NOT_CONFIGURED_DETAIL})
    assert "[description]" in DESCRIBE_NOT_CONFIGURED_DETAIL
    assert "enabled = true" in DESCRIBE_NOT_CONFIGURED_DETAIL
    assert jobs_after["items"] == []
    assert server.requests_to("/api/chat") == []


def test_describe_over_manual_description_returns_409_and_creates_no_job(booted) -> None:
    """S59: a manual description is refused with 409 (clear it first), before any job is created."""
    with FakeModelServer(reply_text=_FIRST_REPLY) as server:
        client, api_key = booted(_toml(server.base_url))
        assert client.put(
            f"/collections/{_COLLECTION}/description", json={"description": _MANUAL_TEXT}, headers=_auth(api_key)
        ).status_code == 200
        chats_before = len(server.requests_to("/api/chat"))

        response = _describe(client, api_key)
        jobs = client.get("/jobs", params={"kind": "description"}, headers=_auth(api_key)).json()

        assert (response.status_code, response.json()) == (409, {"detail": DESCRIBE_MANUAL_DETAIL})
        assert "clear" in DESCRIBE_MANUAL_DETAIL
        assert jobs["items"] == []
        assert len(server.requests_to("/api/chat")) == chats_before
        assert _meta(client).description == _MANUAL_TEXT


def test_describe_provider_down_settles_done_with_outcome_failed(booted) -> None:
    """S32, S58: a configured but unreachable provider is not a 409: 202, the job is ``DONE`` with outcome ``failed`` and
    the collection's stored status ``failed``."""
    with unreachable_port() as port:
        client, api_key = booted(_toml(f"http://127.0.0.1:{port}"))

        response = _describe(client, api_key)

        assert response.status_code == 202
        job = _settled_job(client, api_key, response.json()["job_id"])

    assert (job["status"], job["error"]) == ("DONE", None)
    assert job["result"] == {"outcome": "failed", "description_status": "failed"}


def test_collection_delete_not_blocked_by_running_describe_job(booted) -> None:
    """S87: DELETE /collections/{name} is not refused for a RUNNING describe job; the generation then finds the collection
    gone, writes nothing and recreates nothing, and the job settles ``DONE`` ``superseded``."""
    with FakeModelServer(reply_text=_FIRST_REPLY) as server:
        client, api_key = booted(_toml(server.base_url))
        chats_before = len(server.requests_to("/api/chat"))
        server.hold()
        accepted = _describe(client, api_key)
        assert accepted.status_code == 202
        _wait_for(lambda: len(server.requests_to("/api/chat")) > chats_before, "the model call of the describe job")
        assert client.get(f"/jobs/{accepted.json()['job_id']}", headers=_auth(api_key)).json()["status"] == "RUNNING"

        deleted = client.delete(f"/collections/{_COLLECTION}", headers=_auth(api_key))
        server.release()
        job = _settled_job(client, api_key, accepted.json()["job_id"])

        assert deleted.status_code == 200
        assert _meta(client) is None
        assert (job["status"], job["result"]) == ("DONE", {"outcome": "superseded", "description_status": None})
        assert client.get(f"/collections/{_COLLECTION}", headers=_auth(api_key)).status_code == 404


def test_foreign_namespace_describe_is_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S27: a key of another namespace gets the same 404 for a collection it cannot see as for one that does not exist,
    and no job is created."""
    foreign_key = secrets.token_hex(32)
    with FakeModelServer(reply_text=_FIRST_REPLY) as server, make_real_app(
        tmp_path, monkeypatch, toml_content=_toml(server.base_url), namespaces={foreign_key: _FOREIGN_NAMESPACE}
    ) as (client, _config, owner_key):
        _await_window_resolution(client)
        _ingest_corpus(client, tmp_path, owner_key)
        _wait_for(lambda: _idle(client), "the ingest's generation to settle")

        foreign = _describe(client, foreign_key)
        unknown = _describe(client, foreign_key, "no-such-collection")
        owner_jobs = client.get("/jobs", params={"kind": "description"}, headers=_auth(owner_key)).json()
        foreign_jobs = client.get("/jobs", params={"kind": "description"}, headers=_auth(foreign_key)).json()

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == {"detail": f"Collection {_COLLECTION!r} not found"}
    assert unknown.json() == {"detail": "Collection 'no-such-collection' not found"}
    assert (owner_jobs["items"], foreign_jobs["items"]) == ([], [])


def test_describe_unknown_collection_is_not_found(booted) -> None:
    with FakeModelServer() as server:
        client, api_key = booted(_toml(server.base_url), ingest=False)

        response = _describe(client, api_key, "no-such-collection")

    assert response.status_code == 404


def test_shutdown_during_a_running_describe_ends_the_job_failed_with_the_shutting_down_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S85: the server shutting down while a describe job runs ends the job ``FAILED`` with the shutting-down text (not
    ``DONE``, not left RUNNING); the model is called once for the job and never again."""
    with FakeModelServer(reply_text=_FIRST_REPLY) as server:
        with make_real_app(tmp_path, monkeypatch, toml_content=_toml(server.base_url)) as (client, _config, api_key):
            _await_window_resolution(client)
            _ingest_corpus(client, tmp_path, api_key)
            _wait_for(lambda: _idle(client), "the ingest's generation to settle")
            chats_before = len(server.requests_to("/api/chat"))
            server.hold()
            accepted = _describe(client, api_key)
            job_id = accepted.json()["job_id"]
            job_store = client.app.state.job_store
            _wait_for(lambda: len(server.requests_to("/api/chat")) > chats_before, "the model call of the describe job")
        server.release()

    job = job_store.get(job_id)
    assert (job.status.value, job.error) == ("FAILED", DESCRIBE_SHUTTING_DOWN_DETAIL)
    assert DESCRIBE_SHUTTING_DOWN_DETAIL == "the server is shutting down"
    assert len(server.requests_to("/api/chat")) == chats_before + 1


def test_second_describe_for_a_running_generation_settles_already_running(booted) -> None:
    """S80: while one generation of the collection runs, a second describe is accepted and settles ``DONE``
    ``alreadyRunning`` with the status read as ``pending``; only one model call is made."""
    with FakeModelServer(reply_text=_FIRST_REPLY) as server:
        client, api_key = booted(_toml(server.base_url))
        chats_before = len(server.requests_to("/api/chat"))
        server.hold()
        first = _describe(client, api_key)
        _wait_for(lambda: len(server.requests_to("/api/chat")) > chats_before, "the model call of the first job")

        second = _describe(client, api_key)
        second_job = _settled_job(client, api_key, second.json()["job_id"])
        server.release()
        first_job = _settled_job(client, api_key, first.json()["job_id"])

    assert (first.status_code, second.status_code) == (202, 202)
    assert second_job["result"] == {"outcome": "alreadyRunning", "description_status": "pending"}
    assert (first_job["status"], first_job["result"]["outcome"]) == ("DONE", "generated")
    assert len(server.requests_to("/api/chat")) == chats_before + 1


def test_describe_after_the_lifecycle_closed_ends_the_job_failed_without_a_model_call(booted) -> None:
    """S85: a describe that asks once shutdown began is accepted, then the job ends ``FAILED`` (shutting down) and no
    model call is made."""
    with FakeModelServer(reply_text=_FIRST_REPLY) as server:
        client, api_key = booted(_toml(server.base_url))
        chats_before = len(server.requests_to("/api/chat"))
        client.app.state.description_lifecycle.close()

        response = _describe(client, api_key)
        job = _settled_job(client, api_key, response.json()["job_id"])

    assert response.status_code == 202
    assert (job["status"], job["error"], job["result"]) == ("FAILED", DESCRIBE_SHUTTING_DOWN_DETAIL, None)
    assert len(server.requests_to("/api/chat")) == chats_before


def test_describe_job_that_breaks_unexpectedly_ends_failed_with_the_sanitized_error(
    booted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C10: ``FAILED`` is for a job that itself broke: the error is the sanitized ``internal error``, not the exception."""
    with FakeModelServer() as server:
        client, api_key = booted(_toml(server.base_url))

        async def broken(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("secret detail")

        monkeypatch.setattr(client.app.state.description_lifecycle, "generate_now", broken)

        response = _describe(client, api_key)
        job = _settled_job(client, api_key, response.json()["job_id"])

    assert (job["status"], job["error"]) == ("FAILED", INTERNAL_ERROR_DETAIL)


def test_describe_job_that_cannot_be_saved_answers_500_and_starts_nothing(booted, monkeypatch: pytest.MonkeyPatch) -> None:
    with FakeModelServer() as server:
        client, api_key = booted(_toml(server.base_url))
        chats_before = len(server.requests_to("/api/chat"))

        def refusing(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(client.app.state.job_store, "create_description", refusing)

        response = _describe(client, api_key)

    assert (response.status_code, response.json()) == (500, {"detail": INTERNAL_ERROR_DETAIL})
    assert len(server.requests_to("/api/chat")) == chats_before


def test_non_admin_owner_key_can_describe_its_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The key whose namespace owns the collection needs no admin right to describe it."""
    owner_key = secrets.token_hex(32)
    with FakeModelServer(reply_text=_FIRST_REPLY) as server, make_real_app(
        tmp_path, monkeypatch, toml_content=_toml(server.base_url), namespaces={owner_key: _FOREIGN_NAMESPACE}
    ) as (client, _config, _admin_key):
        _await_window_resolution(client)
        _ingest_corpus(client, tmp_path, owner_key)
        _wait_for(lambda: _idle(client), "the ingest's generation to settle")

        response = _describe(client, owner_key)
        job = _settled_job(client, owner_key, response.json()["job_id"])

    assert response.status_code == 202
    assert (job["status"], job["result"]) == ("DONE", {"outcome": "generated", "description_status": "ok"})
