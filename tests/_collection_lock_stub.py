"""Give a mock ``SearchStore`` the real ``locked_collection_meta`` over a real collection lock.

The PATCH, in-place migrate and MCP ``update_collection`` routes change the meta row inside
``SearchStore.locked_collection_meta``; a mock store has no such method. The real one is bound to the mock, so it
acquires ``lock_for(name)``, reads the row with the mock's ``get_collection_meta`` and releases the lock as the real
store does.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from archon_search.store import SearchStore


def with_locked_collection_meta(store: MagicMock) -> MagicMock:
    """Bind ``SearchStore.locked_collection_meta`` to the mock *store* and return it.

    The mock's ``lock_for`` must return a real ``asyncio.Lock``: one already configured is kept, else a free one is set.
    """
    if not isinstance(store.lock_for.return_value, asyncio.Lock):
        store.lock_for = MagicMock(return_value=asyncio.Lock())
    store.locked_collection_meta = SearchStore.locked_collection_meta.__get__(store)
    return store
