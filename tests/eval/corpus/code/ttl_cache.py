"""TTL-aware in-memory cache with automatic expiry."""
from __future__ import annotations
import time
from typing import Any


class TTLCache:
    """Store values with a per-entry time-to-live."""

    def __init__(self, default_ttl: float = 300.0) -> None:
        self._default_ttl = default_ttl
        self._store: dict[str, tuple[Any, float]] = {}  # key -> (value, expires_at)

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        expires = time.monotonic() + (ttl if ttl is not None else self._default_ttl)
        self._store[key] = (value, expires)

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._store.get(key)
        if entry is None:
            return default
        value, expires = entry
        if time.monotonic() > expires:
            del self._store[key]
            return default
        return value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def purge_expired(self) -> int:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]
        return len(expired)

    def __len__(self) -> int:
        return len(self._store)
