"""Tests for ACL column in LanceDB chunk table schema (FEAT-044 Task 1.3 & 1.4)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pyarrow as pa


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
# Task 1.4: migrate_acl() tests
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
            embedding_model="test",
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
async def test_app_lifespan_calls_migrate_acl():
    """lifespan startup calls migrate_acl() after migrate_namespace()."""
    from pathlib import Path
    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    config = SearchConfig(db_path="/tmp/test_lifespan_acl")
    job_store = JobStore()

    app = create_app(config, job_store)

    # Replace the real store with a mock.
    mock_store = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.migrate_acl = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    # Patch telemetry writer attribute to avoid AttributeError in shutdown path.
    app.state.telemetry_writer = None
    app.state._background_tasks = set()

    # Drive the lifespan manually.
    lifespan_ctx = app.router.lifespan_context(app)
    async with lifespan_ctx:
        pass

    # Verify call order
    call_names = [c[0] for c in mock_store.method_calls]
    assert "migrate_namespace" in call_names, "migrate_namespace() must be called during startup"
    assert "migrate_acl" in call_names, "migrate_acl() must be called during startup"

    ns_idx = call_names.index("migrate_namespace")
    acl_idx = call_names.index("migrate_acl")
    assert acl_idx > ns_idx, "migrate_acl() must be called after migrate_namespace()"
