"""BE-31 (collection-descriptions plan, C6, S26, S27, S28, S61, S74): ``PUT`` and ``DELETE /collections/{name}/description``.

The routes run over a mock store holding the caller's row. A recording writer (or the real ``DescriptionWriter`` over the
mock store) stands in for the guarded write, so what is asserted is what the route asks for: the text it passes on, that
it holds no collection lock while it asks, the status it answers, and that nothing is requested from the lifecycle.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from archon_search.collection_meta import (
    CollectionMeta,
    DescriptionStatus,
    visible_description_status,
)
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE, MAX_DESCRIPTION_CHARS
from archon_search.description_write import (
    UNCONDITIONAL,
    DescriptionWriteOutcome,
    FieldOp,
)
from archon_search.description_writer import (
    ClearChange,
    DescriptionChange,
    DescriptionWriter,
    ManualSetChange,
    VectorOutcome,
    WriteResult,
)
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from tests._collection_lock_stub import with_locked_collection_meta
from tests._description_text_cases import REJECTED_TEXTS

_MANUAL_TEXT = "Hand written note about the planning documents."
_OLD_TEXT = "A description the model wrote earlier."
_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
_COLLECTION = "planning"
_FOREIGN_NAMESPACE = "someone-else"
_VECTOR = [0.25, 0.5, 0.75]
_RETRY_AFTER_HEADER = "retry-after"
_VERBS = ["put", "delete"]


def _meta(
    *, description: str = _OLD_TEXT, source: str | None = "generated", status: str | None = "ok"
) -> CollectionMeta:
    return CollectionMeta(
        name=_COLLECTION,
        namespace=DEFAULT_NAMESPACE,
        active_embedding_model=_EMBEDDING_MODEL,
        description=description,
        description_source=source,  # type: ignore[arg-type]
        description_status=status,  # type: ignore[arg-type]
    )


class _RecordingLifecycle:
    """The lifecycle as the routes may see it: the status overlay for a read (``pending`` for the collections in
    *in_flight*), and any generation requested is recorded."""

    def __init__(self) -> None:
        self.in_flight: set[str] = set()
        self.requests: list[tuple[Any, ...]] = []

    def overlay_status(self, meta: CollectionMeta) -> DescriptionStatus | None:
        return "pending" if meta.name in self.in_flight else meta.description_status

    async def request_generation(self, *args: Any, **kwargs: Any) -> None:
        self.requests.append((args, kwargs))


class _RecordingWriter:
    """Stands in for ``DescriptionWriter``: records each call and whether the collection lock was held during it."""

    def __init__(self, store: MagicMock, outcome: DescriptionWriteOutcome = DescriptionWriteOutcome.WRITTEN) -> None:
        self._store = store
        self._outcome = outcome
        self.calls: list[tuple[str, str, DescriptionChange]] = []
        self.lock_held_during_call: list[bool] = []

    async def apply(self, collection: str, namespace: str, change: DescriptionChange) -> WriteResult:
        self.calls.append((collection, namespace, change))
        self.lock_held_during_call.append(self._store.lock_for(collection).locked())
        return WriteResult(self._outcome, VectorOutcome.REFRESHED)


class _FixedEmbedders:
    """The embedder cache's stand-in: every model embeds to ``_VECTOR``."""

    async def get_or_load(self, model_name: str) -> _FixedEmbedders:
        return self

    async def embed_one(self, text: str) -> list[float]:
        return list(_VECTOR)


