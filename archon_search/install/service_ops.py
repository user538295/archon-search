"""Secrets-env creation and legacy service-file cleanup."""
from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path

import click

from archon_search._durable_io import ensure_private_dir


def _create_secrets_env(secrets_path: Path, *, dry_run: bool = False) -> bool:
    """Create *secrets_path* as an empty file with mode 0o600, if absent.

    Operators populate it with the provider key (``ANTHROPIC_API_KEY=<key>``
    or ``OPENAI_API_KEY=<key>``) so the managed service can load it at start
    time.  No-op when the file already exists
    (preserves any content the operator has added) or when *dry_run* is True.

    Returns True when the file was created, False otherwise.
    """
    if dry_run:
        return False
    if secrets_path.exists():
        return False
    ensure_private_dir(secrets_path.parent)
    secrets_path.touch(mode=0o600)
    secrets_path.chmod(0o600)
    return True


# ---------------------------------------------------------------------------
# Legacy service cleanup (Task 3.4)
# ---------------------------------------------------------------------------

def _legacy_service_path() -> Path:
    """Return the path to a legacy externally-managed search service file."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / "com.archon.search.plist"
    return Path.home() / ".config" / "systemd" / "user" / "archon-search.service"


def _remove_legacy_service(legacy_path: Path) -> None:
    """Unload and remove a legacy externally-managed service definition."""
    with contextlib.suppress(OSError):  # best-effort
        if sys.platform == "darwin":
            subprocess.run(["launchctl", "unload", str(legacy_path)], check=False, capture_output=True)
        elif sys.platform.startswith("linux"):
            service_name = legacy_path.stem
            subprocess.run(["systemctl", "--user", "stop", service_name], check=False, capture_output=True)
            subprocess.run(["systemctl", "--user", "disable", service_name], check=False, capture_output=True)
    try:
        legacy_path.unlink(missing_ok=True)
        click.echo(f"Removed legacy service file: {legacy_path}")
    except OSError as exc:
        click.echo(f"Warning: could not remove legacy service file: {exc}", err=True)
