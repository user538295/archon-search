"""Exponential-backoff retry decorator for async functions."""
from __future__ import annotations
import asyncio
import functools
import logging
from typing import Any, Callable, Coroutine, Type, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


def retry(
    *exc_types: Type[BaseException],
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
) -> Callable[[Callable[..., Coroutine[Any, Any, T]]], Callable[..., Coroutine[Any, Any, T]]]:
    """Retry an async function with exponential back-off on specified exceptions."""

    def decorator(fn: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            delay = base_delay
            for attempt in range(attempts):
                try:
                    return await fn(*args, **kwargs)
                except exc_types:
                    if attempt == attempts - 1:
                        raise
                    log.warning("Attempt %d failed; retrying in %.1fs", attempt + 1, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)
            raise RuntimeError("unreachable")

        return wrapper

    return decorator
