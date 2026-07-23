"""archon-search start subcommand."""
from __future__ import annotations

import os
from pathlib import Path

import click

from archon_search.cli._helpers import _get_service, _CONTAINER_MSG
from archon_search.config import ConfigError, load_config


@click.command()
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def start(config_path: Path | None) -> None:
    """Start the archon-search service."""
    if os.environ.get("ARCHON_SEARCH_CONTAINER") == "1":
        click.echo(_CONTAINER_MSG, err=True)
        raise SystemExit(1)
    try:
        # Validate config now; service reads it independently via ARCHON_SEARCH_CONFIG env var
        # baked into the plist/unit at register() time. Config injection into the service
        # constructor is deferred to Phase 5 when the HTTP server is wired up.
        load_config(config_path)
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    try:
        service = _get_service()
        service.start()
        click.echo("archon-search started")
    except RuntimeError as exc:
        if "systemctl binary not found" in str(exc):
            click.echo(_CONTAINER_MSG, err=True)
        else:
            click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
