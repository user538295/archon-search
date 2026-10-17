"""Linux SystemdSearchService — manages archon_search.server as a systemd user service."""
from __future__ import annotations

import getpass
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from archon_search.paths import get_data_dir
from archon_search.platform.service import (
    _STOP_WAIT_TIMEOUT_S,
    SearchServiceLifecycle,
    ServiceStatus,
    get_secrets_env_file,
)

log = logging.getLogger(__name__)

_SERVICE_NAME = "archon-search"

_UNIT_TEMPLATE = """\
[Unit]
Description=Archon Search Server (archon-search)
After=network.target

[Service]
ExecStart="{python}" -m archon_search.server
WorkingDirectory={cwd}
Environment="ARCHON_SEARCH_CONFIG={config_path}"
Environment="ARCHON_SEARCH_DATA_DIR={data_dir}"
EnvironmentFile=-{secrets_file}
Restart=always
RestartSec=5
Nice=10
CPUQuota=50%

[Install]
WantedBy=default.target
"""

# systemd expands % specifiers in every directive above; %% is a literal %.
_SPECIFIER_ESCAPES = str.maketrans({"%": "%%"})
# Inside a double-quoted word systemd also C-unescapes, so \ and " take a backslash.
_QUOTED_WORD_ESCAPES = str.maketrans({"%": "%%", "\\": "\\\\", '"': '\\"'})
# systemd refuses quotes and backslashes in an ExecStart= executable path, and since v261
# also the glob characters *?[ (src/core/load-fragment.c config_parse_exec calls
# string_is_safe(path, 0), i.e. without STRING_ALLOW_GLOBS). $ is refused too: systemd
# $-expands argv only, so keeping it literal in both argv[0] and the executed path would
# need the @ prefix.
_UNSUPPORTED_EXECUTABLE_CHARS = frozenset("\"'\\$*?[")
# A control character (a newline above all) would end the directive and inject the rest
# of the value as further unit lines; systemd also rejects them in an executable path.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# systemd joins a line ending in an unpaired backslash with the next one (an even run is
# literal); a quoted word always ends in ", so only the unquoted directives can end in one.
_LINE_CONTINUATION = "\\"
# systemd strips these from both ends of a directive value (src/shared/conf-parser.c
# strstrip, WHITESPACE in src/basic/string-util.h); a quoted word keeps them inside its quotes.
_SYSTEMD_WHITESPACE = " \t\n\r"
# An unquoted path systemd ignores unless normalized, so it must not keep a .. component.
_PARENT_DIR = ".."
# The ExecStart= executable: the one field systemd also refuses quotes, $ and globs in.
_EXECUTABLE_PLACEHOLDER = "python"
# systemd reads unit files as UTF-8.
_UNIT_ENCODING = "utf-8"


@dataclass(frozen=True)
class _UnitField:
    """One path-valued unit field: what systemd reads back, and how the directive escapes it.

    ``quoted`` fields sit inside a double-quoted word; the others are written unquoted.
    Both get ``%`` doubled.
    """

    label: str
    value: str
    quoted: bool

    def rendered(self) -> str:
        """Escape ``value`` for its directive (systemd.syntax(7) "Quoting", systemd.unit(5) "Specifiers")."""
        return self.value.translate(_QUOTED_WORD_ESCAPES if self.quoted else _SPECIFIER_ESCAPES)


def _unquoted_path(path: Path) -> str:
    """Return ``path`` as the one normalized absolute path an unquoted directive needs.

    WorkingDirectory= and EnvironmentFile= are not unquoted (a space stays as-is) and
    are rejected when not normalized, so a path containing ``..`` is resolved — and
    resolve() then follows every symlink in it, not just the ones before the ``..``.
    Only such a path: resolving any other would freeze a symlinked data dir to its
    install-time target.
    """
    return str(path.resolve() if _PARENT_DIR in path.parts else path)