def _app(tmp_path: Path, rows: dict[str, CollectionMeta | None]) -> tuple[TestClient, MagicMock, _RecordingLifecycle]:
    """An app over a mock store; *rows* maps a namespace to the row it sees (a namespace not listed sees none)."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))

    async def get_collection_meta(name: str, namespace: str = DEFAULT_NAMESPACE) -> CollectionMeta | None:
        return rows.get(namespace) if name == _COLLECTION else None

    store = MagicMock()
    store.get_all_collections_meta = AsyncMock(return_value=[])
    store.get_collection_meta = AsyncMock(side_effect=get_collection_meta)
    store.count_chunks = AsyncMock(return_value=3)
    store.count_documents = AsyncMock(return_value=1)
    store.get_acl_stats = AsyncMock(return_value=(0, 0))
    store.update_collection_meta = AsyncMock()
    store.write_description = AsyncMock(return_value=DescriptionWriteOutcome.WRITTEN)
    with_locked_collection_meta(store)
    store.connect = AsyncMock()
    store.disconnect = AsyncMock()
    app.state.search_store = store
    lifecycle = _RecordingLifecycle()
    app.state.description_lifecycle = lifecycle
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"}), store, lifecycle


def _reads(store: MagicMock, *rows: CollectionMeta | None) -> None:
    """Make successive ``get_collection_meta`` reads answer *rows* in order, the last one for every read after."""
    remaining = list(rows)
    store.get_collection_meta = AsyncMock(
        side_effect=lambda *_args, **_kwargs: remaining.pop(0) if len(remaining) > 1 else remaining[0]
    )


def _put(client: TestClient, text: Any) -> Response:
    return client.put(f"/collections/{_COLLECTION}/description", json={"description": text})


def _delete(client: TestClient) -> Response:
    return client.delete(f"/collections/{_COLLECTION}/description")


def _change(client: TestClient, verb: str) -> Response:
    return _put(client, _MANUAL_TEXT) if verb == "put" else _delete(client)


# ---------------------------------------------------------------------------
# S61: the text rule, one place, refused and never trimmed
# ---------------------------------------------------------------------------

_REJECTED_PARAMS = [pytest.param(text, id=case) for case, text in REJECTED_TEXTS.items()]


@pytest.mark.parametrize("text", _REJECTED_PARAMS)
def test_set_rejects_empty_multiline_control_and_overlong_text(tmp_path: Path, text: str) -> None:
    """422, nothing written, and the text is never trimmed into something acceptable."""
    client, store, lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    writer = _RecordingWriter(store)
    client.app.state.description_writer = writer

    response = _put(client, text)

    assert response.status_code == 422, response.text
    assert writer.calls == [], "invalid text reaches no write"
    assert lifecycle.requests == []


@pytest.mark.parametrize("text", [pytest.param(None, id="null"), pytest.param(7, id="number")])
def test_set_rejects_a_body_without_text(tmp_path: Path, text: Any) -> None:
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    writer = _RecordingWriter(store)
    client.app.state.description_writer = writer

    assert _put(client, text).status_code == 422
    assert client.put(f"/collections/{_COLLECTION}/description", json={}).status_code == 422
    assert writer.calls == []


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("x" * MAX_DESCRIPTION_CHARS, id="exactly-the-limit"),
        pytest.param("  padded on both sides  ", id="surrounding-spaces-kept"),
        pytest.param("Zażółć gęślą jaźń: 日本語のメモ — notes", id="non-ascii-plain-text"),
    ],
)
def test_set_accepts_one_line_of_plain_text_and_passes_it_on_untrimmed(tmp_path: Path, text: str) -> None:
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    writer = _RecordingWriter(store)
    client.app.state.description_writer = writer

    response = _put(client, text)

    assert response.status_code == 200, response.text
    [(_collection, _namespace, change)] = writer.calls
    assert change == ManualSetChange(text=text)


# ---------------------------------------------------------------------------
# S26: set, as the namespace owner, without the collection lock
# ---------------------------------------------------------------------------


def test_set_answers_the_updated_collection_detail_and_holds_no_collection_lock(tmp_path: Path) -> None:
    before = _meta()
    after = _meta(description=_MANUAL_TEXT, source="manual", status="ok")
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: before})
    _reads(store, before, after)
    writer = _RecordingWriter(store)
    client.app.state.description_writer = writer

    response = _put(client, _MANUAL_TEXT)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == _COLLECTION
    assert body["description"] == _MANUAL_TEXT
    assert body["description_source"] == "manual"
    assert body["description_status"] == "ok"
    assert body["active_embedding_model"] == _EMBEDDING_MODEL
    assert writer.calls == [(_COLLECTION, DEFAULT_NAMESPACE, ManualSetChange(text=_MANUAL_TEXT))]
    assert writer.lock_held_during_call == [False], "the writer takes the lock itself; the route must not hold it"


def test_set_through_the_real_writer_stores_manual_ok_text_with_a_fresh_vector_unconditionally(tmp_path: Path) -> None:
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    client.app.state.description_writer = DescriptionWriter(
        store, _FixedEmbedders(), default_embedding_model=_EMBEDDING_MODEL
    )

    response = _put(client, _MANUAL_TEXT)

    assert response.status_code == 200, response.text
    collection, namespace, fields, guard = store.write_description.await_args.args
    assert (collection, namespace) == (_COLLECTION, DEFAULT_NAMESPACE)
    assert guard == UNCONDITIONAL
    assert fields.description.value == _MANUAL_TEXT
    assert fields.source.value == "manual"
    assert fields.status.value == "ok"
    assert fields.last_described.action is FieldOp.SET
    assert fields.described_at_doc_count.action is FieldOp.KEEP, "a set leaves the baseline alone"
    assert fields.description_embedding.value == _VECTOR


def test_set_never_requests_a_generation(tmp_path: Path) -> None:
    client, store, lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    client.app.state.description_writer = _RecordingWriter(store)

    assert _put(client, _MANUAL_TEXT).status_code == 200

    assert lifecycle.requests == []


def test_set_while_a_generation_is_in_flight_answers_ok_not_pending(tmp_path: Path) -> None:
    """S26/S74: the in-flight generation is refused at its own write, so a manual description never reads ``pending``."""
    before = _meta()
    after = _meta(description=_MANUAL_TEXT, source="manual", status="ok")
    client, store, lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: before})
    _reads(store, before, after)
    client.app.state.description_writer = _RecordingWriter(store)
    lifecycle.in_flight.add(_COLLECTION)

    body = _put(client, _MANUAL_TEXT).json()

    assert (body["description_source"], body["description_status"]) == ("manual", "ok")


def test_clear_while_a_generation_is_in_flight_answers_the_stored_empty_state_not_pending(tmp_path: Path) -> None:
    """S28: the response says what the clear stored (no description, source or status); a generation that began earlier
    is refused at its own write, so it must not make the cleared collection read ``pending``."""
    before = _meta()
    after = _meta(description="", source=None, status=None)
    client, store, lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: before})
    _reads(store, before, after)
    client.app.state.description_writer = _RecordingWriter(store)
    lifecycle.in_flight.add(_COLLECTION)

    body = _delete(client).json()

    assert (body["description"], body["description_source"], body["description_status"]) == ("", None, None)


def test_a_manual_description_is_never_pending_and_a_generated_one_is() -> None:
    lifecycle = _RecordingLifecycle()
    lifecycle.in_flight.add(_COLLECTION)

    assert visible_description_status(_meta(source="manual"), lifecycle) == "ok"
    assert visible_description_status(_meta(source="generated"), lifecycle) == "pending"
    assert visible_description_status(_meta(source=None, status=None), lifecycle) == "pending"
    assert visible_description_status(_meta(source="manual", status="ok"), None) == "ok"


# ---------------------------------------------------------------------------
# S28: clear resets every field and starts nothing
# ---------------------------------------------------------------------------


def test_clear_resets_baseline_and_starts_no_generation(tmp_path: Path) -> None:
    """Every description field is cleared in one unconditional write, and nothing is requested from the lifecycle."""
    client, store, lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta(description=_MANUAL_TEXT, source="manual")})
    client.app.state.description_writer = DescriptionWriter(
        store, _FixedEmbedders(), default_embedding_model=_EMBEDDING_MODEL
    )

    response = _delete(client)

    assert response.status_code == 200, response.text
    collection, namespace, fields, guard = store.write_description.await_args.args
    assert (collection, namespace) == (_COLLECTION, DEFAULT_NAMESPACE)
    assert guard == UNCONDITIONAL
    for field in (
        fields.description,
        fields.source,
        fields.status,
        fields.last_described,
        fields.described_at_doc_count,
        fields.description_embedding,
    ):
        assert field.action is FieldOp.CLEAR
    assert store.write_description.await_count == 1
    assert lifecycle.requests == [], "clear starts nothing: the next qualifying trigger regenerates"


def test_clear_answers_the_collection_detail_with_the_description_reset(tmp_path: Path) -> None:
    before = _meta(description=_MANUAL_TEXT, source="manual")
    after = _meta(description="", source=None, status=None)
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: before})
    _reads(store, before, after)
    writer = _RecordingWriter(store)
    client.app.state.description_writer = writer

    response = _delete(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["description"], body["description_source"], body["description_status"]) == ("", None, None)
    assert writer.calls == [(_COLLECTION, DEFAULT_NAMESPACE, ClearChange())]
    assert writer.lock_held_during_call == [False]


# ---------------------------------------------------------------------------
# S27: the caller must see the collection; 404 otherwise, never 403
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", _VERBS)
def test_a_collection_the_caller_cannot_see_is_not_found_and_nothing_is_written(tmp_path: Path, verb: str) -> None:
    """The row belongs to another namespace, so the caller's namespace sees none."""
    client, store, lifecycle = _app(tmp_path, {_FOREIGN_NAMESPACE: _meta()})
    writer = _RecordingWriter(store)
    client.app.state.description_writer = writer

    response = _change(client, verb)

    assert response.status_code == 404, response.text
    assert _COLLECTION in response.json()["detail"]
    assert writer.calls == []
    assert lifecycle.requests == []


