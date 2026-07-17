"""CSP120 BE-2 — CollectionMeta.metadata_reindex_job_id integration test.

Verifies that metadata_reindex_job_id survives a write via update_collection_meta
on one SearchStore connection and a reload via get_collection_meta on a fresh
connection (cross-connection persistence).

Run with:
    uv run pytest tests/integration/test_csp120_be2_metadata_reindex_job_id.py -v --no-cov
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_metadata_reindex_job_id_persisted_across_store_connections(tmp_path: Path) -> None:
    """Write metadata_reindex_job_id via update_collection_meta on one SearchStore connection.

    Open a fresh connection; reload via get_collection_meta; verify field survives.
    """
    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import SearchStore

    db_path = tmp_path / "db_meta_reindex_id"

    # Write via a first connection.
    store_a = SearchStore(db_path)
    await store_a.connect()
    try:
        meta = CollectionMeta(name="meta-reindex-persist", metadata_reindex_job_id="job-persist-99")
        await store_a.update_collection_meta(meta)
    finally:
        await store_a.disconnect()

    # Open a second independent connection and verify the field survived.
    store_b = SearchStore(db_path)
    await store_b.connect()
    try:
        retrieved = await store_b.get_collection_meta("meta-reindex-persist")
        assert retrieved is not None
        assert retrieved.metadata_reindex_job_id == "job-persist-99"
    finally:
        await store_b.disconnect()
