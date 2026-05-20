"""macOS LaunchdSearchService — manages archon_search.server as a launchd daemon."""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

from archon_search.platform.service import SearchServiceLifecycle, ServiceStatus

log = logging.getLogger("archon_search")

_LABEL = "com.archon.search"

_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/sbin/taskpolicy</string>
        <string>-b</string>
        <string>{python}</string>
        <string>-m</string>
        <string>archon_search.server</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{cwd}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ARCHON_SEARCH_CONFIG</key>
        <string>{config_path}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>60</integer>
</dict>
</plist>
"""


class LaunchdSearchService(SearchServiceLifecycle):
    """Manages the archon-search server as a macOS launchd user agent."""

    @property
    def _plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / "com.archon.search.plist"

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(cmd, capture_output=True, text=True)

    def _is_loaded(self) -> bool:
        try:
            result = self._run(["launchctl", "list", _LABEL])
            return result.returncode == 0
        except FileNotFoundError:
            return False

    def register(self) -> None:
        cwd = str(Path.home() / ".archon-search")
        config_path = str(Path.home() / ".archon-search" / "archon-search.toml")
        log_path = str(Path.home() / ".archon-search" / "logs" / "archon-search.log")

        content = _PLIST_TEMPLATE.format(
            label=_LABEL,
            python=sys.executable,
            cwd=cwd,
            config_path=config_path,
            log_path=log_path,
        )

        try:
            self._plist_path.parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            self._plist_path.write_text(content)
        except PermissionError as e:
            raise RuntimeError(f"Permission denied writing {self._plist_path}") from e

    def unregister(self) -> None:
        if self._is_loaded():
            try:
                result = self._run(["launchctl", "unload", str(self._plist_path)])
                if result.returncode != 0:
                    log.warning("launchctl unload failed (rc=%d): %s", result.returncode, result.stderr)
            except FileNotFoundError as exc:
                raise RuntimeError("launchctl binary not found") from exc
        if self._plist_path.exists():
            self._plist_path.unlink()

    def start(self) -> None:
        if not self._plist_path.exists():
            raise RuntimeError(f"Plist not installed at {self._plist_path}")
        if self._is_loaded():
            return
        try:
            r1 = self._run(["launchctl", "load", str(self._plist_path)])
            if r1.returncode != 0:
                raise RuntimeError(f"launchctl load failed (rc={r1.returncode}): {r1.stderr}")
            r2 = self._run(["launchctl", "start", _LABEL])
            if r2.returncode != 0:
                raise RuntimeError(f"launchctl start failed (rc={r2.returncode}): {r2.stderr}")
        except FileNotFoundError as exc:
            raise RuntimeError("launchctl binary not found") from exc

    def stop(self) -> None:
        if not self._is_loaded():
            return
        try:
            result = self._run(["launchctl", "unload", str(self._plist_path)])
            if result.returncode != 0:
                raise RuntimeError(f"launchctl unload failed (rc={result.returncode}): {result.stderr}")
        except FileNotFoundError as exc:
            raise RuntimeError("launchctl binary not found") from exc

    def status(self) -> ServiceStatus:
        try:
            result = self._run(["launchctl", "list", _LABEL])
        except FileNotFoundError:
            return ServiceStatus(running=False, pid=None, uptime_seconds=None)

        if result.returncode != 0:
            return ServiceStatus(running=False, pid=None, uptime_seconds=None)

        pid_match = re.search(r'"PID"\s*=\s*(\d+)', result.stdout)
        if not pid_match:
            return ServiceStatus(running=False, pid=None, uptime_seconds=None)

        try:
            pid = int(pid_match.group(1))
        except ValueError:
            return ServiceStatus(running=False, pid=None, uptime_seconds=None)

        if pid == 0:
            return ServiceStatus(running=False, pid=0, uptime_seconds=None)

        return ServiceStatus(running=True, pid=pid, uptime_seconds=None)
