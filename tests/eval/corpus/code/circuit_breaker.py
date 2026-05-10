"""Circuit breaker pattern for async service calls."""
from __future__ import annotations
import asyncio
import time
from enum import Enum, auto
from typing import Any, Callable, Coroutine, TypeVar

T = TypeVar("T")


class State(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitBreaker:
    """Open the circuit after `failure_threshold` failures in a row.
    Attempt recovery after `recovery_timeout` seconds.
    """

    def __init__(
        self, failure_threshold: int = 5, recovery_timeout: float = 30.0
    ) -> None:
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._failures = 0
        self._state = State.CLOSED
        self._opened_at: float = 0.0

    async def call(
        self, fn: Callable[..., Coroutine[Any, Any, T]], *args: Any, **kwargs: Any
    ) -> T:
        if self._state == State.OPEN:
            if time.monotonic() - self._opened_at >= self._recovery_timeout:
                self._state = State.HALF_OPEN
            else:
                raise RuntimeError("Circuit is open")
        try:
            result = await fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        self._failures = 0
        self._state = State.CLOSED

    def _on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._state = State.OPEN
            self._opened_at = time.monotonic()