@pytest.mark.parametrize("verb", _VERBS)
@pytest.mark.parametrize("name", ["has.dot", "x" * 65, "-leading-dash"])
def test_a_name_the_store_refuses_is_not_found_before_the_store_is_asked(tmp_path: Path, verb: str, name: str) -> None:
    """No collection can have a name the store refuses, so the caller cannot see one: 404, like any unknown name."""
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    writer = _RecordingWriter(store)
    client.app.state.description_writer = writer

    if verb == "put":
        response = client.put(f"/collections/{name}/description", json={"description": _MANUAL_TEXT})
    else:
        response = client.delete(f"/collections/{name}/description")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": f"Collection {name!r} not found"}
    store.get_collection_meta.assert_not_awaited()
    assert writer.calls == []


@pytest.mark.parametrize("verb", _VERBS)
def test_a_row_the_store_cannot_read_is_a_server_fault_not_a_false_not_found(tmp_path: Path, verb: str) -> None:
    """A ValueError from reading a valid name's row is a corrupt row, not an absent collection: the owner must not be
    told 404."""
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    client.app.state.description_writer = _RecordingWriter(store)
    store.get_collection_meta = AsyncMock(side_effect=ValueError("corrupt last_indexed"))
    client = TestClient(client.app, raise_server_exceptions=False, headers=dict(client.headers))

    assert _change(client, verb).status_code == 500


