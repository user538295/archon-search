"""Tests for SearchStore.ping() — Task 1.2."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from archon_search.constants import PING_TTL_SECONDS
from archon_search.store import SearchStore


@pytest.fixture
def store(tmp_path):
    return SearchStore(tmp_path / "db")


@pytest.fixture
def connected_store(tmp_path):
    """Store with a mocked _db (no real LanceDB connection needed)."""
    s = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_tables_result = MagicMock()
    mock_tables_result.tables = []
    mock_db.list_tables = AsyncMock(return_value=mock_tables_result)
    s._db = mock_db
    return s, mock_db


@pytest.mark.asyncio
async def test_ping_returns_true_when_connected(connected_store):
    s, mock_db = connected_store
    result = await s.ping()
    assert result is True


@pytest.mark.asyncio
async def test_ping_returns_false_when_db_is_none(store):
    assert await store.ping() is False


@pytest.mark.asyncio
async def test_ping_returns_false_on_list_tables_exception(tmp_path):
    s = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_db.list_tables = AsyncMock(side_effect=RuntimeError("connection lost"))
    s._db = mock_db
    assert await s.ping() is False


@pytest.mark.asyncio
async def test_ping_returns_false_on_timeout(tmp_path):
    s = SearchStore(tmp_path / "db")
    mock_db = MagicMock()

    async def slow_tables():
        await asyncio.sleep(0.5)

    mock_db.list_tables = AsyncMock(side_effect=slow_tables)
    s._db = mock_db
    with patch("archon_search.store.PING_TIMEOUT_SECONDS", 0.01):
        result = await s.ping()
    assert result is False


@pytest.mark.asyncio
async def test_ping_ttl_cache_prevents_second_call_on_true(connected_store):
    s, mock_db = connected_store
    with patch("archon_search.store.PING_TTL_SECONDS", 60.0):
        await s.ping()
        await s.ping()
    mock_db.list_tables.assert_called_once()


@pytest.mark.asyncio
async def test_ping_ttl_cache_prevents_second_call_on_false(tmp_path):
    s = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_db.list_tables = AsyncMock(side_effect=RuntimeError("fail"))
    s._db = mock_db
    with patch("archon_search.store.PING_TTL_SECONDS", 60.0):
        await s.ping()
        await s.ping()
    mock_db.list_tables.assert_called_once()


@pytest.mark.asyncio
async def test_ping_ttl_cache_expires(tmp_path):
    s = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_tables_result = MagicMock()
    mock_tables_result.tables = []
    mock_db.list_tables = AsyncMock(return_value=mock_tables_result)
    s._db = mock_db
    t0 = 1000.0
    # Let ping() run normally for the first call so _ping_cache is written with a
    # real timestamp, then force monotonic to return a value past the TTL for the
    # second call's TTL check, guaranteeing the cache is seen as expired.
    await s.ping()
    assert mock_db.list_tables.call_count == 1
    cached_ts = s._ping_cache[0]  # timestamp written by ping 1
    t1 = cached_ts + PING_TTL_SECONDS + 0.1
    with patch("archon_search.store.time.monotonic", return_value=t1):
        await s.ping()
    assert mock_db.list_tables.call_count == 2


@pytest.mark.asyncio
async def test_ping_cache_reset_on_disconnect(tmp_path):
    s = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_tables_result = MagicMock()
    mock_tables_result.tables = []
    mock_db.list_tables = AsyncMock(return_value=mock_tables_result)
    s._db = mock_db
    with patch("archon_search.store.PING_TTL_SECONDS", 60.0):
        result1 = await s.ping()
    assert result1 is True
    assert mock_db.list_tables.call_count == 1
    await s.disconnect()
    assert s._ping_cache is None  # disconnect() cleared the cache
    result2 = await s.ping()
    assert result2 is False
    assert mock_db.list_tables.call_count == 1  # no second call


@pytest.mark.asyncio
async def test_ping_cache_reset_on_connect(tmp_path):
    s = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_tables_result = MagicMock()
    mock_tables_result.tables = []
    mock_db.list_tables = AsyncMock(return_value=mock_tables_result)
    s._db = mock_db
    # Prime the cache
    with patch("archon_search.store.PING_TTL_SECONDS", 60.0):
        await s.ping()
    assert mock_db.list_tables.call_count == 1
    # Call connect() — should reset cache
    with patch("lancedb.connect_async", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_db
        await s.connect()
    # After connect, cache is reset — next ping should call list_tables again
    # (within TTL of original cache, but cache was reset)
    with patch("archon_search.store.PING_TTL_SECONDS", 60.0):
        await s.ping()
    assert mock_db.list_tables.call_count == 2


@pytest.mark.asyncio
async def test_ping_cache_cleared_even_when_connect_fails(tmp_path):
    s = SearchStore(tmp_path / "db")
    s._ping_cache = (time.monotonic(), True)
    with patch("lancedb.connect_async", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            await s.connect()
    assert s._ping_cache is None


@pytest.mark.asyncio
async def test_ping_propagates_cancelled_error(tmp_path):
    s = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_db.list_tables = AsyncMock(side_effect=asyncio.CancelledError())
    s._db = mock_db
    with pytest.raises(asyncio.CancelledError):
        await s.ping()
    assert s._ping_cache is None


@pytest.mark.asyncio
async def test_ping_returns_false_when_tables_attribute_missing(tmp_path):
    """Verify that _ = result.tables is load-bearing: AttributeError → False."""
    s = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    result_obj = MagicMock(spec=[])  # spec=[] means no attributes — .tables raises AttributeError
    mock_db.list_tables = AsyncMock(return_value=result_obj)
    s._db = mock_db
    assert await s.ping() is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ping_true_against_live_store(tmp_path):
    s = SearchStore(tmp_path / "db")
    await s.connect()
    assert await s.ping() is True
    await s.disconnect()
    assert await s.ping() is False
