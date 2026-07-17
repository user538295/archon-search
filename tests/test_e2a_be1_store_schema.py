"""Tests for BE-1: store schema migrations for TTL and Scoping (E2a)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pytest

from archon_search.store import STORE_SCHEMA_VERSION, SearchStore
from archon_search._types import DocumentInfo, normalize_iso_utc

# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_store_schema_version_is_1():
    """STORE_SCHEMA_VERSION must be 1 after E2a bump."""
    assert STORE_SCHEMA_VERSION == 1


def test_both_migration_specs_have_introduced_at_1():
    """Both E2a MigrationSpec entries must have introduced_at=1."""
    specs = SearchStore._all_migrations()
    e2a_specs = [s for s in specs if s.name in ("migrate_expires_at_and_scopes", "migrate_default_ttl_seconds")]
    assert len(e2a_specs) == 2
    for spec in e2a_specs:
        assert spec.introduced_at == 1, f"{spec.name} has introduced_at={spec.introduced_at}, expected 1"


def test_schema_has_expires_at_and_scopes():
    """_schema() must contain expires_at (utf8, nullable) and scopes (list<utf8>, nullable)."""
    schema = SearchStore._schema(4)
    assert "expires_at" in schema.names
    assert "scopes" in schema.names

    expires_field = schema.field("expires_at")
    assert expires_field.type == pa.utf8()
    assert expires_field.nullable

    scopes_field = schema.field("scopes")
    assert pa.types.is_list(scopes_field.type)
    assert scopes_field.type.value_type == pa.utf8()
    assert scopes_field.nullable


def test_meta_schema_has_default_ttl_seconds():
    """_meta_schema() must contain default_ttl_seconds (int64, nullable)."""
    schema = SearchStore._meta_schema()
    assert "default_ttl_seconds" in schema.names
    field = schema.field("default_ttl_seconds")
    assert field.type == pa.int64()
    assert field.nullable


def test_document_info_default_scopes_is_empty_list():
    """DocumentInfo.scopes default value must be []."""
    doc = DocumentInfo(doc_id="x", source_path="/p", chunk_count=1, indexed_at="2024-01-01T00:00:00.000000Z")
    assert doc.scopes == []


# ---------------------------------------------------------------------------
# Integration tests (real LanceDB in tmp_path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.integration
async def test_migrate_expires_at_and_scopes_idempotent(tmp_path):
    """Running migrate_expires_at_and_scopes twice produces no error and no data change."""
    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.ensure_collection("col1", embedding_dim=4)

        # First run
        await store.migrate_expires_at_and_scopes()
        # Second run — must be idempotent
        await store.migrate_expires_at_and_scopes()

        db = store._require_connected()
        table = await db.open_table("col1")
        schema = await table.schema()
        assert "expires_at" in schema.names
        assert "scopes" in schema.names
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_migrate_default_ttl_seconds_idempotent(tmp_path):
    """Running migrate_default_ttl_seconds twice produces no error; column stays null for pre-E2a collections."""
    from archon_search.collection_meta import CollectionMeta

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        # Initialize the meta table by registering a collection
        await store.ensure_collection("col_ttl", embedding_dim=4)
        meta = CollectionMeta(
            name="col_ttl",
            namespace="default",
            active_embedding_model="test",
        )
        await store.update_collection_meta(meta)

        # First run
        await store.migrate_default_ttl_seconds()
        # Second run — must be idempotent
        await store.migrate_default_ttl_seconds()

        db = store._require_connected()
        meta_table = await db.open_table("_archon_collection_meta")
        schema = await meta_table.schema()
        assert "default_ttl_seconds" in schema.names
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_schema_version_upgrade_0_to_1_runs_both_migrations(tmp_path):
    """Opening a v0 store and applying pending migrations via apply_in_place_migrations upgrades to v1."""
    import lancedb

    # Build a v0 schema without the E2a columns
    old_schema = pa.schema([
        pa.field("doc_id", pa.utf8()),
        pa.field("chunk_id", pa.utf8()),
        pa.field("text", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), 4)),
        pa.field("source_path", pa.utf8()),
        pa.field("indexed_at", pa.utf8()),
        pa.field("file_type", pa.utf8()),
        pa.field("language", pa.utf8()),
        pa.field("metadata", pa.utf8()),
        pa.field("custom_score", pa.float32(), nullable=True),
        pa.field("ingested_by", pa.utf8()),
        pa.field("updated_at", pa.utf8()),
        pa.field("acl", pa.list_(pa.utf8()), nullable=True),
        # No expires_at, no scopes
    ])
    old_meta_schema = pa.schema([
        pa.field("name", pa.utf8()),
        pa.field("description", pa.utf8()),
        pa.field("centroid_json", pa.utf8()),
        pa.field("description_embedding_json", pa.utf8()),
        pa.field("doc_count", pa.int64()),
        pa.field("chunk_count", pa.int64()),
        pa.field("active_embedding_model", pa.utf8()),
        pa.field("pending_embedding_model", pa.utf8(), nullable=True),
        pa.field("needs_reindex", pa.bool_(), nullable=True),
        pa.field("reindex_job_id", pa.utf8(), nullable=True),
        # community_rebuild_job_id has no migration (recreation-only per GBC110 BE-4), so a
        # current-binary store always has this nullable column present, even at v0.
        pa.field("community_rebuild_job_id", pa.utf8(), nullable=True),
        # metadata_reindex_job_id has no migration (recreation-only per CSP120 BE-2), so a
        # current-binary store always has this nullable column present, even at v0.
        pa.field("metadata_reindex_job_id", pa.utf8(), nullable=True),
        pa.field("last_indexed", pa.utf8()),
        pa.field("last_described", pa.utf8()),
        pa.field("described_at_doc_count", pa.int64()),
        pa.field("namespace", pa.utf8()),
        pa.field("centroid_sum_json", pa.utf8(), nullable=True),
        pa.field("mutations_since_recompute", pa.int64(), nullable=True),
        pa.field("needs_recompute", pa.bool_(), nullable=True),
        pa.field("schema_version", pa.int64(), nullable=True),
        # No default_ttl_seconds
    ])

    db_path = tmp_path / "db"
    raw_db = await lancedb.connect_async(str(db_path))
    await raw_db.create_table("_archon_collection_meta", schema=old_meta_schema)
    await raw_db.create_table("mycol", schema=old_schema)

    # Add a meta row for "mycol" at schema_version=0
    meta_table = await raw_db.open_table("_archon_collection_meta")
    await meta_table.add([{
        "name": "mycol",
        "description": "",
        "centroid_json": "[]",
        "description_embedding_json": "[]",
        "doc_count": 0,
        "chunk_count": 0,
        "active_embedding_model": "test",
        "pending_embedding_model": None,
        "needs_reindex": None,
        "reindex_job_id": None,
        "community_rebuild_job_id": None,
        "metadata_reindex_job_id": None,
        "last_indexed": "2024-01-01T00:00:00.000000Z",
        "last_described": "2024-01-01T00:00:00.000000Z",
        "described_at_doc_count": 0,
        "namespace": "default",
        "centroid_sum_json": None,
        "mutations_since_recompute": None,
        "needs_recompute": None,
        "schema_version": 0,
    }])

    # Now open the store and apply migrations
    store = SearchStore(db_path)
    await store.connect()
    try:
        pending = await store.pending_migrations("mycol", "default")
        assert len(pending) == 2, f"Expected 2 pending migrations, got {len(pending)}: {[s.name for s in pending]}"

        await store.apply_in_place_migrations("mycol", "default", pending)

        # Verify chunk table has expires_at and scopes
        db = store._require_connected()
        chunk_table = await db.open_table("mycol")
        chunk_schema = await chunk_table.schema()
        assert "expires_at" in chunk_schema.names
        assert "scopes" in chunk_schema.names

        # Verify meta table has default_ttl_seconds
        meta = await db.open_table("_archon_collection_meta")
        meta_schema = await meta.schema()
        assert "default_ttl_seconds" in meta_schema.names

        # Verify schema_version is now 1
        meta_rows = await meta.query().to_list()
        col_row = next(r for r in meta_rows if r["name"] == "mycol")
        assert col_row["schema_version"] == STORE_SCHEMA_VERSION == 1
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_expiring_chunks_real_store(tmp_path):
    """Ingest chunk with expires_at; query_expiring_chunks returns it within the window."""
    from datetime import UTC

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("testcol", embedding_dim=4)

        # Apply E2a migrations
        pending = await store.pending_migrations("testcol", "default")
        if pending:
            await store.apply_in_place_migrations("testcol", "default", pending)

        now = datetime.now(UTC)
        future_expires = normalize_iso_utc(now + timedelta(hours=1))

        db = store._require_connected()
        table = await db.open_table("testcol")

        chunk_id = ("a" * 64) + "-000000"
        doc_id = "a" * 64
        now_iso = normalize_iso_utc(now)

        await table.add([{
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "text": "expiring chunk",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "source_path": "/tmp/test.txt",
            "indexed_at": now_iso,
            "file_type": "",
            "language": "",
            "metadata": "{}",
            "custom_score": None,
            "ingested_by": "cli",
            "updated_at": now_iso,
            "acl": None,
            "expires_at": future_expires,
            "scopes": None,
        }])

        # Query with 2-hour window
        items, next_cursor = await store.query_expiring_chunks("testcol", "default", within_seconds=7200, limit=10)
        assert len(items) == 1
        assert items[0]["chunk_id"] == chunk_id
        assert items[0]["expires_at"] == future_expires
        assert next_cursor is None
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_expiring_chunks_returns_within_window(tmp_path):
    """Chunks outside the window are excluded from query_expiring_chunks."""
    from datetime import UTC

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        pending = await store.pending_migrations("col", "default")
        if pending:
            await store.apply_in_place_migrations("col", "default", pending)

        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)

        db = store._require_connected()
        table = await db.open_table("col")

        # Chunk 1: expires in 1 hour (within 2h window)
        c1_id = "b" * 64
        c1_expires = normalize_iso_utc(now + timedelta(hours=1))
        # Chunk 2: expires in 3 hours (outside 2h window)
        c2_id = "c" * 64
        c2_expires = normalize_iso_utc(now + timedelta(hours=3))

        await table.add([
            {
                "doc_id": c1_id, "chunk_id": c1_id + "-000000",
                "text": "within", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/a.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": c1_expires, "scopes": None,
            },
            {
                "doc_id": c2_id, "chunk_id": c2_id + "-000000",
                "text": "outside", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/b.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": c2_expires, "scopes": None,
            },
        ])

        items, _ = await store.query_expiring_chunks("col", "default", within_seconds=7200, limit=10)
        returned_chunk_ids = [item["chunk_id"] for item in items]
        assert c1_id + "-000000" in returned_chunk_ids
        assert c2_id + "-000000" not in returned_chunk_ids
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_expiring_chunks_excludes_already_expired(tmp_path):
    """Chunks with expires_at < now are excluded from query_expiring_chunks."""
    from datetime import UTC

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        pending = await store.pending_migrations("col", "default")
        if pending:
            await store.apply_in_place_migrations("col", "default", pending)

        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)
        db = store._require_connected()
        table = await db.open_table("col")

        # Chunk already expired
        c_id = "d" * 64
        c_expires = normalize_iso_utc(now - timedelta(hours=1))
        # Valid chunk within window — proves the method can return results and we are not just checking an empty list
        valid_id = "da" * 32
        valid_expires = normalize_iso_utc(now + timedelta(hours=1))

        await table.add([
            {
                "doc_id": c_id, "chunk_id": c_id + "-000000",
                "text": "expired", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/e.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": c_expires, "scopes": None,
            },
            {
                "doc_id": valid_id, "chunk_id": valid_id + "-000000",
                "text": "valid", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/e2.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": valid_expires, "scopes": None,
            },
        ])

        items, _ = await store.query_expiring_chunks("col", "default", within_seconds=7200, limit=10)
        chunk_ids = [item["chunk_id"] for item in items]
        # The valid chunk must appear; the expired chunk must not
        assert valid_id + "-000000" in chunk_ids
        assert not any(item["chunk_id"] == c_id + "-000000" for item in items)
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_expiring_chunks_excludes_null_expires(tmp_path):
    """Chunks with expires_at=null are excluded from query_expiring_chunks."""
    from datetime import UTC

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        pending = await store.pending_migrations("col", "default")
        if pending:
            await store.apply_in_place_migrations("col", "default", pending)

        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)
        db = store._require_connected()
        table = await db.open_table("col")

        c_id = "e" * 64
        # Valid chunk within window — proves the method can return results and we are not just checking an empty list
        valid_id = "ea" * 32
        valid_expires = normalize_iso_utc(now + timedelta(hours=1))

        await table.add([
            {
                "doc_id": c_id, "chunk_id": c_id + "-000000",
                "text": "no expiry", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/f.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": None, "scopes": None,
            },
            {
                "doc_id": valid_id, "chunk_id": valid_id + "-000000",
                "text": "valid", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/f2.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": valid_expires, "scopes": None,
            },
        ])

        items, _ = await store.query_expiring_chunks("col", "default", within_seconds=7200, limit=10)
        chunk_ids = [item["chunk_id"] for item in items]
        # The valid chunk must appear; the null-expires chunk must not
        assert valid_id + "-000000" in chunk_ids
        assert not any(item["chunk_id"] == c_id + "-000000" for item in items)
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_documents_scopes_are_set_union(tmp_path):
    """list_documents returns scopes as sorted set-union of all chunk scopes for a document."""
    from datetime import UTC

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        pending = await store.pending_migrations("col", "default")
        if pending:
            await store.apply_in_place_migrations("col", "default", pending)

        now = datetime.now(UTC)
        db = store._require_connected()
        table = await db.open_table("col")

        doc_id = "f" * 64
        now_iso = normalize_iso_utc(now)

        await table.add([
            {
                "doc_id": doc_id, "chunk_id": doc_id + "-000000",
                "text": "chunk 0", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/g.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": None, "scopes": ["a"],
            },
            {
                "doc_id": doc_id, "chunk_id": doc_id + "-000001",
                "text": "chunk 1", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/g.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": None, "scopes": ["b"],
            },
            {
                "doc_id": doc_id, "chunk_id": doc_id + "-000002",
                "text": "chunk 2", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/g.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": None, "scopes": ["a", "c"],
            },
        ])

        items, _, _ = await store.list_documents("col", limit=10)
        assert len(items) == 1
        doc = items[0]
        assert doc.doc_id == doc_id
        # scopes must be sorted set-union: ["a", "b", "c"]
        assert doc.scopes == ["a", "b", "c"]
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# New tests added for code review fixes (C1-T-1 through C1-T-5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_do_ingest_on_pre_migration_table_drops_ttl_silently(tmp_path):
    """Ingesting a ChunkRecord with expires_at/scopes into a pre-E2a table silently drops TTL data.

    This verifies the has_ttl_cols=False path in _do_ingest.
    """
    import lancedb
    import pyarrow as pa

    from archon_search._types import ChunkRecord, normalize_iso_utc

    # Build an old-schema table (no expires_at/scopes columns)
    old_schema = pa.schema([
        pa.field("doc_id", pa.utf8()),
        pa.field("chunk_id", pa.utf8()),
        pa.field("text", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), 4)),
        pa.field("source_path", pa.utf8()),
        pa.field("indexed_at", pa.utf8()),
        pa.field("file_type", pa.utf8()),
        pa.field("language", pa.utf8()),
        pa.field("metadata", pa.utf8()),
        pa.field("custom_score", pa.float32(), nullable=True),
        pa.field("ingested_by", pa.utf8()),
        pa.field("updated_at", pa.utf8()),
        pa.field("acl", pa.list_(pa.utf8()), nullable=True),
        # No expires_at, no scopes
    ])

    db_path = tmp_path / "db"
    raw_db = await lancedb.connect_async(str(db_path))
    await raw_db.create_table("oldcol", schema=old_schema)
    raw_db.close()

    store = SearchStore(db_path)
    await store.connect()
    try:
        from datetime import UTC

        now = datetime.now(UTC)
        now_iso = normalize_iso_utc(now)
        doc_id = "a" * 64
        chunk = ChunkRecord(
            doc_id=doc_id,
            chunk_id=doc_id + "-000000",
            text="test chunk",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_path="/tmp/test.txt",
            indexed_at=now_iso,
            expires_at=normalize_iso_utc(now + timedelta(hours=1)),
            scopes=["user:alice"],
        )

        # Should succeed without error (has_ttl_cols=False path)
        db = store._require_connected()
        count = await store._do_ingest(db, "oldcol", [chunk])
        assert count == 1

        # Verify row was stored and TTL columns don't exist (as expected for old schema)
        table = await db.open_table("oldcol")
        rows = await table.query().to_list()
        assert len(rows) == 1
        assert rows[0]["doc_id"] == doc_id
        schema_names = (await table.schema()).names
        assert "expires_at" not in schema_names
        assert "scopes" not in schema_names
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_documents_on_pre_e2a_store_returns_empty_scopes(tmp_path):
    """list_documents on a table without the scopes column returns scopes=[] for all documents."""
    import lancedb
    import pyarrow as pa

    from archon_search._types import normalize_iso_utc

    # Build an old-schema table (no scopes column)
    old_schema = pa.schema([
        pa.field("doc_id", pa.utf8()),
        pa.field("chunk_id", pa.utf8()),
        pa.field("text", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), 4)),
        pa.field("source_path", pa.utf8()),
        pa.field("indexed_at", pa.utf8()),
        pa.field("file_type", pa.utf8()),
        pa.field("language", pa.utf8()),
        pa.field("metadata", pa.utf8()),
        pa.field("custom_score", pa.float32(), nullable=True),
        pa.field("ingested_by", pa.utf8()),
        pa.field("updated_at", pa.utf8()),
        pa.field("acl", pa.list_(pa.utf8()), nullable=True),
        # No scopes column
    ])

    now = datetime.now(timezone.utc)
    now_iso = normalize_iso_utc(now)
    doc_id = "b" * 64

    db_path = tmp_path / "db"
    raw_db = await lancedb.connect_async(str(db_path))
    tbl = await raw_db.create_table("oldcol", schema=old_schema)
    await tbl.add([{
        "doc_id": doc_id, "chunk_id": doc_id + "-000000",
        "text": "test", "vector": [0.1, 0.2, 0.3, 0.4],
        "source_path": "/h.txt", "indexed_at": now_iso,
        "file_type": "", "language": "", "metadata": "{}",
        "custom_score": None, "ingested_by": "cli", "updated_at": now_iso, "acl": None,
    }])
    raw_db.close()

    store = SearchStore(db_path)
    await store.connect()
    try:
        items, _, _ = await store.list_documents("oldcol", limit=10)
        assert len(items) == 1
        assert items[0].scopes == []  # pre-E2a: no scopes column → empty list
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_expiring_chunks_cursor_pagination(tmp_path):
    """query_expiring_chunks cursor pagination returns chunks in (expires_at, chunk_id) order."""
    from datetime import UTC

    from archon_search._types import normalize_iso_utc

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)
        pending = await store.pending_migrations("col", "default")
        if pending:
            await store.apply_in_place_migrations("col", "default", pending)

        now = datetime.now(UTC)
        db = store._require_connected()
        table = await db.open_table("col")

        # 3 chunks with different expires_at, 1h apart
        chunks_data = [
            ("c" * 64, normalize_iso_utc(now + timedelta(hours=1))),
            ("d" * 64, normalize_iso_utc(now + timedelta(hours=2))),
            ("e" * 64, normalize_iso_utc(now + timedelta(hours=3))),
        ]
        now_iso = normalize_iso_utc(now)
        for doc_id, expires in chunks_data:
            await table.add([{
                "doc_id": doc_id, "chunk_id": doc_id + "-000000",
                "text": "test", "vector": [0.1, 0.2, 0.3, 0.4],
                "source_path": "/i.txt", "indexed_at": now_iso,
                "file_type": "", "language": "", "metadata": "{}",
                "custom_score": None, "ingested_by": "cli", "updated_at": now_iso,
                "acl": None, "expires_at": expires, "scopes": None,
            }])

        # Page 1: limit=1
        items1, cursor1 = await store.query_expiring_chunks("col", "default", within_seconds=4 * 3600, limit=1)
        assert len(items1) == 1
        assert cursor1 is not None
        # First chunk should be the one expiring soonest
        assert items1[0]["expires_at"] == chunks_data[0][1]

        # Page 2
        items2, cursor2 = await store.query_expiring_chunks("col", "default", within_seconds=4 * 3600, limit=1, cursor=cursor1)
        assert len(items2) == 1
        assert cursor2 is not None
        assert items2[0]["expires_at"] == chunks_data[1][1]

        # Page 3
        items3, cursor3 = await store.query_expiring_chunks("col", "default", within_seconds=4 * 3600, limit=1, cursor=cursor2)
        assert len(items3) == 1
        assert cursor3 is None  # last page
        assert items3[0]["expires_at"] == chunks_data[2][1]
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_expiring_chunks_pre_migration_returns_empty(tmp_path):
    """query_expiring_chunks on a table without expires_at column returns ([], None)."""
    import lancedb
    import pyarrow as pa

    old_schema = pa.schema([
        pa.field("doc_id", pa.utf8()),
        pa.field("chunk_id", pa.utf8()),
        pa.field("text", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), 4)),
        pa.field("source_path", pa.utf8()),
        pa.field("indexed_at", pa.utf8()),
        pa.field("file_type", pa.utf8()),
        pa.field("language", pa.utf8()),
        pa.field("metadata", pa.utf8()),
        pa.field("custom_score", pa.float32(), nullable=True),
        pa.field("ingested_by", pa.utf8()),
        pa.field("updated_at", pa.utf8()),
        pa.field("acl", pa.list_(pa.utf8()), nullable=True),
    ])

    db_path = tmp_path / "db"
    raw_db = await lancedb.connect_async(str(db_path))
    await raw_db.create_table("oldcol", schema=old_schema)
    raw_db.close()

    store = SearchStore(db_path)
    await store.connect()
    try:
        items, cursor = await store.query_expiring_chunks("oldcol", "default", within_seconds=3600, limit=10)
        assert items == []
        assert cursor is None
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_expiring_chunks_raises_on_nonpositive_within_seconds(tmp_path):
    """query_expiring_chunks raises ValueError for within_seconds <= 0."""
    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store._run_startup_migrations()
        await store.ensure_collection("col", embedding_dim=4)

        import pytest as _pytest
        with _pytest.raises(ValueError, match="within_seconds must be positive"):
            await store.query_expiring_chunks("col", "default", within_seconds=0, limit=10)

        with _pytest.raises(ValueError, match="within_seconds must be positive"):
            await store.query_expiring_chunks("col", "default", within_seconds=-1, limit=10)
    finally:
        await store.disconnect()
