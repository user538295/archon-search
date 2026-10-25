"""``SearchStore`` refuses to write, drop or count a collection another namespace owns (SecReview #2).

The LanceDB table is keyed by the collection name alone; the metadata row (name + namespace) is the only record of
who owns it. Every shared write path (ingest, delete, drop, import) goes through ``ensure_collection``,
``ingest_chunks``, ``delete_document``, ``drop_collection`` and ``count_chunks``, so that is where ownership is
enforced, whatever route or MCP tool reached them.

Ownership rule under test:

* a collection with a meta row belongs to that row's namespace, and only that namespace may touch it;
* a collection with NO meta row but a non-empty table (an orphan) is treated as ``default``-namespace data, the
  namespace ``list_collections`` already reports it under; any other namespace is refused;
* an empty or absent table is claimable by anyone: creating a collection is how ownership starts.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon_search._types import ChunkRecord
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.store import CollectionOwnershipError, SearchStore

_DIM = 4
_MODEL = "test-model"
_OWNER = "team-a"
_INTRUDER = "team-b"
_COLLECTION = "shared-name"


def _doc_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _chunk(doc_id: str, text: str, idx: int = 0) -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[1.0, 0.0, 0.0, 0.0],
        source_path=f"/data/{doc_id[:8]}.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )


@pytest.fixture
async def store(tmp_path: Path):
    search_store = SearchStore(tmp_path / "db")
    await search_store.connect()
    yield search_store
    await search_store.disconnect()


async def _owned_by(store: SearchStore, namespace: str, *, doc: str = "owner-doc") -> str:
    """Create ``_COLLECTION`` owned by *namespace* holding one document; return the doc id."""
    doc_id = _doc_id(doc)
    await store.ensure_collection(_COLLECTION, _DIM, namespace=namespace)
    await store.ingest_chunks(
        _COLLECTION, [_chunk(doc_id, "owner text")], embedding_model=_MODEL, namespace=namespace
    )
    return doc_id


async def _orphan(store: SearchStore) -> str:
    """Create ``_COLLECTION`` as an orphan: rows in the table and no meta row.

    Every write records its claim first, so the orphan is made the way one arises in the field: the meta row is lost
    (a crash between rows and meta in an older build, a manual repair) after the rows landed.
    """
    doc_id = _doc_id("orphan-doc")
    await store.ensure_collection(_COLLECTION, _DIM)
    await store.ingest_chunks(_COLLECTION, [_chunk(doc_id, "orphan text")], namespace=DEFAULT_NAMESPACE)
    await store.delete_collection_meta(_COLLECTION, DEFAULT_NAMESPACE)
    assert await store.get_collection_meta(_COLLECTION, DEFAULT_NAMESPACE) is None
    return doc_id


# --- a foreign namespace is refused at every shared layer ------------------------------------------------------


async def test_ingest_chunks_into_a_collection_another_namespace_owns_is_refused(store: SearchStore) -> None:
    await _owned_by(store, _OWNER)

    with pytest.raises(CollectionOwnershipError):
        await store.ingest_chunks(
            _COLLECTION, [_chunk(_doc_id("evil"), "poison")], embedding_model=_MODEL, namespace=_INTRUDER
        )

    assert await store.count_chunks(_COLLECTION, _OWNER) == 1, "the owner's table must be untouched"
    meta = await store.get_collection_meta(_COLLECTION, _OWNER)
    assert meta is not None and meta.chunk_count == 1, "the owner's centroid bookkeeping must be untouched"


async def test_ensure_collection_for_a_collection_another_namespace_owns_is_refused(store: SearchStore) -> None:
    await _owned_by(store, _OWNER)

    with pytest.raises(CollectionOwnershipError):
        await store.ensure_collection(_COLLECTION, _DIM, namespace=_INTRUDER)


async def test_delete_document_in_a_collection_another_namespace_owns_is_refused(store: SearchStore) -> None:
    doc_id = await _owned_by(store, _OWNER)

    with pytest.raises(CollectionOwnershipError):
        await store.delete_document(_COLLECTION, doc_id, namespace=_INTRUDER)

    assert await store.count_chunks(_COLLECTION, _OWNER) == 1


async def test_delete_by_source_path_in_a_collection_another_namespace_owns_is_refused(store: SearchStore) -> None:
    await _owned_by(store, _OWNER)

    with pytest.raises(CollectionOwnershipError):
        await store.delete_by_source_path(_COLLECTION, "/data/anything.md", namespace=_INTRUDER)


async def test_drop_collection_of_a_collection_another_namespace_owns_is_refused(store: SearchStore) -> None:
    await _owned_by(store, _OWNER)

    with pytest.raises(CollectionOwnershipError):
        await store.drop_collection(_COLLECTION, namespace=_INTRUDER)

    assert await store.count_chunks(_COLLECTION, _OWNER) == 1, "the table must survive"


async def test_count_chunks_of_a_collection_another_namespace_owns_is_refused(store: SearchStore) -> None:
    await _owned_by(store, _OWNER)

    with pytest.raises(CollectionOwnershipError):
        await store.count_chunks(_COLLECTION, _INTRUDER)


async def test_the_default_namespace_gets_no_access_to_another_namespaces_collection(store: SearchStore) -> None:
    """Namespace ``default`` (where the admin key lives) is a tenant like any other for data-plane writes."""
    doc_id = await _owned_by(store, _OWNER)

    with pytest.raises(CollectionOwnershipError):
        await store.delete_document(_COLLECTION, doc_id, namespace=DEFAULT_NAMESPACE)
    with pytest.raises(CollectionOwnershipError):
        await store.drop_collection(_COLLECTION, namespace=DEFAULT_NAMESPACE)


async def test_a_refused_drop_holds_no_lock_afterwards(store: SearchStore) -> None:
    await _owned_by(store, _OWNER)

    with pytest.raises(CollectionOwnershipError):
        await store.drop_collection(_COLLECTION, namespace=_INTRUDER)

    assert not store.lock_for(_COLLECTION).locked()


# --- the owner is unaffected ----------------------------------------------------------------------------------


async def test_the_owner_can_ingest_delete_count_and_drop(store: SearchStore) -> None:
    doc_id = await _owned_by(store, _OWNER)

    await store.ingest_chunks(
        _COLLECTION, [_chunk(_doc_id("second"), "more")], embedding_model=_MODEL, namespace=_OWNER
    )
    assert await store.count_chunks(_COLLECTION, _OWNER) == 2
    assert await store.delete_document(_COLLECTION, doc_id, namespace=_OWNER) == 1
    await store.drop_collection(_COLLECTION, namespace=_OWNER)

    assert await store.count_chunks(_COLLECTION, _OWNER) == 0


async def test_a_single_namespace_deployment_keeps_working_with_the_default_arguments(store: SearchStore) -> None:
    """Everything in ``default``: the calls made without a namespace argument behave as before."""
    doc_id = _doc_id("plain")
    await store.ensure_collection(_COLLECTION, _DIM)
    await store.ingest_chunks(_COLLECTION, [_chunk(doc_id, "plain")], embedding_model=_MODEL)

    assert await store.count_chunks(_COLLECTION, DEFAULT_NAMESPACE) == 1
    assert await store.delete_document(_COLLECTION, doc_id) == 1
    await store.drop_collection(_COLLECTION, namespace=DEFAULT_NAMESPACE)


async def test_a_brand_new_collection_is_claimed_by_the_first_namespace_to_ingest_into_it(store: SearchStore) -> None:
    await store.ensure_collection(_COLLECTION, _DIM, namespace=_OWNER)
    await store.ingest_chunks(
        _COLLECTION, [_chunk(_doc_id("first"), "first")], embedding_model=_MODEL, namespace=_OWNER
    )

    meta = await store.get_collection_meta(_COLLECTION, _OWNER)
    assert meta is not None and meta.namespace == _OWNER
    with pytest.raises(CollectionOwnershipError):
        await store.ingest_chunks(
            _COLLECTION, [_chunk(_doc_id("late"), "late")], embedding_model=_MODEL, namespace=_INTRUDER
        )


async def test_an_absent_collection_can_be_created_by_any_namespace(store: SearchStore) -> None:
    await store.ensure_collection("fresh-one", _DIM, namespace=_INTRUDER)

    assert await store.count_chunks("fresh-one", _INTRUDER) == 0


# --- orphans: a table with rows and no meta row ---------------------------------------------------------------


async def test_an_orphan_collection_is_refused_to_a_non_default_namespace(store: SearchStore) -> None:
    doc_id = await _orphan(store)

    with pytest.raises(CollectionOwnershipError):
        await store.ingest_chunks(
            _COLLECTION, [_chunk(_doc_id("evil"), "poison")], embedding_model=_MODEL, namespace=_INTRUDER
        )
    with pytest.raises(CollectionOwnershipError):
        await store.delete_document(_COLLECTION, doc_id, namespace=_INTRUDER)
    with pytest.raises(CollectionOwnershipError):
        await store.drop_collection(_COLLECTION, namespace=_INTRUDER)
    with pytest.raises(CollectionOwnershipError):
        await store.count_chunks(_COLLECTION, _INTRUDER)

    assert await store.count_chunks(_COLLECTION, DEFAULT_NAMESPACE) == 1


async def test_an_orphan_collection_stays_manageable_by_the_default_namespace(store: SearchStore) -> None:
    """``list_collections`` reports an orphan under ``default``, so ``default`` may repair or drop it."""
    doc_id = await _orphan(store)

    assert await store.delete_document(_COLLECTION, doc_id, namespace=DEFAULT_NAMESPACE) == 1
    await store.drop_collection(_COLLECTION, namespace=DEFAULT_NAMESPACE)


async def test_an_empty_orphan_table_is_claimable_by_any_namespace(store: SearchStore) -> None:
    """A table ``ensure_collection`` created and nothing was written to yet has no data to protect."""
    await store.ensure_collection(_COLLECTION, _DIM)

    await store.ingest_chunks(
        _COLLECTION, [_chunk(_doc_id("claim"), "claim")], embedding_model=_MODEL, namespace=_INTRUDER
    )

    meta = await store.get_collection_meta(_COLLECTION, _INTRUDER)
    assert meta is not None and meta.namespace == _INTRUDER


# --- the error reveals nothing about the owner ------------------------------------------------------------------


def test_the_error_message_is_fixed_so_it_cannot_reveal_an_owner() -> None:
    assert str(CollectionOwnershipError(_COLLECTION, _INTRUDER)) == str(CollectionOwnershipError("other", "x"))
