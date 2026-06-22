"""Integration test for startup migration via full app lifespan (D3 BE-6).

Verifies that _run_startup_migrations() runs on startup and that the
schema_version column is present in _archon_collection_meta after lifespan.
Uses make_real_app to exercise the full FastAPI lifespan path.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.integration.conftest import make_real_app


@pytest.mark.integration
def test_startup_migrations_schema_version_column_present(tmp_path, monkeypatch) -> None:
    """Starting the app via make_real_app runs startup migrations.

    Seeds a pre-D3 LanceDB meta table (without schema_version column), then
    starts the app via TestClient.  After the lifespan, schema_version must
    be present in _archon_collection_meta.
    """
    import lancedb
    import pyarrow as pa

    from archon_search.store import SearchStore

    db_path = str(tmp_path / "db")

    # Seed a pre-D3 meta table (no schema_version column) before the app starts.
    async def _seed() -> None:
        db = await lancedb.connect_async(db_path)
        old_schema = pa.schema(
            [f for f in SearchStore._meta_schema() if f.name != "schema_version"]
        )
        await db.create_table("_archon_collection_meta", schema=old_schema)

    asyncio.run(_seed())

    # Boot the app — lifespan calls _run_startup_migrations() which adds the column.
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        resp = client.get("/health")
        assert resp.status_code == 200

    # Verify the schema_version column was added by the lifespan migration.
    async def _check() -> list[str]:
        db = await lancedb.connect_async(db_path)
        tbl = await db.open_table("_archon_collection_meta")
        return (await tbl.schema()).names

    column_names = asyncio.run(_check())
    assert "schema_version" in column_names, (
        f"schema_version column missing after startup; columns present: {column_names}"
    )
