"""archon-search install and uninstall subcommands."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from shutil import rmtree

import click

from archon_search.cli._helpers import _get_service
from archon_search.config import get_default_config_path, load_config

_HEALTH_TIMEOUT = 60


def _legacy_service_path() -> Path:
    """Return the path to the legacy Archon-managed search service file."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / "com.archon.search.plist"
    return Path.home() / ".config" / "systemd" / "user" / "archon-search.service"


def _remove_legacy_service(legacy_path: Path) -> None:
    """Unload and remove a legacy Archon-managed service definition."""
    import subprocess
    try:
        if sys.platform == "darwin":
            subprocess.run(["launchctl", "unload", str(legacy_path)], check=False, capture_output=True)
        elif sys.platform.startswith("linux"):
            service_name = legacy_path.stem
            subprocess.run(["systemctl", "--user", "stop", service_name], check=False, capture_output=True)
            subprocess.run(["systemctl", "--user", "disable", service_name], check=False, capture_output=True)
    except Exception:
        pass  # best-effort
    try:
        legacy_path.unlink(missing_ok=True)
        click.echo(f"Removed legacy service file: {legacy_path}")
    except Exception as exc:
        click.echo(f"Warning: could not remove legacy service file: {exc}", err=True)


def _get_db_path(config_path: Path | None = None) -> Path:
    """Return the expanded database path from config."""
    cfg = load_config(config_path)
    return Path(cfg.db_path).expanduser()


def _wait_for_health(host: str, port: int, timeout: int = _HEALTH_TIMEOUT) -> bool:
    """Poll HTTP health endpoint until ready or timeout. Returns True if up."""
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            url = f"http://{host}:{port}/health"
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(1)
    return False


@click.command()
@click.option("--dry-run", is_flag=True, default=False, help="Print actions without executing")
@click.option("--non-interactive", is_flag=True, default=False, help="Skip interactive prompts")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def install(dry_run: bool, non_interactive: bool, config_path: Path | None) -> None:
    """Install archon-search service."""
    config_file = config_path or get_default_config_path()

    if dry_run:
        click.echo("[dry-run] Would create config if absent")
        click.echo("[dry-run] Would ensure data/log directories exist")
        click.echo("[dry-run] Would register and start service")
        click.echo("[dry-run] Would wait for health check")
        return

    if not non_interactive:
        answer = click.prompt("Proceed with installation? [y/N]", default="N")
        if answer.strip().lower() != "y":
            click.echo("Installation aborted.")
            raise SystemExit(1)

    # Create default config if absent
    if not config_file.exists():
        click.echo(f"Creating default config at {config_file}")
        config_file.parent.mkdir(parents=True, exist_ok=True)
        from archon_search.cli.config_cmd import _default_toml
        config_file.write_text(_default_toml(), encoding="utf-8")

    # Load config to get host/port for health check
    cfg = load_config(config_file)

    # Ensure data/log directories exist
    db_path = Path(cfg.db_path).expanduser()
    db_path.mkdir(parents=True, exist_ok=True)
    log_path = Path(cfg.log_file).expanduser().parent
    log_path.mkdir(parents=True, exist_ok=True)

    # Detect and handle legacy service
    legacy = _legacy_service_path()
    if legacy.exists():
        click.echo(f"Legacy service definition found at {legacy} — migrating ...")
        _remove_legacy_service(legacy)

    service = _get_service()
    click.echo("Registering service ...")
    service.register()

    click.echo("Starting service ...")
    service.start()

    click.echo("Waiting for service to become ready ...")
    ready = _wait_for_health(cfg.host, cfg.port)
    if not ready:
        click.echo(f"Warning: service did not become ready within {_HEALTH_TIMEOUT}s", err=True)
        raise SystemExit(1)

    click.echo("archon-search installed and running.")


@click.command()
@click.option("--delete-db", is_flag=True, default=False, help="Also delete the search database directory")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def uninstall(delete_db: bool, config_path: Path | None) -> None:
    """Uninstall archon-search service."""
    try:
        service = _get_service()
        service.stop()
        service.unregister()
    except Exception as exc:
        click.echo(f"Error during service teardown: {exc}", err=True)
        raise SystemExit(1)

    if delete_db:
        db_path = _get_db_path(config_path)
        if db_path.exists():
            rmtree(db_path)
            click.echo(f"Deleted search database at {db_path}.")

    click.echo("archon-search uninstalled.")
