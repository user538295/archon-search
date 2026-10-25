"""Tests for ACL column in LanceDB chunk table schema ."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pyarrow as pa
import pytest


@pytest.mark.asyncio
async def test_new_collection_has_acl_column(tmp_path):
    """ensure_collection() creates a table whose schema contains an acl field of type list<utf8>, nullable."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("test_acl", embedding_dim=4)
        db = store._require_connected()
        table = await db.open_table("test_acl")
        schema: pa.Schema = await table.schema()

        assert "acl" in schema.names, "acl column missing from chunk table schema"
        acl_field = schema.field("acl")
        assert pa.types.is_list(acl_field.type), f"acl should be list type, got {acl_field.type}"
        assert acl_field.type.value_type == pa.utf8(), (
            f"acl list value type should be utf8, got {acl_field.type.value_type}"
        )
        assert acl_field.nullable, "acl field should be nullable"
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# migrate_acl tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migrate_acl_adds_column_to_existing_tables(tmp_path):
    """migrate_acl() adds acl column to existing chunk tables that lack it."""
    import lancedb
    import pyarrow as pa

    from archon_search.store import SearchStore

    db_path = tmp_path / "db"

    # Create a table WITHOUT the acl column to simulate a pre-ACL collection.
    schema_without_acl = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("chunk_id", pa.utf8()),
            pa.field("text", pa.utf8()),
            pa.field("vector", pa.list_(pa.float32(), 4)),
            pa.field("source_path", pa.utf8()),
            pa.field("indexed_at", pa.utf8()),
            pa.field("file_type", pa.utf8()),
            pa.field("language", pa.utf8()),
            pa.field("metadata", pa.utf8()),
            pa.field("custom_score", pa.float32()),
            pa.field("ingested_by", pa.utf8()),
            pa.field("updated_at", pa.utf8()),
            # acl column intentionally omitted
        ]
    )

    # Also create the meta table so migrate_acl can discover the collection.
    meta_schema = pa.schema(
        [
            pa.field("name", pa.utf8()),
            pa.field("description", pa.utf8()),
            pa.field("centroid_json", pa.utf8()),
            pa.field("doc_count", pa.int64()),
            pa.field("chunk_count", pa.int64()),
            pa.field("embedding_model", pa.utf8()),
            pa.field("last_indexed", pa.utf8()),
            pa.field("last_described", pa.utf8()),
            pa.field("described_at_doc_count", pa.int64()),
            pa.field("namespace", pa.utf8()),
        ]
    )

    db_path.mkdir(parents=True, exist_ok=True)
    raw_db = await lancedb.connect_async(str(db_path))
    # Create chunk table without acl
    await raw_db.create_table("old_collection", schema=schema_without_acl)
    # Register in meta table
    await raw_db.create_table(
        "_archon_collection_meta",
        data=[
            {
                "name": "old_collection",
                "description": "",
                "centroid_json": "",
                "doc_count": 0,
                "chunk_count": 0,
                "embedding_model": "test",
                "last_indexed": "",
                "last_described": "",
                "described_at_doc_count": -1,
                "namespace": "default",
            }
        ],
        schema=meta_schema,
    )
    raw_db.close()

    # Now run migrate_acl via SearchStore
    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.migrate_acl()

        db = store._require_connected()
        table = await db.open_table("old_collection")
        schema: pa.Schema = await table.schema()

        assert "acl" in schema.names, "acl column should have been added by migrate_acl()"
        acl_field = schema.field("acl")
        assert pa.types.is_list(acl_field.type), f"acl should be list type, got {acl_field.type}"
        assert acl_field.nullable, "acl field should be nullable"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_acl_idempotent(tmp_path):
    """Calling migrate_acl() twice does not raise an error."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("my_col", embedding_dim=4)

        # Register in meta so migrate_acl sees it.
        from archon_search.collection_meta import CollectionMeta
        meta = CollectionMeta(
            name="my_col",
            description=None,
            centroid=None,
            doc_count=0,
            chunk_count=0,
            active_embedding_model="test",
            last_indexed=None,
            last_described=None,
            described_at_doc_count=None,
            namespace="default",
        )
        await store.update_collection_meta(meta)

        # First call — column already exists (created by ensure_collection)
        await store.migrate_acl()
        # Second call — must be idempotent
        await store.migrate_acl()
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_acl_skips_when_no_meta_table(tmp_path):
    """migrate_acl() completes without error on a fresh store with no collections."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        # No meta table, no collections — should be a no-op
        await store.migrate_acl()
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_ingest_chunks_serializes_acl_field(tmp_path):
    """ingest_chunks() with ChunkRecord(acl=['ns1']) → persisted to LanceDB; read-back returns acl==['ns1']."""
    from archon_search._types import ChunkRecord
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("acl_test", embedding_dim=4)
        chunk = ChunkRecord(
            doc_id="a" * 64,
            chunk_id=("a" * 64) + "-000000",
            text="hello world",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_path="/tmp/test.md",
            indexed_at="2024-01-01T00:00:00+00:00",
            acl=["ns1"],
        )
        result = await store.ingest_chunks("acl_test", [chunk])
        assert result.chunks_ingested == 1

        db = store._require_connected()
        table = await db.open_table("acl_test")
        rows = await table.query().to_list()
        assert len(rows) == 1
        assert rows[0]["acl"] == ["ns1"], f"Expected acl==['ns1'], got {rows[0]['acl']}"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_ingest_chunks_serializes_deny_all_acl(tmp_path):
    """ChunkRecord(acl=[]) persisted to LanceDB → read-back returns acl==[] (not None)."""
    from archon_search._types import ChunkRecord
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("deny_all_test", embedding_dim=4)
        chunk = ChunkRecord(
            doc_id="b" * 64,
            chunk_id=("b" * 64) + "-000000",
            text="top secret content",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_path="/tmp/secret.md",
            indexed_at="2024-01-01T00:00:00+00:00",
            acl=[],  # deny-all
        )
        result = await store.ingest_chunks("deny_all_test", [chunk])
        assert result.chunks_ingested == 1

        db = store._require_connected()
        table = await db.open_table("deny_all_test")
        rows = await table.query().to_list()
        assert len(rows) == 1
        assert rows[0]["acl"] == [], f"Expected acl==[], got {rows[0]['acl']}"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_app_lifespan_calls_startup_migrations(tmp_path):
    """lifespan startup calls _run_startup_migrations() which internally applies all migrations.

    BE-6 consolidated the five direct migrate_*() calls into _run_startup_migrations().
    This test verifies the lifespan wires up _run_startup_migrations correctly.
    """
    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    config = SearchConfig(db_path="/tmp/test_lifespan_acl")
    # Isolate the jobs file under tmp_path so the test never touches
    # the developer's real ~/.archon-search (C9 Task 2.4 iterative review).
    job_store = JobStore(path=tmp_path / "jobs.json")

    app = create_app(config, job_store)

    # Replace the real store with a mock.
    mock_store = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    # Patch telemetry writer attribute to avoid AttributeError in shutdown path.
    app.state.telemetry_writer = None
    app.state._background_tasks = set()

    # Drive the lifespan manually.
    lifespan_ctx = app.router.lifespan_context(app)
    async with lifespan_ctx:
        pass

    # Verify _run_startup_migrations() is called during startup (after connect)
    call_names = [c[0] for c in mock_store.method_calls]
    assert "_run_startup_migrations" in call_names, "_run_startup_migrations() must be called during startup"
    assert "connect" in call_names, "connect() must be called during startup"
    connect_idx = call_names.index("connect")
    migration_idx = call_names.index("_run_startup_migrations")
    assert migration_idx > connect_idx, "_run_startup_migrations() must be called after connect()"


