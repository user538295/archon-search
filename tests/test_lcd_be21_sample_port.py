"""BE-21 (C2, C3) — the store port reads behind the description sample: ``list_chunk_ids`` and ``read_excerpts``.

Scenarios covered: S55 (only open chunks, ordered by ``chunk_id``), S82 (the order is the id order whatever the row
order; a store error raises instead of reading as no chunks).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from archon_search._types import ChunkRecord
from archon_search.store import CollectionOwnershipError, StoreReadError

_DIM = 4
_NAMESPACE_OWNER = "alice"
_NAMESPACE_OTHER = "bob"
_EMBEDDING_MODEL = "mock-embedder"


def _doc_id(letter: str) -> str:
    return letter * 64


def _chunk(letter: str, idx: int, *, acl: list[str] | None = None, text: str | None = None) -> ChunkRecord:
    doc_id = _doc_id(letter)
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text if text is not None else f"text {letter}{idx}",
        vector=[float(idx % 4)] * _DIM,
        source_path=f"/tmp/{letter}.md",
        indexed_at=datetime.now(UTC).isoformat(),
        acl=acl,
    )


async def _physical_chunk_ids(store, collection: str) -> list[str]:
    table = await store._require_connected().open_table(collection)
    return [row["chunk_id"] for row in await table.query().select(["chunk_id"]).to_list()]


@pytest.mark.asyncio
async def test_list_chunk_ids_excludes_acl_and_deny_all_chunks(connected_store, col_name: str) -> None:
    """S55 — a chunk with any ACL, deny-all ``[]`` included, is never listed unless restricted chunks are allowed."""
    await connected_store.ensure_collection(col_name, _DIM)
    open_chunk = _chunk("a", 0)
    acl_chunk = _chunk("a", 1, acl=["team-x"])
    deny_all_chunk = _chunk("a", 2, acl=[])
    await connected_store.ingest_chunks(col_name, [deny_all_chunk, acl_chunk, open_chunk])

    assert await connected_store.list_chunk_ids(col_name, "default") == [open_chunk.chunk_id]
    assert await connected_store.list_chunk_ids(col_name, "default", include_restricted=True) == [
        open_chunk.chunk_id,
        acl_chunk.chunk_id,
        deny_all_chunk.chunk_id,
    ]


@pytest.mark.asyncio
async def test_list_chunk_ids_lists_nothing_when_every_chunk_is_restricted(connected_store, col_name: str) -> None:
    """The read worked and found no open chunk: an empty list, not an error."""
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [_chunk("a", 0, acl=[]), _chunk("a", 1, acl=["team-x"])])

    assert await connected_store.list_chunk_ids(col_name, "default") == []


@pytest.mark.asyncio
async def test_port_reads_treat_a_table_without_acl_column_as_open(connected_store, col_name: str) -> None:
    """A table from before the ``acl`` column existed has only open rows, for the listing and for the read."""
    db = connected_store._require_connected()
    await db.create_table(col_name, data=[{"chunk_id": "b-1", "text": "second"}, {"chunk_id": "a-1", "text": "first"}])
    try:
        assert await connected_store.list_chunk_ids(col_name, "default") == ["a-1", "b-1"]
        assert await connected_store.read_excerpts(col_name, "default", ["b-1", "a-1"]) == ["second", "first"]
    finally:
        await db.drop_table(col_name)


@pytest.mark.asyncio
async def test_port_reads_of_an_existing_table_without_rows_are_empty(connected_store, col_name: str) -> None:
    """A created collection with no chunk is an empty read that worked, not an error."""
    await connected_store.ensure_collection(col_name, _DIM)

    assert await connected_store.list_chunk_ids(col_name, "default") == []
    assert await connected_store.list_chunk_ids(col_name, "default", include_restricted=True) == []
    assert await connected_store.read_excerpts(col_name, "default", [_chunk("a", 0).chunk_id]) == []


@pytest.mark.asyncio
async def test_list_chunk_ids_sorted_by_chunk_id_after_compaction(connected_store, col_name: str) -> None:
    """S82 — the order is the id order whatever the row order, before and after the fragments are compacted."""
    await connected_store.ensure_collection(col_name, _DIM)
    # One fragment per ingest, written in descending id order, so the stored row order is the reverse of the id order.
    for letter in ("c", "b", "a"):
        await connected_store.ingest_chunks(col_name, [_chunk(letter, 1), _chunk(letter, 0)])
    expected = sorted(f"{_doc_id(letter)}-{idx:06d}" for letter in "abc" for idx in (0, 1))
    assert await _physical_chunk_ids(connected_store, col_name) != expected, "the rows must be stored out of order"

    assert await connected_store.list_chunk_ids(col_name, "default") == expected

    table = await connected_store._require_connected().open_table(col_name)
    await table.optimize()

    assert await connected_store.list_chunk_ids(col_name, "default") == expected


@pytest.mark.asyncio
async def test_list_chunk_ids_is_empty_for_an_absent_collection(connected_store) -> None:
    assert await connected_store.list_chunk_ids("never-created-collection", "default") == []


@pytest.mark.asyncio
async def test_read_excerpts_returns_texts_in_the_requested_order(connected_store, col_name: str) -> None:
    """The result follows the order of the ids asked for, not the stored order or the id order."""
    await connected_store.ensure_collection(col_name, _DIM)
    chunks = [_chunk("a", 0), _chunk("a", 1), _chunk("b", 0), _chunk("c", 0)]
    await connected_store.ingest_chunks(col_name, chunks)
    asked = [chunks[3], chunks[0], chunks[2]]

    excerpts = await connected_store.read_excerpts(col_name, "default", [c.chunk_id for c in asked])

    assert excerpts == [c.text for c in asked]


@pytest.mark.asyncio
async def test_read_excerpts_skips_an_id_that_is_no_longer_stored(connected_store, col_name: str) -> None:
    """A chunk deleted between the listing and the read is left out; the other excerpts keep their order."""
    await connected_store.ensure_collection(col_name, _DIM)
    stored = [_chunk("a", 0), _chunk("a", 1)]
    await connected_store.ingest_chunks(col_name, stored)
    vanished = _chunk("b", 0)

    excerpts = await connected_store.read_excerpts(
        col_name, "default", [stored[1].chunk_id, vanished.chunk_id, stored[0].chunk_id]
    )

    assert excerpts == [stored[1].text, stored[0].text]


@pytest.mark.parametrize("restricting_acl", [["team-x"], []], ids=["acl", "deny-all"])
@pytest.mark.asyncio
async def test_read_excerpts_leaves_out_a_chunk_that_turned_restricted_after_it_was_listed(
    connected_store, col_name: str, restricting_acl: list[str]
) -> None:
    """A re-ingest keeps the chunk id but can add an ACL (deny-all included); its text is not read unless allowed."""
    await connected_store.ensure_collection(col_name, _DIM)
    open_chunk = _chunk("a", 0)
    await connected_store.ingest_chunks(col_name, [open_chunk])
    listed = await connected_store.list_chunk_ids(col_name, "default")
    assert listed == [open_chunk.chunk_id]

    await connected_store.delete_document(col_name, open_chunk.doc_id)
    restricted = _chunk("a", 0, acl=restricting_acl, text="restricted text")
    await connected_store.ingest_chunks(col_name, [restricted])

    assert await connected_store.read_excerpts(col_name, "default", listed) == []
    assert await connected_store.read_excerpts(col_name, "default", listed, include_restricted=True) == [
        restricted.text
    ]


@pytest.mark.asyncio
async def test_read_excerpts_is_empty_for_no_ids_and_for_an_absent_collection(connected_store, col_name: str) -> None:
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [_chunk("a", 0)])

    assert await connected_store.read_excerpts(col_name, "default", []) == []
    assert await connected_store.read_excerpts("never-created-collection", "default", [_chunk("a", 0).chunk_id]) == []


@pytest.mark.asyncio
async def test_port_reads_refuse_a_collection_owned_by_another_namespace(connected_store, col_name: str) -> None:
    """The reads follow the ownership rule of the other store calls: the owner's namespace reads, another is refused."""
    chunk = _chunk("a", 0)
    await connected_store.ensure_collection(col_name, _DIM, namespace=_NAMESPACE_OWNER)
    await connected_store.ingest_chunks(
        col_name, [chunk], embedding_model=_EMBEDDING_MODEL, namespace=_NAMESPACE_OWNER
    )

    assert await connected_store.list_chunk_ids(col_name, _NAMESPACE_OWNER) == [chunk.chunk_id]
    assert await connected_store.read_excerpts(col_name, _NAMESPACE_OWNER, [chunk.chunk_id]) == [chunk.text]
    with pytest.raises(CollectionOwnershipError):
        await connected_store.list_chunk_ids(col_name, _NAMESPACE_OTHER)
    with pytest.raises(CollectionOwnershipError):
        await connected_store.read_excerpts(col_name, _NAMESPACE_OTHER, [chunk.chunk_id])


