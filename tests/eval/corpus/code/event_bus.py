"""Simple asyncio publish-subscribe event bus."""
from __future__ import annotations
import asyncio
from collections import defaultdict
from typing import Any, Callable, Coroutine


Handler = Callable[..., Coroutine[Any, Any, None]]


class EventBus:
    """In-process pub-sub bus for coroutine-based subscribers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event: str, handler: Handler) -> None:
        self._handlers[event].append(handler)

    def unsubscribe(self, event: str, handler: Handler) -> None:
        self._handlers[event] = [h for h in self._handlers[event] if h is not handler]

    async def publish(self, event: str, **payload: Any) -> None:
        handlers = list(self._handlers.get(event, []))
        await asyncio.gather(*(h(**payload) for h in handlers), return_exceptions=True)
