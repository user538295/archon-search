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
        # stop() returns 0 only once the supervisor confirms the service is down
        # (S04); a non-zero result means the wait timed out and it may still be
        # running. Exit 0 either way (stop was issued), but don't claim a clean stop.
        if service.stop() == 0:
            click.echo("archon-search stopped")
        else:
            click.echo(
                "Warning: archon-search did not confirm stopped within the timeout; "
                "it may still be running. Check `archon-search status`.",
                err=True,
            )
    except RuntimeError as exc:
        if "systemctl binary not found" in str(exc):
            click.echo(_CONTAINER_MSG, err=True)
        else:
            click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
