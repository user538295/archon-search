"""archon-search stop subcommand."""
from __future__ import annotations

import click

from archon_search.cli._helpers import _get_service


@click.command()
def stop() -> None:
    """Stop the archon-search service.

    Service identity (plist label / systemd unit name) is fixed — no --config needed.
    """
    try:
        service = _get_service()
        service.stop()
        click.echo("archon-search stopped")
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