@pytest.mark.parametrize("verb", _VERBS)
def test_a_collection_dropped_while_the_write_waited_is_not_found(tmp_path: Path, verb: str) -> None:
    """The writer reports ``collectionMissing`` (a concurrent DELETE won): 404, no row is revived."""
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    client.app.state.description_writer = _RecordingWriter(store, DescriptionWriteOutcome.COLLECTION_MISSING)

    assert _change(client, verb).status_code == 404


@pytest.mark.parametrize("verb", _VERBS)
def test_a_collection_dropped_after_the_write_is_not_found(tmp_path: Path, verb: str) -> None:
    """The write landed, then the row was gone when the response was built."""
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    _reads(store, _meta(), None)
    client.app.state.description_writer = _RecordingWriter(store)

    assert _change(client, verb).status_code == 404


# ---------------------------------------------------------------------------
# 503 store_busy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", _VERBS)
def test_a_lock_timeout_is_503_store_busy_with_retry_after(tmp_path: Path, verb: str) -> None:
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    client.app.state.description_writer = _RecordingWriter(store, DescriptionWriteOutcome.LOCK_TIMEOUT)

    response = _change(client, verb)

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "store_busy"
    assert _RETRY_AFTER_HEADER in response.headers


@pytest.mark.parametrize("verb", _VERBS)
@pytest.mark.parametrize("refusal", [DescriptionWriteOutcome.REFUSED_MANUAL, DescriptionWriteOutcome.REFUSED_STALE])
def test_an_unconditional_write_that_is_refused_is_a_server_fault_not_a_success(
    tmp_path: Path, verb: str, refusal: DescriptionWriteOutcome
) -> None:
    """Set and clear carry no guard, so a refusal means the store broke its contract: never answer 200 for it."""
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    client.app.state.description_writer = _RecordingWriter(store, refusal)
    client = TestClient(client.app, raise_server_exceptions=False, headers=dict(client.headers))

    assert _change(client, verb).status_code == 500


@pytest.mark.parametrize("verb", _VERBS)
def test_a_server_without_a_search_store_is_503_service_not_ready(tmp_path: Path, verb: str) -> None:
    client, _store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})
    client.app.state.search_store = None

    response = _change(client, verb)

    assert response.status_code == 503
    assert response.json() == {"detail": "service not ready"}


# ---------------------------------------------------------------------------
# The writer exists whether or not [description] is enabled (manual wins, no provider needed)
# ---------------------------------------------------------------------------


def test_create_app_builds_a_description_writer_even_when_descriptions_are_disabled(tmp_path: Path) -> None:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    assert config.description.enabled is False

    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))

    assert isinstance(app.state.description_writer, DescriptionWriter)
    assert app.state.description_lifecycle is None
    assert app.state.description_client is None