# ---------------------------------------------------------------------------
# hybrid_search_with_trace returns acl field in ScoredSearchCandidate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_and_hybrid_search_with_trace_returns_acl_field(tmp_path):
    """hybrid_search_with_trace() maps acl from stored row into ScoredSearchCandidate.acl."""
    from archon_search._types import ChunkRecord
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("hs_acl_test", embedding_dim=4)
        chunk = ChunkRecord(
            doc_id="c" * 64,
            chunk_id=("c" * 64) + "-000000",
            text="namespace restricted content",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_path="/tmp/restricted.md",
            indexed_at="2024-01-01T00:00:00+00:00",
            acl=["ns1"],
        )
        await store.ingest_chunks("hs_acl_test", [chunk])
        await store.rebuild_fts_index("hs_acl_test")

        results = await store.hybrid_search_with_trace(
            "hs_acl_test",
            query_vector=[0.1, 0.2, 0.3, 0.4],
            query_text="namespace restricted",
            candidate_depth=5,
        )

        assert len(results) == 1
        assert results[0].acl == ["ns1"], f"Expected acl==['ns1'], got {results[0].acl}"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_hybrid_search_with_trace_null_acl_chunk(tmp_path):
    """hybrid_search_with_trace() returns acl=None for chunks ingested with acl=None."""
    from archon_search._types import ChunkRecord
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("hs_null_acl", embedding_dim=4)
        chunk = ChunkRecord(
            doc_id="d" * 64,
            chunk_id=("d" * 64) + "-000000",
            text="public content no acl",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_path="/tmp/public.md",
            indexed_at="2024-01-01T00:00:00+00:00",
            acl=None,
        )
        await store.ingest_chunks("hs_null_acl", [chunk])
        await store.rebuild_fts_index("hs_null_acl")

        results = await store.hybrid_search_with_trace(
            "hs_null_acl",
            query_vector=[0.1, 0.2, 0.3, 0.4],
            query_text="public content",
            candidate_depth=5,
        )

        assert len(results) == 1
        assert results[0].acl is None, f"Expected acl=None, got {results[0].acl}"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_hybrid_search_with_trace_row_missing_acl_column_defaults_none(tmp_path):
    """hybrid_search_with_trace() uses .get('acl') — missing acl column yields acl=None, no KeyError."""
    import lancedb
    import pyarrow as pa

    from archon_search.store import SearchStore

    db_path = tmp_path / "db"

    # Create a table WITHOUT the acl column to simulate a pre-ACL chunk table.
    schema_without_acl = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("chunk_id", pa.utf8()),
            pa.field("text", pa.utf8()),
            pa.field("vector", pa.list_(pa.float32(), 4)),
            pa.field("source_path", pa.utf8()),
            pa.field("indexed_at", pa.utf8()),
            pa.field("file_type", pa.utf8()),
            pa.field("language", pa.utf8()),
            pa.field("metadata", pa.utf8()),
            pa.field("custom_score", pa.float32()),
            pa.field("ingested_by", pa.utf8()),
            pa.field("updated_at", pa.utf8()),
            # acl column intentionally omitted
        ]
    )

    db_path.mkdir(parents=True, exist_ok=True)
    raw_db = await lancedb.connect_async(str(db_path))
    await raw_db.create_table(
        "legacy_col",
        data=[
            {
                "doc_id": "e" * 64,
                "chunk_id": ("e" * 64) + "-000000",
                "text": "legacy chunk without acl",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/legacy.md",
                "indexed_at": "2024-01-01T00:00:00+00:00",
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "archon-search-cli",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        schema=schema_without_acl,
    )
    raw_db.close()

    store = SearchStore(db_path)
    await store.connect()
    try:
        # Should not raise KeyError — must use .get("acl")
        results = await store.hybrid_search_with_trace(
            "legacy_col",
            query_vector=[0.1, 0.2, 0.3, 0.4],
            query_text="legacy chunk",
            candidate_depth=5,
        )

        assert len(results) == 1
        assert results[0].acl is None, f"Expected acl=None for missing column, got {results[0].acl}"
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# fetch_adjacent_chunks returns acl field in ChunkRecord
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_adjacent_chunks_returns_acl_field(tmp_path):
    """fetch_adjacent_chunks() maps acl from stored row into ChunkRecord.acl."""
    from archon_search._types import ChunkRecord
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("adj_acl_test", embedding_dim=4)
        doc_id = "f" * 64
        # Ingest three consecutive chunks; center is index 1
        chunks = [
            ChunkRecord(
                doc_id=doc_id,
                chunk_id=f"{doc_id}-{i:06d}",
                text=f"chunk {i}",
                vector=[0.1, 0.2, 0.3, 0.4],
                source_path="/tmp/doc.md",
                indexed_at="2024-01-01T00:00:00+00:00",
                acl=["ns1"],
            )
            for i in range(3)
        ]
        await store.ingest_chunks("adj_acl_test", chunks)

        adjacent = await store.fetch_adjacent_chunks(
            "adj_acl_test", doc_id, center_idx=1, window=1
        )

        assert len(adjacent) == 2
        for chunk in adjacent:
            assert chunk.acl == ["ns1"], f"Expected acl==['ns1'], got {chunk.acl}"
    finally:
        await store.disconnect()


_LEGACY_DOC_ID = "a" * 64


async def _create_pre_acl_table(db_path, name: str) -> None:
    """Create *name* with three chunks and no acl / TTL / scopes / G15 provenance columns (pre-migration)."""
    import lancedb
    import pyarrow as pa

    schema_without_acl = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("chunk_id", pa.utf8()),
            pa.field("text", pa.utf8()),
            pa.field("vector", pa.list_(pa.float32(), 4)),
            pa.field("source_path", pa.utf8()),
            pa.field("indexed_at", pa.utf8()),
            pa.field("file_type", pa.utf8()),
            pa.field("language", pa.utf8()),
            pa.field("metadata", pa.utf8()),
            pa.field("custom_score", pa.float32()),
            pa.field("ingested_by", pa.utf8()),
            pa.field("updated_at", pa.utf8()),
            # acl, expires_at, scopes, acl_source, acl_sidecar_path, acl_warning intentionally omitted
        ]
    )
    db_path.mkdir(parents=True, exist_ok=True)
    raw_db = await lancedb.connect_async(str(db_path))
    await raw_db.create_table(
        name,
        data=[
            {
                "doc_id": _LEGACY_DOC_ID,
                "chunk_id": f"{_LEGACY_DOC_ID}-{i:06d}",
                "text": f"legacy chunk {i}",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/tmp/legacy.md",
                "indexed_at": "2024-01-01T00:00:00+00:00",
                "file_type": "",
                "language": "",
                "metadata": "{}",
                "custom_score": None,
                "ingested_by": "archon-search-cli",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
            for i in range(3)
        ],
        schema=schema_without_acl,
    )
    raw_db.close()


@pytest.mark.asyncio
async def test_fetch_adjacent_chunks_missing_acl_defaults_none(tmp_path):
    """A pre-migration table without the ACL columns yields no ACL, scopes or provenance on neighbours."""
    from archon_search.store import SearchStore

    db_path = tmp_path / "db"
    await _create_pre_acl_table(db_path, "legacy_adj")

    store = SearchStore(db_path)
    await store.connect()
    try:
        adjacent = await store.fetch_adjacent_chunks(
            "legacy_adj", _LEGACY_DOC_ID, center_idx=1, window=1
        )

        assert len(adjacent) == 2
        for chunk in adjacent:
            assert (chunk.acl, chunk.scopes, chunk.acl_source, chunk.acl_sidecar_path) == (None, None, None, None)
            assert chunk.acl_warning == []
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_list_chunks_raw_on_pre_acl_table_exports_null_provenance(tmp_path):
    """Exporting a pre-migration table reads the absent ACL / scope / provenance columns as None / []."""
    from archon_search.store import SearchStore

    db_path = tmp_path / "db"
    await _create_pre_acl_table(db_path, "legacy_export")

    store = SearchStore(db_path)
    await store.connect()
    try:
        rows = [r async for r in store.list_chunks_raw("legacy_export", "default")]

        assert len(rows) == 3
        for row in rows:
            assert {k: row[k] for k in ("acl", "scopes", "acl_source", "acl_sidecar_path", "acl_warning")} == {
                "acl": None, "scopes": None, "acl_source": None, "acl_sidecar_path": None, "acl_warning": [],
            }
            assert row["expires_at"] is None
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# Read paths carry scopes + G15 ACL provenance from the stored row
# ---------------------------------------------------------------------------

_PROV_DOC_ID = "e" * 64
_PROV_SIDECAR_PATH = "docs/doc.md.acl"
_PROV_WARNINGS = ["acl sidecar entry 'deny-all' is not a valid namespace; ignored"]


def _provenance_chunks(count: int):
    from archon_search._types import ChunkRecord

    return [
        ChunkRecord(
            doc_id=_PROV_DOC_ID,
            chunk_id=f"{_PROV_DOC_ID}-{i:06d}",
            text=f"provenance chunk {i}",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_path="/tmp/doc.md",
            indexed_at="2024-01-01T00:00:00+00:00",
            acl=["default"],
            scopes=["user:alice"],
            acl_source="sidecar",
            acl_sidecar_path=_PROV_SIDECAR_PATH,
            acl_warning=list(_PROV_WARNINGS),
        )
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_fetch_adjacent_chunks_carries_scopes_and_acl_provenance(tmp_path):
    """Neighbour chunks keep the stored scopes and ACL provenance, not only the acl list."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("adj_prov", embedding_dim=4)
        await store.ingest_chunks("adj_prov", _provenance_chunks(3))

        adjacent = await store.fetch_adjacent_chunks("adj_prov", _PROV_DOC_ID, center_idx=1, window=1)

        assert len(adjacent) == 2
        for chunk in adjacent:
            assert chunk.scopes == ["user:alice"]
            assert (chunk.acl_source, chunk.acl_sidecar_path, chunk.acl_warning) == (
                "sidecar", _PROV_SIDECAR_PATH, _PROV_WARNINGS,
            )
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_hybrid_search_with_trace_results_carry_acl_provenance(tmp_path):
    """SearchStore.hybrid_search_with_trace results keep the stored ACL provenance, not only the acl list."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("hybrid_prov", embedding_dim=4)
        await store.ingest_chunks("hybrid_prov", _provenance_chunks(1))

        results = await store.hybrid_search_with_trace("hybrid_prov", [0.1, 0.2, 0.3, 0.4], "provenance", 5)

        assert len(results) == 1
        r = results[0]
        assert r.acl == ["default"]
        assert (r.acl_source, r.acl_sidecar_path, r.acl_warning) == ("sidecar", _PROV_SIDECAR_PATH, _PROV_WARNINGS)
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# get_acl_stats tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_acl_stats_counts_protected_and_open(tmp_path):
    """get_acl_stats() returns (2, 1) for 2 acl-protected and 1 open chunk."""
    from archon_search._types import ChunkRecord
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("stats_mixed", embedding_dim=4)
        doc_id = "a" * 64
        chunks = [
            ChunkRecord(
                doc_id=doc_id,
                chunk_id=f"{doc_id}-{i:06d}",
                text=f"chunk {i}",
                vector=[0.1, 0.2, 0.3, 0.4],
                source_path="/tmp/doc.md",
                indexed_at="2024-01-01T00:00:00+00:00",
                acl=["ns1"] if i < 2 else None,
            )
            for i in range(3)
        ]
        await store.ingest_chunks("stats_mixed", chunks)

        protected, open_ = await store.get_acl_stats("stats_mixed")
        assert protected == 2, f"Expected 2 protected, got {protected}"
        assert open_ == 1, f"Expected 1 open, got {open_}"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_get_acl_stats_empty_collection(tmp_path):
    """get_acl_stats() returns (0, 0) for a collection with no chunks."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("stats_empty", embedding_dim=4)

        protected, open_ = await store.get_acl_stats("stats_empty")
        assert protected == 0, f"Expected 0 protected, got {protected}"
        assert open_ == 0, f"Expected 0 open, got {open_}"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_get_acl_stats_all_open(tmp_path):
    """get_acl_stats() returns (0, N) when all chunks have acl=None."""
    from archon_search._types import ChunkRecord
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("stats_all_open", embedding_dim=4)
        doc_id = "b" * 64
        chunks = [
            ChunkRecord(
                doc_id=doc_id,
                chunk_id=f"{doc_id}-{i:06d}",
                text=f"chunk {i}",
                vector=[0.1, 0.2, 0.3, 0.4],
                source_path="/tmp/doc.md",
                indexed_at="2024-01-01T00:00:00+00:00",
                acl=None,
            )
            for i in range(3)
        ]
        await store.ingest_chunks("stats_all_open", chunks)

        protected, open_ = await store.get_acl_stats("stats_all_open")
        assert protected == 0, f"Expected 0 protected, got {protected}"
        assert open_ == 3, f"Expected 3 open, got {open_}"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_get_acl_stats_all_protected(tmp_path):
    """get_acl_stats() returns (N, 0) when all chunks have acl=["ns1"]."""
    from archon_search._types import ChunkRecord
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("stats_all_protected", embedding_dim=4)
        doc_id = "c" * 64
        chunks = [
            ChunkRecord(
                doc_id=doc_id,
                chunk_id=f"{doc_id}-{i:06d}",
                text=f"chunk {i}",
                vector=[0.1, 0.2, 0.3, 0.4],
                source_path="/tmp/doc.md",
                indexed_at="2024-01-01T00:00:00+00:00",
                acl=["ns1"],
            )
            for i in range(4)
        ]
        await store.ingest_chunks("stats_all_protected", chunks)

        protected, open_ = await store.get_acl_stats("stats_all_protected")
        assert protected == 4, f"Expected 4 protected, got {protected}"
        assert open_ == 0, f"Expected 0 open, got {open_}"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_get_acl_stats_collection_not_found(tmp_path):
    """get_acl_stats() returns (0, 0) when collection does not exist."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        protected, open_ = await store.get_acl_stats("nonexistent")
        assert protected == 0, f"Expected 0 protected, got {protected}"
        assert open_ == 0, f"Expected 0 open, got {open_}"
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# Chunk-ACL visibility: list_documents / count_visible_documents / get_chunk_visibility
# ---------------------------------------------------------------------------

_VIS_OPEN_DOC = "1" * 64
_VIS_TEAM_A_DOC = "2" * 64
_VIS_TEAM_B_DOC = "3" * 64
_VIS_MIXED_DOC = "4" * 64
_VIS_DENIED_DOC = "5" * 64


async def _seed_visibility_collection(store, name: str) -> None:
    """open / team-a only / team-b only / mixed (one team-a + one open chunk) / deny-all documents."""
    from archon_search._types import ChunkRecord

    def chunk(doc_id: str, idx: int, acl: list[str] | None, scopes: list[str] | None = None) -> ChunkRecord:
        return ChunkRecord(
            doc_id=doc_id,
            chunk_id=f"{doc_id}-{idx:06d}",
            text=f"text {doc_id[:1]} {idx}",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_path=f"/docs/{doc_id[:1]}.md",
            indexed_at="2024-01-01T00:00:00+00:00",
            acl=acl,
            scopes=scopes,
        )

    await store.ensure_collection(name, embedding_dim=4)
    await store.ingest_chunks(
        name,
        [
            chunk(_VIS_OPEN_DOC, 0, None),
            chunk(_VIS_TEAM_A_DOC, 0, ["team-a"]),
            chunk(_VIS_TEAM_B_DOC, 0, ["team-b"]),
            chunk(_VIS_MIXED_DOC, 0, ["team-a"], scopes=["user:secret"]),
            chunk(_VIS_MIXED_DOC, 1, None, scopes=["user:public"]),
            chunk(_VIS_DENIED_DOC, 0, []),
        ],
    )


@pytest.mark.asyncio
async def test_list_documents_omits_documents_hidden_from_the_namespace(tmp_path):
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await _seed_visibility_collection(store, "vis_list")

        items, next_cursor, total = await store.list_documents("vis_list", namespace="team-b")

        assert [i.doc_id for i in items] == sorted([_VIS_OPEN_DOC, _VIS_TEAM_B_DOC, _VIS_MIXED_DOC])
        assert next_cursor is None
        assert total == 3, "total must count visible documents only"
        mixed = next(i for i in items if i.doc_id == _VIS_MIXED_DOC)
        assert mixed.chunk_count == 1, "chunk_count of a partly visible document counts visible chunks only"
        assert mixed.scopes == ["user:public"], "scopes of hidden chunks must not leak"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_list_documents_a_different_namespace_sees_its_own_documents(tmp_path):
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await _seed_visibility_collection(store, "vis_list_a")

        items, _cursor, total = await store.list_documents("vis_list_a", namespace="team-a")

        assert {i.doc_id for i in items} == {_VIS_OPEN_DOC, _VIS_TEAM_A_DOC, _VIS_MIXED_DOC}
        assert total == 3
        mixed = next(i for i in items if i.doc_id == _VIS_MIXED_DOC)
        assert mixed.chunk_count == 2
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_list_documents_pagination_cursor_is_stable_over_hidden_documents(tmp_path):
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await _seed_visibility_collection(store, "vis_page")
        expected = sorted([_VIS_OPEN_DOC, _VIS_TEAM_B_DOC, _VIS_MIXED_DOC])

        first, cursor, total_first = await store.list_documents("vis_page", limit=2, namespace="team-b")
        assert [i.doc_id for i in first] == expected[:2]
        assert cursor == expected[1]
        second, cursor_2, total_second = await store.list_documents(
            "vis_page", limit=2, cursor=cursor, namespace="team-b"
        )
        assert [i.doc_id for i in second] == expected[2:]
        assert cursor_2 is None
        assert total_first == total_second == 3
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_list_documents_pre_acl_table_lists_every_document(tmp_path):
    """A table without the acl column predates the feature: nothing is hidden."""
    from archon_search.store import SearchStore

    db_path = tmp_path / "db"
    await _create_pre_acl_table(db_path, "vis_legacy")
    store = SearchStore(db_path)
    await store.connect()
    try:
        items, _cursor, total = await store.list_documents("vis_legacy", namespace="team-b")

        assert [i.doc_id for i in items] == [_LEGACY_DOC_ID]
        assert total == 1
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_count_visible_documents(tmp_path):
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await _seed_visibility_collection(store, "vis_count")

        assert await store.count_visible_documents("vis_count", "team-a") == 3
        assert await store.count_visible_documents("vis_count", "team-b") == 3
        assert await store.count_visible_documents("vis_count", "team-c") == 2
        assert await store.count_visible_documents("vis_count", "") == 2, "empty namespace sees open documents only"
        assert await store.count_visible_documents("missing", "team-b") == 0
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_get_chunk_visibility_reports_hidden_chunks_and_documents(tmp_path):
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await _seed_visibility_collection(store, "vis_hidden")

        visibility = await store.get_chunk_visibility("vis_hidden", "team-b")

        assert visibility.hidden_chunk_ids == {
            f"{_VIS_TEAM_A_DOC}-000000",
            f"{_VIS_MIXED_DOC}-000000",
            f"{_VIS_DENIED_DOC}-000000",
        }
        # The mixed document keeps a visible chunk, so the document itself stays visible.
        assert visibility.hidden_doc_ids == {_VIS_TEAM_A_DOC, _VIS_DENIED_DOC}
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_get_chunk_visibility_of_unprotected_or_missing_collections_hides_nothing(tmp_path):
    from archon_search.acl import ChunkVisibility
    from archon_search.store import SearchStore

    db_path = tmp_path / "db"
    await _create_pre_acl_table(db_path, "vis_legacy_hidden")
    store = SearchStore(db_path)
    await store.connect()
    try:
        assert await store.get_chunk_visibility("vis_legacy_hidden", "team-b") == ChunkVisibility()
        assert await store.get_chunk_visibility("missing", "team-b") == ChunkVisibility()
    finally:
        await store.disconnect()
