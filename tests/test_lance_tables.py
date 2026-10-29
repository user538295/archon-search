"""LanceDB pages ``list_tables()`` (default 100 names); every caller must see all tables."""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.lance_session import list_all_table_names
from archon_search.store import SearchStore

# One more than LanceDB's default page size (100) forces a second page.
TABLE_COUNT = 101
EMBEDDING_DIM = 4


@pytest.mark.asyncio
async def test_list_all_table_names_spans_every_page(tmp_path: Path) -> None:
    import lancedb

    db = await lancedb.connect_async(tmp_path)
    expected = {f"t{i}" for i in range(TABLE_COUNT)}
    for name in expected:
        await db.create_table(name, data=[{"a": 1}])

    assert set(await list_all_table_names(db)) == expected


@pytest.mark.asyncio
async def test_list_collections_and_drop_see_collections_past_first_page(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        names = {f"col-{i:03d}" for i in range(TABLE_COUNT)}
        for name in names:
            await store.ensure_collection(name, EMBEDDING_DIM)

        assert {c.name for c in await store.list_collections()} == names

        last = max(names)
        await store.drop_collection(last, namespace=DEFAULT_NAMESPACE)
        assert last not in {c.name for c in await store.list_collections()}
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_list_all_table_names_raises_on_non_advancing_page_token() -> None:
    from unittest.mock import AsyncMock, MagicMock

    stuck_page = MagicMock(tables=["a"], page_token="same")
    db = MagicMock(list_tables=AsyncMock(return_value=stuck_page))

    with pytest.raises(RuntimeError, match="non-advancing page_token"):
        await list_all_table_names(db)
