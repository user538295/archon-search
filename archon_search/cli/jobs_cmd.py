"""``archon-search jobs`` CLI group — FE-3 (CSP120 S24).

Provides:

* ``archon-search jobs status <job_id>``
  One-shot status check: calls ``GET /jobs/{job_id}`` once and prints the
  current job state.  To track progress to completion, use ``--wait`` on the
  command that submitted the job (e.g. ``archon-search graph build-communities --wait``).
"""
from __future__ import annotations

import click
import httpx

from archon_search.cli.collection import _DEFAULT_API_URL, _resolve_api_key

_EXIT_1_STATUSES = {"FAILED", "FAILED_EXPIRED", "CANCELLED"}


@click.group("jobs")
def jobs() -> None:
    """Job management commands."""


@jobs.command("status")
@click.argument("job_id")
@click.option(
    "--api-url",
    default=_DEFAULT_API_URL,
    show_default=True,
    help="Base URL of the archon-search server.",
)
@click.option(
    "--api-key",
    default=None,
    help="API key (falls back to ARCHON_SEARCH_API_KEY env var or the key file).",
)
def status_cmd(job_id: str, api_url: str, api_key: str | None) -> None:
    """Print the current status of JOB_ID.

    Exit codes:
    - 0 for DONE and all in-progress states (PENDING/QUEUED/RUNNING/CANCELLING)
    - 1 for FAILED, FAILED_EXPIRED, CANCELLED, or 404 (job not found)
    """
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")

    try:
        resp = httpx.get(f"{base_url}/jobs/{job_id}", headers=headers)
    except httpx.ConnectError as exc:
        click.echo(
            "archon-search serve is not running. Start it first.",
            err=True,
        )
        raise SystemExit(1) from exc
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code == 404:
        click.echo(f"Job not found: {job_id}", err=True)
        raise SystemExit(1)

    if resp.status_code != 200:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    job = resp.json()
    current_status = job.get("status", "UNKNOWN")
    collection = job.get("collection", "")
    created_at = job.get("created_at", "")
    progress = job.get("progress")
    error = job.get("error")

    click.echo(f"job_id:     {job_id}")
    click.echo(f"status:     {current_status}")
    click.echo(f"collection: {collection}")
    click.echo(f"created_at: {created_at}")

    if progress is not None:
        click.echo(f"progress:   {progress}")

    if current_status in _EXIT_1_STATUSES:
        if error:
            click.echo(f"error:      {error}")
        raise SystemExit(1)
