"""Linux SystemdSearchService — manages archon_search.server as a systemd user service."""
from __future__ import annotations

import getpass
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from archon_search.platform.service import SearchServiceLifecycle, ServiceStatus

log = logging.getLogger("archon_search")

_SERVICE_NAME = "archon-search"

_UNIT_TEMPLATE = """\
[Unit]
Description=Archon Search Server (archon-search)
After=network.target

[Service]
ExecStart={python} -m archon_search.server
WorkingDirectory={cwd}
Environment=ARCHON_SEARCH_CONFIG={config_path}
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

    def start(self) -> None:
        try:
            result = self._run(["systemctl", "--user", "start", _SERVICE_NAME])
            if result.returncode != 0:
                raise RuntimeError(f"systemctl start failed (rc={result.returncode}): {result.stderr}")
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl binary not found") from exc

    def stop(self) -> None:
        try:
            result = self._run(["systemctl", "--user", "stop", _SERVICE_NAME])
            if result.returncode != 0:
                raise RuntimeError(f"systemctl stop failed (rc={result.returncode}): {result.stderr}")
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl binary not found") from exc

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
            log.exception("Failed to query archon-search systemd service status")
            return ServiceStatus(running=False, pid=None, uptime_seconds=None)

    @staticmethod
    def _parse_pid(stdout: str) -> int | None:
        match = re.search(r"MainPID=(\d+)", stdout)
        if not match:
            return None
        pid = int(match.group(1))
        return pid if pid != 0 else None

    def register(self) -> None:
        cwd = str(Path.home() / ".archon-search")
        config_path = str(Path.home() / ".archon-search" / "archon-search.toml")

        content = _UNIT_TEMPLATE.format(
            python=sys.executable,
            cwd=cwd,
            config_path=config_path,
        )

        try:
            self._unit_path.parent.mkdir(parents=True, exist_ok=True)
            self._unit_path.write_text(content)
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

    def restart(self) -> None:
        try:
            result = self._run(["systemctl", "--user", "restart", _SERVICE_NAME])
            if result.returncode != 0:
                raise RuntimeError(f"systemctl restart failed (rc={result.returncode}): {result.stderr}")
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl binary not found") from exc

    def unregister(self) -> None:
        try:
            self._run(["systemctl", "--user", "stop", _SERVICE_NAME])
            self._run(["systemctl", "--user", "disable", _SERVICE_NAME])
            if self._unit_path.exists():
                self._unit_path.unlink()
            self._run(["systemctl", "--user", "daemon-reload"])
        except Exception:
            log.warning("unregister cleanup failed (best-effort)", exc_info=True)
