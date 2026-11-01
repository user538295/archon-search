"""BE-31 (collection-descriptions plan, C6, S26, S27, S28, S30, S61, S74) — set and clear a description by hand.

``make_real_app`` boots the real app over a real LanceDB store. The first tests run with ``[description]`` left at its
default (off): setting and clearing a description by hand needs no provider and no admin key, only a namespace that owns
the collection, and works on a collection that has no config-path entry. The rest turn generation on against a fake model
server: neither a later ingest nor a describe request overwrites the manual text, and a set or a clear that lands while a
generation is in its model call is not undone when that generation finishes (S74).

Run with:
    uv run pytest tests/integration/test_lcd_be31_manual_description.py --no-cov -q
"""

from __future__ import annotations

import contextlib
import secrets
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE, MAX_DESCRIPTION_CHARS
from archon_search.description_lifecycle import GenerationKind, RequestOutcome
from archon_search.description_write import DescriptionWriteOutcome
from archon_search.server.schemas import WarmupResult
from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_OWNER_NAMESPACE = "team-owner"
_OTHER_NAMESPACE = "team-other"
_COLLECTION = "manual-notes"
_MANUAL_TEXT = "Hand written note about the planning documents."
_SECOND_MANUAL_TEXT = "A different hand written note."
_GENERATED_TEXT = "Notes about the survey of the valley."
_MODEL = "description-model"
_CHAT_PATH = "/api/chat"
_CONFIGURED_WINDOW = 8192
_PROVIDER_TIMEOUT_SECONDS = 120
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_FIRST_DOCUMENTS = 3
_GROWN_DOCUMENTS = 12  # well past the 20% change that lets an ingest ask for a new description


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _document(index: int) -> str:
    return f"Field notes {index} about the survey of the valley and how it was carried out.\n" * 3


def _write_corpus(directory: Path, count: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"note-{index}.txt").write_text(_document(index), encoding="utf-8")
    return directory


def _row(client: TestClient, namespace: str, collection: str = _COLLECTION) -> CollectionMeta | None:
    store = client.app.state.search_store
    return client.portal.call(lambda: store.get_collection_meta(collection, namespace=namespace))  # type: ignore[union-attr]


def _put(client: TestClient, token: str, text: Any, collection: str = _COLLECTION) -> Any:
    return client.put(f"/collections/{collection}/description", json={"description": text}, headers=_headers(token))


def _delete(client: TestClient, token: str, collection: str = _COLLECTION) -> Any:
    return client.delete(f"/collections/{collection}/description", headers=_headers(token))


def _detail(client: TestClient, token: str) -> dict[str, Any]:
    response = client.get(f"/collections/{_COLLECTION}", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"{what} did not happen within {_WAIT_SECONDS}s"
        time.sleep(_POLL_SECONDS)


class _Keys(NamedTuple):
    owner: str
    admin: str
    other: str


@contextlib.contextmanager
def _owner_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any, _Keys]]:
    """An app with a namespace key that owns a meta-only collection (no config path), a second namespace key and the
    default (admin) key; yields the client, the config and the three keys."""
    corpus = _write_corpus(tmp_path / "corpus", _FIRST_DOCUMENTS)
    keys = {"owner": secrets.token_hex(32), "other": secrets.token_hex(32)}
    namespaces = {keys["owner"]: _OWNER_NAMESPACE, keys["other"]: _OTHER_NAMESPACE}
    with make_real_app(tmp_path, monkeypatch, namespaces=namespaces) as (client, config, admin_key):
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=keys["owner"])
        yield client, config, _Keys(owner=keys["owner"], admin=admin_key, other=keys["other"])


def test_namespace_owner_sets_manual_description_on_meta_only_collection_without_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A namespace key that is not admin sets the description of a collection that has a meta row and no config path,
    with ``[description]`` off: the check is that the collection exists in the caller's namespace."""
    with _owner_collection(tmp_path, monkeypatch) as (client, config, keys):
        owner_key = keys.owner
        assert config.description.enabled is False
        assert _COLLECTION not in [Path(path).name for path in config.collections], "no config-path entry"
        assert _detail(client, owner_key)["path"] == ""

        response = _put(client, owner_key, _MANUAL_TEXT)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == _COLLECTION
        assert body["namespace"] == _OWNER_NAMESPACE
        assert body["description"] == _MANUAL_TEXT
        assert body["description_source"] == "manual"
        assert body["description_status"] == "ok"
        assert body["doc_count"] == _FIRST_DOCUMENTS
        assert _detail(client, owner_key)["description"] == _MANUAL_TEXT
        listed = client.get("/collections/", headers=_headers(owner_key)).json()
        entry = next(item for item in listed if item["name"] == _COLLECTION)
        assert (entry["description"], entry["description_source"]) == (_MANUAL_TEXT, "manual")

        stored = _row(client, _OWNER_NAMESPACE)
        assert stored is not None
        assert stored.description == _MANUAL_TEXT
        assert stored.description_embedding, "the text's routing vector is refreshed with the text"
        assert stored.last_described is not None
        assert stored.described_at_doc_count is None, "a manual set leaves the baseline alone"


