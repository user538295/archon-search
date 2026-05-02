"""archon-search status subcommand."""
from __future__ import annotations

import click

from archon_search.cli._helpers import _get_service


@click.command()
def status() -> None:
    """Show archon-search service status."""
    try:
        svc_status = _get_service().status()
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if svc_status.running:
        pid_part = f" (PID {svc_status.pid}" if svc_status.pid is not None else ""
        uptime_part = f", uptime {svc_status.uptime_seconds:.0f}s)" if svc_status.uptime_seconds is not None else (")" if pid_part else "")
        click.echo(f"running{pid_part}{uptime_part}")
    else:
        click.echo("stopped")
