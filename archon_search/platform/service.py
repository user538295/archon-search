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
    def start(self, dry_run: bool = False) -> int: ...

    @abstractmethod
    def stop(self, dry_run: bool = False) -> int: ...

    def restart(self, dry_run: bool = False) -> None:
        self.stop(dry_run=dry_run)
        self.start(dry_run=dry_run)

    @abstractmethod
    def status(self) -> ServiceStatus: ...

    @abstractmethod
    def register(self, dry_run: bool = False) -> None: ...

    @abstractmethod
    def unregister(self, dry_run: bool = False) -> None: ...

    def pre_activate_cleanup(self, dry_run: bool = False) -> None:
        """Stop any running instance before registering a new service file."""
        try:
            self.stop(dry_run=dry_run)
        except Exception:
            pass
