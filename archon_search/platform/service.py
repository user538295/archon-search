"""SearchServiceLifecycle ABC for archon-search platform management."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from archon_search import paths

# Bounds for stop() to wait for the OS to actually tear the service down.
# `launchctl unload` / `systemctl --user stop` return before the process has
# fully exited and released its listening socket, so a subsequent status/health
# probe can still hit the dying server (S04). stop() polls status() until the
# service reports not-running before returning.
_STOP_WAIT_TIMEOUT_S = 10.0
_STOP_POLL_INTERVAL_S = 0.2

_SECRETS_ENV_FILENAME = ".secrets.env"


def get_secrets_env_file() -> Path:
    """Return the provider-key env file the wizard creates and the managed service loads.

    Resolved fresh on every call from ``paths.get_data_dir()`` so
    ``ARCHON_SEARCH_DATA_DIR`` relocates it with the rest of the runtime state.
    """
    return paths.get_data_dir() / _SECRETS_ENV_FILENAME


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
    def register(self, dry_run: bool = False, *, config_path: str) -> None: ...

    @abstractmethod
    def unregister(self, dry_run: bool = False) -> None: ...

    def validate_registration(self, *, config_path: str) -> None:
        """Raise ``RuntimeError`` if ``register(config_path=...)`` would write a service the OS cannot run.

        Side-effect free, so callers run it before stopping the current service.
        The default accepts everything; platforms that validate override it.
        """

    def replace_registration(self, dry_run: bool = False, *, config_path: str) -> None:
        """Replace the registered service: validate, stop any running instance, then register.

        A registration ``validate_registration()`` refuses raises before anything is
        stopped, so the running service is left untouched.
        """
        self.validate_registration(config_path=config_path)
        self.pre_activate_cleanup(dry_run=dry_run)
        self.register(dry_run=dry_run, config_path=config_path)

    def pre_activate_cleanup(self, dry_run: bool = False) -> None:
        """Stop any running instance before registering a new service file."""
        try:
            self.stop(dry_run=dry_run)
        except Exception:
            pass
