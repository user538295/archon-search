"""archon-search stop subcommand."""
from __future__ import annotations

import os

import click

from archon_search.cli._helpers import _get_service, _CONTAINER_MSG


@click.command()
def stop() -> None:
    """Stop the archon-search service.

    Service identity (plist label / systemd unit name) is fixed — no --config needed.
    """
    if os.environ.get("ARCHON_SEARCH_CONTAINER") == "1":
        click.echo(_CONTAINER_MSG, err=True)
        raise SystemExit(1)
    try:
        service = _get_service()
        service.stop()
        click.echo("archon-search stopped")
    except RuntimeError as exc:
        if "systemctl binary not found" in str(exc):
            click.echo(_CONTAINER_MSG, err=True)
        else:
            click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
