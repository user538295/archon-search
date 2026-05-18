"""Tests for ACL column in LanceDB chunk table schema (FEAT-044 Task 1.3)."""

from __future__ import annotations

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
