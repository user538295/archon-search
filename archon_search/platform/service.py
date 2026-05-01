"""SearchServiceLifecycle ABC for archon-search platform management."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ServiceStatus:
    running: bool
    pid: int | None
    uptime_seconds: float | None


class SearchServiceLifecycle(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    def restart(self) -> None:
        self.stop()
        self.start()

    @abstractmethod
    def status(self) -> ServiceStatus: ...

    @abstractmethod
    def register(self) -> None: ...

    @abstractmethod
    def unregister(self) -> None: ...