def test_a_second_set_replaces_the_manual_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _owner_collection(tmp_path, monkeypatch) as (client, _config, keys):
        owner_key = keys.owner
        assert _put(client, owner_key, _MANUAL_TEXT).status_code == 200

        replaced = _put(client, owner_key, _SECOND_MANUAL_TEXT)

        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["description"] == _SECOND_MANUAL_TEXT
        stored = _row(client, _OWNER_NAMESPACE)
        assert stored is not None
        assert (stored.description, stored.description_source) == (_SECOND_MANUAL_TEXT, "manual")


def test_namespace_owner_clears_the_description_and_every_field_that_goes_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _owner_collection(tmp_path, monkeypatch) as (client, _config, keys):
        owner_key = keys.owner
        assert _put(client, owner_key, _MANUAL_TEXT).status_code == 200

        cleared = _delete(client, owner_key)

        assert cleared.status_code == 200, cleared.text
        body = cleared.json()
        assert (body["description"], body["description_source"], body["description_status"]) == ("", None, None)
        after = _row(client, _OWNER_NAMESPACE)
        assert after is not None
        assert not after.description
        assert after.description_source is None
        assert after.description_status is None
        assert after.last_described is None
        assert after.described_at_doc_count is None
        assert not after.description_embedding
        assert after.doc_count == _FIRST_DOCUMENTS, "clear touches nothing but the description fields"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("two\nlines", id="multi-line"),
        pytest.param("tab\there", id="control-character"),
        pytest.param("x" * (MAX_DESCRIPTION_CHARS + 1), id="over-long"),
    ],
)
def test_invalid_text_is_422_and_leaves_the_stored_description_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    with _owner_collection(tmp_path, monkeypatch) as (client, _config, keys):
        owner_key = keys.owner
        assert _put(client, owner_key, _MANUAL_TEXT).status_code == 200

        response = _put(client, owner_key, text)

        assert response.status_code == 422, response.text
        stored = _row(client, _OWNER_NAMESPACE)
        assert stored is not None
        assert (stored.description, stored.description_source) == (_MANUAL_TEXT, "manual")


def test_foreign_namespace_set_and_clear_return_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller that cannot see the collection gets the answer for a collection that does not exist, never 403, and the
    owner's description is unchanged; the default (admin) namespace is no exception."""
    with _owner_collection(tmp_path, monkeypatch) as (client, _config, keys):
        owner_key = keys.owner
        assert _put(client, owner_key, _MANUAL_TEXT).status_code == 200

        for token in (keys.other, keys.admin):
            put = _put(client, token, "an intruder's text")
            deleted = _delete(client, token)
            assert (put.status_code, deleted.status_code) == (404, 404), (put.text, deleted.text)
            assert put.json() == {"detail": f"Collection {_COLLECTION!r} not found"}
            missing = _put(client, token, "an intruder's text", collection="never-existed")
            assert missing.status_code == 404
            assert missing.json() == {"detail": "Collection 'never-existed' not found"}

        stored = _row(client, _OWNER_NAMESPACE)
        assert stored is not None
        assert (stored.description, stored.description_source, stored.description_status) == (_MANUAL_TEXT, "manual", "ok")
        assert _row(client, _OTHER_NAMESPACE) is None
        assert _row(client, DEFAULT_NAMESPACE) is None


@pytest.mark.parametrize("name", ["has.dot", "x" * 65, "-leading-dash"])
def test_a_collection_name_the_store_refuses_is_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    with _owner_collection(tmp_path, monkeypatch) as (client, _config, keys):
        owner_key = keys.owner
        assert _put(client, owner_key, _MANUAL_TEXT, collection=name).status_code == 404
        assert _delete(client, owner_key, collection=name).status_code == 404


def _description_toml(base_url: str) -> str:
    return (
        "[description]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"ollama_context_tokens = {_CONFIGURED_WINDOW}\n"
        f"timeout_seconds = {_PROVIDER_TIMEOUT_SECONDS}\n"
    )


