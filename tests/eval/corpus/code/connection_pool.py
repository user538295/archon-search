"""Generic async connection pool with acquire/release semantics."""
from __future__ import annotations
import asyncio
from typing import Any, AsyncContextManager, Callable, Coroutine, TypeVar

T = TypeVar("T")


class ConnectionPool:
    """Pool of reusable async connections."""

    def __init__(
        self,
        factory: Callable[[], Coroutine[Any, Any, T]],
        max_size: int = 10,
    ) -> None:
        self._factory = factory
        self._max_size = max_size
        self._pool: asyncio.Queue[T] = asyncio.Queue(maxsize=max_size)
        self._size = 0

    async def acquire(self) -> T:
        if not self._pool.empty():
            return self._pool.get_nowait()
        if self._size < self._max_size:
            conn = await self._factory()
            self._size += 1
            return conn
        # Wait for a connection to be returned
        return await self._pool.get()

    async def release(self, conn: T) -> None:
        await self._pool.put(conn)

    def connection(self) -> "_PoolContext[T]":
        return _PoolContext(self)


class _PoolContext(AsyncContextManager):
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._conn: Any = None

    async def __aenter__(self) -> Any:
        self._conn = await self._pool.acquire()
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        await self._pool.release(self._conn)
