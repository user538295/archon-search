"""Rolling-window statistics accumulator."""
from __future__ import annotations
from collections import deque


class RollingWindow:
    """Track the last N numeric samples and compute running statistics."""

    def __init__(self, size: int) -> None:
        if size <= 0:
            raise ValueError("size must be positive")
        self._size = size
        self._buf: deque[float] = deque(maxlen=size)

    def add(self, value: float) -> None:
        self._buf.append(value)

    def mean(self) -> float:
        if not self._buf:
            return 0.0
        return sum(self._buf) / len(self._buf)

    def maximum(self) -> float:
        return max(self._buf) if self._buf else float("-inf")

    def minimum(self) -> float:
        return min(self._buf) if self._buf else float("inf")

    def count(self) -> int:
        return len(self._buf)
