"""The LanceDB connections run on a session with bounded caches.

A default session lets the metadata cache grow to 1 GiB and the index cache to 6 GiB. Every table
write caches the new version's manifest and transaction, so an ingest-heavy server's RSS climbed
without bound: about 12 KiB per meta-table write, measured as ~8 MB of RSS per ingest+delete cycle
of a 300-chunk collection that never levelled off.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import lancedb
import pyarrow as pa
import pytest

from archon_search import lance_session
from archon_search.graph_store import GraphStore
from archon_search.lance_session import (
    LANCE_INDEX_CACHE_BYTES,
    LANCE_METADATA_CACHE_BYTES,
    new_lance_session,
)
from archon_search.store import SearchStore

_WRITES = 300
_TEST_CACHE_BYTES = 256 * 1024


def test_new_lance_session_passes_the_bounded_cache_sizes(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, int]] = []

    def fake_session(**sizes: int) -> str:
        created.append(sizes)
        return "session"

    monkeypatch.setattr(lancedb, "Session", fake_session)

    assert new_lance_session() == "session"
    assert created == [
        {
            "index_cache_size_bytes": LANCE_INDEX_CACHE_BYTES,
            "metadata_cache_size_bytes": LANCE_METADATA_CACHE_BYTES,
        }
    ]


@pytest.mark.parametrize("make_store", [SearchStore, GraphStore], ids=["search_store", "graph_store"])
async def test_store_connect_opens_the_database_on_a_bounded_session(
    make_store: type[SearchStore | GraphStore], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(lancedb, "connect_async", connect)
    monkeypatch.setattr(lancedb, "Session", lambda **sizes: ("session", sizes))

    store = make_store(tmp_path / "db")
    await store.connect()

    _, kwargs = connect.call_args
    assert kwargs["session"] == (
        "session",
        {
            "index_cache_size_bytes": LANCE_INDEX_CACHE_BYTES,
            "metadata_cache_size_bytes": LANCE_METADATA_CACHE_BYTES,
        },
    )


async def test_session_metadata_cache_stays_within_its_bound_under_repeated_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lance_session, "LANCE_METADATA_CACHE_BYTES", _TEST_CACHE_BYTES)
    session = new_lance_session()
    db = await lancedb.connect_async(str(tmp_path), session=session)
    table = await db.create_table("t", schema=pa.schema([("key", pa.string()), ("value", pa.int64())]))

    for value in range(_WRITES):
        await table.merge_insert("key").when_matched_update_all().when_not_matched_insert_all().execute(
            [{"key": "row", "value": value}]
        )

    assert session.size_bytes <= 2 * _TEST_CACHE_BYTES
