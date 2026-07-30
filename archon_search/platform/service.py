"""SearchServiceLifecycle ABC for archon-search platform management."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

# Bounds for stop() to wait for the OS to actually tear the service down.
# `launchctl unload` / `systemctl --user stop` return before the process has
# fully exited and released its listening socket, so a subsequent status/health
# probe can still hit the dying server (S04). stop() polls status() until the
# service reports not-running before returning.
_STOP_WAIT_TIMEOUT_S = 10.0
_STOP_POLL_INTERVAL_S = 0.2


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

    def _wait_until_stopped(self, timeout: float = _STOP_WAIT_TIMEOUT_S) -> bool:
        """Poll ``status()`` until the service reports not-running.

        Concrete ``stop()`` implementations call this after issuing the
        platform stop command so that, once this method returns True, the server
        is genuinely gone — ``status()`` reports stopped and ``GET /health`` is
        unreachable (S04).

        Returns True as soon as the service reports not-running, or False if
        ``timeout`` elapses while it is still up (rather than blocking forever).
        Callers must not treat a False result as a clean stop — it means the
        service may still be running.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.status().running:
                return True
            time.sleep(_STOP_POLL_INTERVAL_S)
        return False

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