def _ends_in_line_continuation(value: str) -> bool:
    """Whether ``value`` ends in an odd run of backslashes, which systemd joins with the next line."""
    return (len(value) - len(value.rstrip(_LINE_CONTINUATION))) % 2 == 1


def _unit_fields(config_path: str) -> dict[str, _UnitField]:
    """Return the unit template's path-valued fields, keyed by template placeholder.

    WorkingDirectory= and EnvironmentFile= (the secrets file the wizard writes and the
    macOS wrapper reads) are unquoted paths; ExecStart= and Environment= values are
    double-quoted words. Environment=ARCHON_SEARCH_DATA_DIR keeps .. and symlinks as
    given, so the service follows a data-dir symlink that is later retargeted.
    """
    data_dir = get_data_dir()
    return {
        _EXECUTABLE_PLACEHOLDER: _UnitField("interpreter path", sys.executable, quoted=True),
        "cwd": _UnitField("working directory", _unquoted_path(data_dir), quoted=False),
        "config_path": _UnitField("config path", config_path, quoted=True),
        "data_dir": _UnitField("data directory", str(data_dir), quoted=True),
        "secrets_file": _UnitField("secrets file path", _unquoted_path(get_secrets_env_file()), quoted=False),
    }


def _validated_unit_fields(config_path: str) -> dict[str, _UnitField]:
    """Return ``_unit_fields(config_path)``, raising ``RuntimeError`` for one the unit cannot carry intact."""
    fields = _unit_fields(config_path)
    for field in fields.values():
        if _CONTROL_CHARS.search(field.value):
            raise RuntimeError(
                f"systemd unit cannot hold a {field.label} containing control characters: {field.value!r}"
            )
        try:
            field.value.encode(_UNIT_ENCODING)
        except UnicodeEncodeError:
            raise RuntimeError(
                f"systemd unit cannot hold a {field.label} that is not valid UTF-8: {field.value!r}"
            ) from None
        if field.quoted:
            continue
        if _ends_in_line_continuation(field.value):
            raise RuntimeError(
                f"systemd would join the next unit line onto a {field.label} ending in a backslash: {field.value!r}"
            )
        if field.value != field.value.strip(_SYSTEMD_WHITESPACE):
            raise RuntimeError(
                f"systemd would strip a {field.label} with leading or trailing whitespace: {field.value!r}"
            )
        # _unquoted_path() resolves any .., but a symlink loop is resolved only as far as
        # resolve() gets; refuse rather than rely on how far that is.
        if _PARENT_DIR in Path(field.value).parts:
            raise RuntimeError(
                f"systemd would ignore a {field.label} that is not normalized (contains ..): {field.value!r}"
            )
    python = fields[_EXECUTABLE_PLACEHOLDER].value
    if _UNSUPPORTED_EXECUTABLE_CHARS.intersection(python):
        raise RuntimeError(
            f"systemd cannot run an interpreter path containing quotes, backslashes, $ or *?[: {python!r}"
        )
    return fields


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

    def validate_registration(self, *, config_path: str) -> None:
        """Raise ``RuntimeError`` for a value the unit file cannot carry intact."""
        _validated_unit_fields(config_path)

    def register(self, dry_run: bool = False, *, config_path: str) -> None:
        # register() is public and callable without replace_registration(), so it validates
        # too (defence in depth) — from the same values it renders, so what is checked is written.
        fields = _validated_unit_fields(config_path)
        if dry_run:
            return
        content = _UNIT_TEMPLATE.format(**{name: field.rendered() for name, field in fields.items()})

        try:
            self._unit_path.parent.mkdir(parents=True, exist_ok=True)
            self._unit_path.write_text(content, encoding=_UNIT_ENCODING)  # noqa: durable-write
        except PermissionError as e:
            raise RuntimeError(f"Permission denied writing {self._unit_path}") from e
        except OSError as e:
            raise RuntimeError(f"Could not write {self._unit_path}") from e

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