@contextlib.contextmanager
def _described_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, hold_chat: bool = False, chat_status: int | None = None
) -> Iterator[tuple[TestClient, str, FakeModelServer, Path]]:
    """A real app with ``[description]`` on against a fake model server (answering *chat_status* on the chat endpoint when
    given); yields the client, the admin key, the server and the corpus directory (written with the first documents)."""
    corpus = _write_corpus(tmp_path / "corpus", _FIRST_DOCUMENTS)
    status_overrides = {} if chat_status is None else {_CHAT_PATH: chat_status}
    with (
        FakeModelServer(reply_text=_GENERATED_TEXT, hold_chat=hold_chat, status_overrides=status_overrides) as server,
        make_real_app(tmp_path, monkeypatch, toml_content=_description_toml(server.base_url)) as (client, _config, key),
    ):
        _wait_for(
            lambda: getattr(client.app.state, "warmup_result", None) not in (None, WarmupResult.PENDING),
            "the lifespan warm-up",
        )
        _wait_for(client.app.state._llm_window_task.done, "the context-window resolution")
        yield client, key, server, corpus


def _record_write_outcomes(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> list[DescriptionWriteOutcome]:
    """Record what each guarded description write from now on answers, so a test can see a refusal happen."""
    store = client.app.state.search_store
    original = store.write_description
    outcomes: list[DescriptionWriteOutcome] = []

    async def recording(*args: Any, **kwargs: Any) -> DescriptionWriteOutcome:
        outcome = await original(*args, **kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(store, "write_description", recording)
    return outcomes


def _await_idle(client: TestClient) -> None:
    lifecycle = client.app.state.description_lifecycle
    _wait_for(lambda: not lifecycle.is_pending(_COLLECTION), "the generation to settle")


def _await_generated(client: TestClient, key: str) -> None:
    _wait_for(lambda: _detail(client, key)["description_status"] == "ok", "the first generated description")
    assert _detail(client, key)["description_source"] == "generated"


def test_ingest_and_describe_never_overwrite_manual_description(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a manual set, an ingest that grows the collection past the regeneration threshold and a describe request both
    leave the manual text and source alone, and the model is not asked again."""
    with _described_app(tmp_path, monkeypatch) as (client, key, server, corpus):
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=key)
        _await_generated(client, key)
        assert _put(client, key, _MANUAL_TEXT).status_code == 200
        asked_before = len(server.requests_to(_CHAT_PATH))

        lifecycle = client.app.state.description_lifecycle
        original = lifecycle.request_generation
        outcomes: list[RequestOutcome] = []

        async def recording(*args: Any, **kwargs: Any) -> Any:
            ticket = await original(*args, **kwargs)
            outcomes.append(ticket.outcome)
            return ticket

        monkeypatch.setattr(lifecycle, "request_generation", recording)
        _write_corpus(corpus, _GROWN_DOCUMENTS)
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=key)

        grown = _detail(client, key)
        assert grown["doc_count"] == _GROWN_DOCUMENTS, "the ingest really grew the collection"
        assert outcomes == [RequestOutcome.REFUSED_MANUAL], "the ingest asked, and the lifecycle refused a manual text"
        assert (grown["description"], grown["description_source"], grown["description_status"]) == (
            _MANUAL_TEXT,
            "manual",
            "ok",
        )

        ticket = client.portal.call(  # type: ignore[union-attr]
            lambda: original(_COLLECTION, DEFAULT_NAMESPACE, GenerationKind.DESCRIBE)
        )
        assert ticket.outcome is RequestOutcome.REFUSED_MANUAL

        final = _detail(client, key)
        assert (final["description"], final["description_source"], final["description_status"]) == (
            _MANUAL_TEXT,
            "manual",
            "ok",
        )
        assert len(server.requests_to(_CHAT_PATH)) == asked_before, "the model is not asked about a manual description"


def test_a_set_during_a_generation_is_not_overwritten_when_the_generation_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S74: the generation has read its row and is in its model call; the operator sets a manual description; the
    generated write is then refused inside the lock and its text discarded."""
    with _described_app(tmp_path, monkeypatch, hold_chat=True) as (client, key, server, corpus):
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=key)
        _wait_for(lambda: len(server.requests_to(_CHAT_PATH)) >= 1, "the model call to start")
        assert _detail(client, key)["description_status"] == "pending"
        outcomes = _record_write_outcomes(monkeypatch, client)

        response = _put(client, key, _MANUAL_TEXT)

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["description"], body["description_source"], body["description_status"]) == (
            _MANUAL_TEXT,
            "manual",
            "ok",
        ), "a manual description never reads pending, though the generation is still in its model call"
        assert _detail(client, key)["description_status"] == "ok"
        listed = next(item for item in client.get("/collections/", headers=_headers(key)).json() if item["name"] == _COLLECTION)
        assert (listed["description_source"], listed["description_status"]) == ("manual", "ok")

        server.release()
        _await_idle(client)

        assert outcomes == [DescriptionWriteOutcome.WRITTEN, DescriptionWriteOutcome.REFUSED_MANUAL], (
            "the set landed, then the generation reached its write and was refused as manual"
        )
        final = _detail(client, key)
        assert (final["description"], final["description_source"], final["description_status"]) == (
            _MANUAL_TEXT,
            "manual",
            "ok",
        )
        stored = _row(client, DEFAULT_NAMESPACE)
        assert stored is not None
        assert stored.described_at_doc_count is None, "the discarded generation saved no baseline"


def test_a_clear_during_a_generation_is_not_undone_when_the_generation_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S74: a description exists; a second generation is in its model call; the operator clears it; the stale generated
    write is refused and the collection stays without a description."""
    with _described_app(tmp_path, monkeypatch) as (client, key, server, corpus):
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=key)
        _await_generated(client, key)
        _await_idle(client)
        asked_before = len(server.requests_to(_CHAT_PATH))
        server.hold()
        _write_corpus(corpus, _GROWN_DOCUMENTS)
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=key)
        _wait_for(lambda: len(server.requests_to(_CHAT_PATH)) > asked_before, "the second model call to start")
        outcomes = _record_write_outcomes(monkeypatch, client)

        response = _delete(client, key)

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["description"], body["description_source"], body["description_status"]) == ("", None, None)

        server.release()
        _await_idle(client)

        assert outcomes == [DescriptionWriteOutcome.WRITTEN, DescriptionWriteOutcome.REFUSED_STALE], (
            "the clear landed, then the generation reached its write and was refused as stale"
        )
        final = _detail(client, key)
        assert (final["description"], final["description_source"], final["description_status"]) == ("", None, None)
        stored = _row(client, DEFAULT_NAMESPACE)
        assert stored is not None
        assert not stored.description
        assert stored.description_source is None
        assert stored.described_at_doc_count is None


def test_a_generation_that_fails_after_a_set_does_not_mark_the_manual_description_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S74: the provider answers an error once the operator has set a description; the failure is refused at its record
    step, so a manual ``ok`` never becomes ``failed``."""
    with _described_app(tmp_path, monkeypatch, hold_chat=True, chat_status=500) as (client, key, server, corpus):
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=key)
        _wait_for(lambda: len(server.requests_to(_CHAT_PATH)) >= 1, "the model call to start")
        outcomes = _record_write_outcomes(monkeypatch, client)
        assert _put(client, key, _MANUAL_TEXT).status_code == 200

        server.release()
        _await_idle(client)

        assert outcomes == [DescriptionWriteOutcome.WRITTEN, DescriptionWriteOutcome.REFUSED_MANUAL], (
            "the set landed, then the failed generation tried to record `failed` and was refused as manual"
        )
        stored = _row(client, DEFAULT_NAMESPACE)
        assert stored is not None
        assert (stored.description, stored.description_source, stored.description_status) == (_MANUAL_TEXT, "manual", "ok")


def test_clear_with_generation_on_starts_no_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S28: a clear only resets; the model is not asked until the next qualifying trigger."""
    with _described_app(tmp_path, monkeypatch) as (client, key, server, corpus):
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=key)
        _await_generated(client, key)
        assert _put(client, key, _MANUAL_TEXT).status_code == 200
        asked_before = len(server.requests_to(_CHAT_PATH))
        requested: list[tuple[Any, ...]] = []

        async def spy(*args: Any, **kwargs: Any) -> None:
            requested.append(args)

        monkeypatch.setattr(client.app.state.description_lifecycle, "request_generation", spy)

        response = _delete(client, key)

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["description"], body["description_source"], body["description_status"]) == ("", None, None)
        assert requested == [], "a clear requests nothing from the lifecycle"
        assert len(server.requests_to(_CHAT_PATH)) == asked_before
        stored = _row(client, DEFAULT_NAMESPACE)
        assert stored is not None
        assert not stored.description
        assert stored.described_at_doc_count is None
