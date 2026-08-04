"""Linux SystemdSearchService — manages archon_search.server as a systemd user service."""
from __future__ import annotations

import getpass
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from archon_search.paths import get_data_dir
from archon_search.platform.service import _STOP_WAIT_TIMEOUT_S, SearchServiceLifecycle, ServiceStatus

log = logging.getLogger(__name__)

_SERVICE_NAME = "archon-search"

_UNIT_TEMPLATE = """\
[Unit]
Description=Archon Search Server (archon-search)
After=network.target

[Service]
ExecStart={python} -m archon_search.server
WorkingDirectory={cwd}
Environment=ARCHON_SEARCH_CONFIG={config_path}
Environment=ARCHON_SEARCH_DATA_DIR={data_dir}
EnvironmentFile=-%h/.archon-search/.secrets.env
Restart=always
RestartSec=5
Nice=10
CPUQuota=50%

[Install]
WantedBy=default.target
"""


class SystemdSearchService(SearchServiceLifecycle):
    """Manages the archon-search server as a Linux systemd user service."""

    @property
    def _unit_path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / "archon-search.service"

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(cmd, capture_output=True, text=True)

    def start(self, dry_run: bool = False) -> int:
        if dry_run:
            return 0
        try:
            result = self._run(["systemctl", "--user", "start", _SERVICE_NAME])
            if result.returncode != 0:
                raise RuntimeError(f"systemctl start failed (rc={result.returncode}): {result.stderr}")
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl binary not found") from exc
        return 0

    def stop(self, dry_run: bool = False) -> int:
        if dry_run:
            return 0
        try:
            result = self._run(["systemctl", "--user", "stop", _SERVICE_NAME])
            if result.returncode != 0:
                raise RuntimeError(f"systemctl stop failed (rc={result.returncode}): {result.stderr}")
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl binary not found") from exc
        # systemctl stop can return before the unit has fully deactivated and
        # released its socket; wait until the service is actually down so
        # /health is unreachable afterward (S04). Return 0 only when confirmed
        # down; 1 (with a WARNING) if the wait timed out, so callers can
        # distinguish a clean stop from a possibly-still-up one.
        if self._wait_until_stopped():
            return 0
        log.warning(
            "archon-search did not report stopped within %.0fs of stop; "
            "it may still be running",
            _STOP_WAIT_TIMEOUT_S,
        )
        return 1

    def status(self) -> ServiceStatus:
        try:
            is_active = self._run(["systemctl", "--user", "is-active", _SERVICE_NAME])
            running = is_active.stdout.strip() == "active"

            if not running:
                return ServiceStatus(running=False, pid=None, uptime_seconds=None)

            pid_result = self._run(
                ["systemctl", "--user", "show", _SERVICE_NAME, "--property=MainPID"]
            )
            pid = self._parse_pid(pid_result.stdout)
            if pid is None:
                return ServiceStatus(running=False, pid=None, uptime_seconds=None)

            return ServiceStatus(running=True, pid=pid, uptime_seconds=None)
        except Exception:
            log.debug("Failed to query archon-search systemd service status", exc_info=True)
            return ServiceStatus(running=False, pid=None, uptime_seconds=None)

    @staticmethod
    def _parse_pid(stdout: str) -> int | None:
        match = re.search(r"MainPID=(\d+)", stdout)
        if not match:
            return None
        pid = int(match.group(1))
        return pid if pid != 0 else None

    def register(self, dry_run: bool = False, config_path: str | None = None) -> None:
        if dry_run:
            return
        data_dir = get_data_dir()
        cwd = str(data_dir)
        # Honor the caller-supplied config path (e.g. `wizard --config`) so the
        # unit's ARCHON_SEARCH_CONFIG points at the config the installer just
        # wrote, not the hardcoded default (S206).
        config_path = config_path or str(data_dir / "archon-search.toml")

        content = _UNIT_TEMPLATE.format(
            python=sys.executable,
            cwd=cwd,
            config_path=config_path,
            data_dir=str(data_dir),
        )

        try:
            self._unit_path.parent.mkdir(parents=True, exist_ok=True)
            self._unit_path.write_text(content)  # noqa: durable-write
        except PermissionError as e:
            raise RuntimeError(f"Permission denied writing {self._unit_path}") from e

        try:
            reload = self._run(["systemctl", "--user", "daemon-reload"])
            if reload.returncode != 0:
                log.warning("daemon-reload returned rc=%d: %s", reload.returncode, reload.stderr)

            result = self._run(["systemctl", "--user", "enable", _SERVICE_NAME])
            if result.returncode != 0:
                log.warning("systemctl enable failed (rc=%d): %s", result.returncode, result.stderr)
                self._unit_path.unlink(missing_ok=True)
                self._run(["systemctl", "--user", "daemon-reload"])
                raise RuntimeError(f"systemctl enable failed (rc={result.returncode}): {result.stderr}")

            user = os.environ.get("USER") or getpass.getuser()
            linger = self._run(["loginctl", "enable-linger", user])
            if linger.returncode != 0:
                log.warning("loginctl enable-linger returned rc=%d: %s", linger.returncode, linger.stderr)
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl binary not found") from exc

    def restart(self, dry_run: bool = False) -> None:
        if dry_run:
            return
        try:
            result = self._run(["systemctl", "--user", "restart", _SERVICE_NAME])
            if result.returncode != 0:
                raise RuntimeError(f"systemctl restart failed (rc={result.returncode}): {result.stderr}")
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl binary not found") from exc

    def unregister(self, dry_run: bool = False) -> None:
        # Uninstall path — unlike stop(), this deliberately does NOT wait for the
        # unit to go down (S04's /health-unreachable guarantee is a stop()
        # contract; uninstall then removes the unit and makes no such promise).
        if dry_run:
            return
        try:
            self._run(["systemctl", "--user", "stop", _SERVICE_NAME])
            self._run(["systemctl", "--user", "disable", _SERVICE_NAME])
            if self._unit_path.exists():
                self._unit_path.unlink()
            self._run(["systemctl", "--user", "daemon-reload"])
        except Exception:
            log.warning("unregister cleanup failed (best-effort)", exc_info=True)
