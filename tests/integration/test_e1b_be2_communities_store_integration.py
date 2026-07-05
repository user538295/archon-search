"""Integration tests for BE-2: _archon_graph_{col}_communities table.

Uses real LanceDB in tmp_path. Tests verify:
- write_communities + get_communities_for_entities round-trip fidelity
- get_community_stats returns (0, None) before any write
- get_community_stats returns correct count and last_built_at after write
- list_community_representatives returns all communities with representative_chunk_ids populated

Run with:
    uv run pytest tests/integration/test_e1b_be2_communities_store_integration.py -v --no-cov
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from archon_search.graph_types import Community

pytestmark = pytest.mark.integration

_EMBEDDING_DIM = 4  # minimal dimension for tests not requiring real embeddings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _community(idx: int, entity_ids: list[str], chunk_ids: list[str]) -> Community:
    return Community(
        community_id=f"comm-{idx}",
        entity_ids=entity_ids,
        representative_chunk_ids=chunk_ids,
        built_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        summary_text=None,
    )


# ---------------------------------------------------------------------------
# test_write_and_read_communities
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_and_read_communities(tmp_path) -> None:
    """Write 3 Community objects; get_communities_for_entities round-trips correctly.

    Community 0: entities [A, B]
    Community 1: entities [C]
    Community 2: entities [D, E]

    Query for entity B → should return only Community 0.
    Query for entity C + D → should return Community 1 and Community 2.
    """
    from archon_search.graph_store import GraphStore

    col = "testcol"
    comm0 = _community(0, ["entity-A", "entity-B"], ["chunk-0a", "chunk-0b"])
    comm1 = _community(1, ["entity-C"], ["chunk-1a"])
    comm2 = _community(2, ["entity-D", "entity-E"], ["chunk-2a", "chunk-2b", "chunk-2c"])

    async def _run():
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_communities_table(col, ns="default")
            await gs.write_communities(col, [comm0, comm1, comm2], ns="default")

            # Query entity B → should return comm0 only
            result_b = await gs.get_communities_for_entities(col, ["entity-B"], ns="default")
            # Query entity C + D → should return comm1 and comm2
            result_cd = await gs.get_communities_for_entities(col, ["entity-C", "entity-D"], ns="default")
            # Query unknown entity → should return empty
            result_unknown = await gs.get_communities_for_entities(col, ["entity-Z"], ns="default")
            return result_b, result_cd, result_unknown
        finally:
            await gs.disconnect()

    result_b, result_cd, result_unknown = asyncio.run(_run())

    # Verify community B result
    assert len(result_b) == 1, f"Expected 1 community for entity-B, got {len(result_b)}"
    assert result_b[0].community_id == "comm-0"
    assert "entity-A" in result_b[0].entity_ids
    assert "entity-B" in result_b[0].entity_ids
    assert "chunk-0a" in result_b[0].representative_chunk_ids
    assert "chunk-0b" in result_b[0].representative_chunk_ids

    # Verify community C+D result
    assert len(result_cd) == 2, f"Expected 2 communities for entity-C+D, got {len(result_cd)}"
    comm_ids = {c.community_id for c in result_cd}
    assert "comm-1" in comm_ids
    assert "comm-2" in comm_ids

    # Verify unknown entity returns empty
    assert result_unknown == []


# ---------------------------------------------------------------------------
# test_get_community_stats_empty
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_community_stats_empty(tmp_path) -> None:
    """get_community_stats returns (0, None) when no communities have been built."""
    from archon_search.graph_store import GraphStore

    col = "emptycol"

    async def _run() -> tuple:
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            # Do NOT call ensure_communities_table — table does not exist
            return await gs.get_community_stats(col, ns="default")
        finally:
            await gs.disconnect()

    count, last_built = asyncio.run(_run())

    assert count == 0, f"Expected count=0, got {count}"
    assert last_built is None, f"Expected last_built=None, got {last_built}"


# ---------------------------------------------------------------------------
# test_get_community_stats_after_write
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_community_stats_after_write(tmp_path) -> None:
    """get_community_stats returns correct count and a non-null last_built_at after write."""
    from archon_search.graph_store import GraphStore

    col = "statscol"
    communities = [
        _community(i, [f"entity-{i}"], [f"chunk-{i}"]) for i in range(5)
    ]

    async def _run() -> tuple:
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_communities_table(col, ns="default")
            await gs.write_communities(col, communities, ns="default")
            return await gs.get_community_stats(col, ns="default")
        finally:
            await gs.disconnect()

    count, last_built = asyncio.run(_run())

    assert count == 5, f"Expected count=5, got {count}"
    assert last_built is not None, "Expected last_built to be a datetime, got None"
    assert isinstance(last_built, datetime), f"Expected datetime, got {type(last_built)}"


# ---------------------------------------------------------------------------
# test_list_community_representatives_all
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_community_representatives_all(tmp_path) -> None:
    """list_community_representatives returns all communities with representative_chunk_ids populated."""
    from archon_search.graph_store import GraphStore

    col = "repscol"
    communities = [
        _community(0, ["entity-A", "entity-B"], ["chunk-0a", "chunk-0b"]),
        _community(1, ["entity-C"], ["chunk-1a"]),
        _community(2, ["entity-D"], ["chunk-2a", "chunk-2b"]),
    ]

    async def _run() -> list[Community]:
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_communities_table(col, ns="default")
            await gs.write_communities(col, communities, ns="default")
            return await gs.list_community_representatives(col, ns="default")
        finally:
            await gs.disconnect()

    result = asyncio.run(_run())

    assert len(result) == 3, f"Expected 3 communities, got {len(result)}"
    ids = {c.community_id for c in result}
    assert ids == {"comm-0", "comm-1", "comm-2"}

    by_id = {c.community_id: c for c in result}
    assert by_id["comm-0"].representative_chunk_ids == ["chunk-0a", "chunk-0b"]
    assert by_id["comm-1"].representative_chunk_ids == ["chunk-1a"]
    assert by_id["comm-2"].representative_chunk_ids == ["chunk-2a", "chunk-2b"]


# ---------------------------------------------------------------------------
# test_write_communities_summary_text_roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_communities_summary_text_roundtrip(tmp_path) -> None:
    """Communities with and without summary_text round-trip correctly."""
    from archon_search.graph_store import GraphStore

    col = "summarycol"
    comm_with_summary = Community(
        community_id="comm-with",
        entity_ids=["entity-A"],
        representative_chunk_ids=["chunk-A"],
        built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        summary_text="This is a summary.",
    )
    comm_without_summary = Community(
        community_id="comm-without",
        entity_ids=["entity-B"],
        representative_chunk_ids=["chunk-B"],
        built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        summary_text=None,
    )

    async def _run():
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_communities_table(col, ns="default")
            await gs.write_communities(col, [comm_with_summary, comm_without_summary], ns="default")
            return await gs.list_community_representatives(col, ns="default")
        finally:
            await gs.disconnect()

    result = asyncio.run(_run())

    by_id = {c.community_id: c for c in result}
    assert by_id["comm-with"].summary_text == "This is a summary."
    assert by_id["comm-without"].summary_text is None


# ---------------------------------------------------------------------------
# test_write_communities_idempotent
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_communities_idempotent(tmp_path) -> None:
    """Running write_communities twice fully replaces the first run; old IDs are gone."""
    from archon_search.graph_store import GraphStore

    col = "idemcol"
    # v1: IDs comm-0, comm-1, comm-2
    communities_v1 = [_community(i, [f"entity-{i}"], [f"chunk-{i}"]) for i in range(3)]
    # v2: DIFFERENT IDs comm-10, comm-11 (not a subset of v1)
    communities_v2 = [_community(10 + i, [f"entity-new-{i}"], [f"chunk-new-{i}"]) for i in range(2)]

    async def _run() -> tuple:
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_communities_table(col, ns="default")
            await gs.write_communities(col, communities_v1, ns="default")
            count_after_first, _ = await gs.get_community_stats(col, ns="default")
            await gs.write_communities(col, communities_v2, ns="default")
            count_after_second, _ = await gs.get_community_stats(col, ns="default")
            remaining = await gs.list_community_representatives(col, ns="default")
            return count_after_first, count_after_second, remaining
        finally:
            await gs.disconnect()

    first, second, remaining = asyncio.run(_run())

    assert first == 3, f"Expected 3 after first write, got {first}"
    assert second == 2, f"Expected 2 after second write (full replace), got {second}"
    remaining_ids = {c.community_id for c in remaining}
    assert "comm-0" not in remaining_ids, "Old comm-0 should be gone after second write"
    assert "comm-10" in remaining_ids
    assert "comm-11" in remaining_ids


# ---------------------------------------------------------------------------
# test_write_communities_empty_list_clears_existing
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_communities_empty_list_clears_existing(tmp_path) -> None:
    """write_communities with an empty list deletes all existing communities (not a no-op)."""
    from archon_search.graph_store import GraphStore

    col = "clearcol"
    communities = [_community(i, [f"entity-{i}"], [f"chunk-{i}"]) for i in range(3)]

    async def _run():
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_communities_table(col, ns="default")
            await gs.write_communities(col, communities, ns="default")
            count_before, _ = await gs.get_community_stats(col, ns="default")
            # Write empty list — should clear all communities
            await gs.write_communities(col, [], ns="default")
            count_after, _ = await gs.get_community_stats(col, ns="default")
            return count_before, count_after
        finally:
            await gs.disconnect()

    before, after = asyncio.run(_run())
    assert before == 3
    assert after == 0, f"Expected 0 after writing empty list, got {after}"


# ---------------------------------------------------------------------------
# test_get_chunks_by_ids_returns_only_found
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_chunks_by_ids_returns_only_found(tmp_path) -> None:
    """SearchStore.get_chunks_by_ids returns only matching chunk IDs; missing IDs silently skipped."""
    import hashlib
    from datetime import datetime, timezone

    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.store import SearchStore

    col = "chunkscol"
    EMBEDDING_DIM = 4
    doc_id = hashlib.sha256(b"doc1").hexdigest()
    # Create 5 chunks: chunk_ids 0-4
    chunk_ids = [f"{doc_id}-{i:06d}" for i in range(5)]

    async def _run():
        db_path = str(tmp_path / "db")
        ss = SearchStore(db_path)
        await ss.connect()
        try:
            await ss.ensure_collection(col, EMBEDDING_DIM)
            chunks = [
                ChunkRecord(
                    doc_id=doc_id,
                    chunk_id=cid,
                    text=f"chunk text {i}",
                    vector=[float(i)] * EMBEDDING_DIM,
                    source_path="/data/doc1.txt",
                    indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
                    acl=None,
                )
                for i, cid in enumerate(chunk_ids)
            ]
            await ss.ingest_chunks(col, chunks)
            # Request 3 valid + 2 unknown IDs
            request_ids = chunk_ids[:3] + ["unknown_id_a" * 5, "unknown_id_b" * 5]
            return await ss.get_chunks_by_ids(col, request_ids)
        finally:
            await ss.disconnect()

    result = asyncio.run(_run())
    assert len(result) == 3, f"Expected 3 rows, got {len(result)}"
    returned_chunk_ids = {r["chunk_id"] for r in result}
    for cid in chunk_ids[:3]:
        assert cid in returned_chunk_ids


# ---------------------------------------------------------------------------
# test_get_chunks_for_doc_returns_all_for_doc
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_chunks_for_doc_returns_all_for_doc(tmp_path) -> None:
    """SearchStore.get_chunks_for_doc returns all chunks for doc_id, excluding other docs."""
    import hashlib
    from datetime import datetime, timezone

    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.store import SearchStore

    col = "docchunkscol"
    EMBEDDING_DIM = 4
    doc_a = hashlib.sha256(b"doc_a").hexdigest()
    doc_b = hashlib.sha256(b"doc_b").hexdigest()

    async def _run():
        db_path = str(tmp_path / "db")
        ss = SearchStore(db_path)
        await ss.connect()
        try:
            await ss.ensure_collection(col, EMBEDDING_DIM)
            # 4 chunks for doc_a, 2 for doc_b
            chunks_a = [
                ChunkRecord(
                    doc_id=doc_a,
                    chunk_id=f"{doc_a}-{i:06d}",
                    text=f"doc_a chunk {i}",
                    vector=[float(i)] * EMBEDDING_DIM,
                    source_path="/data/doc_a.txt",
                    indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
                    acl=None,
                )
                for i in range(4)
            ]
            chunks_b = [
                ChunkRecord(
                    doc_id=doc_b,
                    chunk_id=f"{doc_b}-{i:06d}",
                    text=f"doc_b chunk {i}",
                    vector=[float(i)] * EMBEDDING_DIM,
                    source_path="/data/doc_b.txt",
                    indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
                    acl=None,
                )
                for i in range(2)
            ]
            await ss.ingest_chunks(col, chunks_a + chunks_b)
            return await ss.get_chunks_for_doc(col, doc_a)
        finally:
            await ss.disconnect()

    result = asyncio.run(_run())
    assert len(result) == 4, f"Expected 4 chunks for doc_a, got {len(result)}"
    for row in result:
        assert row["doc_id"] == doc_a, f"Expected doc_a, got {row['doc_id']}"