@pytest.mark.asyncio
async def test_port_reads_reject_an_invalid_collection_name(connected_store) -> None:
    with pytest.raises(ValueError):
        await connected_store.list_chunk_ids("not a valid name!", "default")
    with pytest.raises(ValueError):
        await connected_store.read_excerpts("not a valid name!", "default", [_chunk("a", 0).chunk_id])


@pytest.mark.asyncio
async def test_store_read_error_is_a_typed_error_naming_the_collection(connected_store, col_name: str, monkeypatch) -> None:
    """A failing LanceDB read surfaces as ``StoreReadError`` (cause chained), never as an empty result."""
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [_chunk("a", 0)])
    db = connected_store._require_connected()
    real_open_table = db.open_table

    async def failing_open_table(name: str, *args, **kwargs):
        if name == col_name:
            raise OSError("disk went away")
        return await real_open_table(name, *args, **kwargs)

    monkeypatch.setattr(db, "open_table", failing_open_table)

    with pytest.raises(StoreReadError, match=col_name) as listing:
        await connected_store.list_chunk_ids(col_name, "default")
    assert isinstance(listing.value.__cause__, OSError)
    with pytest.raises(StoreReadError, match=col_name):
        await connected_store.read_excerpts(col_name, "default", [_chunk("a", 0).chunk_id])
