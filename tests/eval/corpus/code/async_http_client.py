"""Async HTTP client using httpx with retry logic and timeout."""
import httpx
import asyncio
from typing import Any


class AsyncHttpClient:
    """Thin wrapper around httpx.AsyncClient with retry and timeout defaults."""

    def __init__(self, base_url: str, timeout: float = 10.0, retries: int = 3) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.retries = retries
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "AsyncHttpClient":
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.retries):
            try:
                assert self._client is not None
                return await self._client.get(path, **kwargs)
            except httpx.TransportError:
                if attempt == self.retries - 1:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError("unreachable")
