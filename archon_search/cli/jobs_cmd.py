"""``archon-search jobs`` CLI group.

Provides:

* ``archon-search jobs list``
  Tabular job listing with optional ``--status`` and ``--limit`` filters.

* ``archon-search jobs show <job_id>``
  Full-detail view of one job; ``--wait`` polls until completion.

* ``archon-search jobs status <job_id>``
  One-shot status check (legacy command, kept for backward compatibility).
"""
from __future__ import annotations

import click
import httpx

from archon_search.cli._helpers import _CONNECT_FAIL, _poll_job, _SERVER_NOT_RUNNING_MSG
from archon_search.cli.collection import _DEFAULT_API_URL, _resolve_api_key

_EXIT_1_STATUSES = {"FAILED", "FAILED_EXPIRED", "CANCELLED"}
_DEFAULT_WAIT_TIMEOUT = 600  # 10 minutes


def _fmt_elapsed(created_at: str, updated_at: str, status: str) -> str:
    """Return a human-readable elapsed duration string."""
    from datetime import datetime, timezone  # noqa: PLC0415

    try:
        start = datetime.fromisoformat(created_at)
        if status in _EXIT_1_STATUSES or status == "DONE":
            end = datetime.fromisoformat(updated_at)
        else:
            end = datetime.now(timezone.utc)
        secs = max(0, int((end - start).total_seconds()))
    except (ValueError, TypeError):
        return "-"
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60}s"


def _print_job_detail(job: dict) -> None:
    """Print full detail of a job dict."""
    fields = [
        ("job_id", job.get("job_id", "")),
        ("job_type", job.get("job_type", "")),
        ("status", job.get("status", "")),
        ("collection", job.get("collection", "")),
        ("source", job.get("source", "")),
        ("source_path", job.get("source_path", "")),
        ("created_at", job.get("created_at", "")),
        ("updated_at", job.get("updated_at", "")),
    ]
    result = job.get("result")
    if result is not None:
        fields.append(("result", result))
    progress = job.get("progress")
    if progress is not None:
        fields.append(("progress", progress))
    error = job.get("error")
    if error:
        fields.append(("error", error))

    width = max(len(k) for k, _ in fields)
    for k, v in fields:
        click.echo(f"{k:<{width}}  {v}")


@click.group("jobs")
def jobs() -> None:
    """Job management commands."""


@jobs.command("list")
@click.option(
    "--status",
    multiple=True,
    metavar="STATUS",
    help="Filter by status (repeatable: --status running --status queued).",
)
@click.option("--limit", default=50, show_default=True, type=click.IntRange(min=1, max=200), help="Maximum number of jobs to return (max: 200).")
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
def list_cmd(status: tuple[str, ...], limit: int, api_url: str, api_key: str | None) -> None:
    """List recent jobs, newest first."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")

    query_params: list[tuple[str, str | int]] = [("limit", limit)]
    for s in status:
        query_params.append(("status", s))

    try:
        resp = httpx.get(f"{base_url}/jobs", params=query_params, headers=headers)
    except _CONNECT_FAIL:
        click.echo(_SERVER_NOT_RUNNING_MSG, err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code != 200:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    data = resp.json()
    items = data.get("items", [])
    total = data.get("total", len(items))

    if not items:
        click.echo("No jobs found.")
        return

    # Header
    click.echo(f"{'ID':<8}  {'TYPE':<18}  {'COLLECTION':<20}  {'STATUS':<14}  {'STARTED':<20}  ELAPSED")
    click.echo("-" * 94)
    for j in items:
        jid = (j.get("job_id") or "")[:8]
        jtype = (j.get("job_type") or "")[:18]
        col = (j.get("collection") or "")[:20]
        st = (j.get("status") or "")[:14]
        started = (j.get("created_at") or "")[:19].replace("T", " ")
        elapsed = _fmt_elapsed(
            j.get("created_at", ""),
            j.get("updated_at", ""),
            j.get("status", ""),
        )
        click.echo(f"{jid:<8}  {jtype:<18}  {col:<20}  {st:<14}  {started:<20}  {elapsed}")

    if total > len(items):
        click.echo(f"Showing {len(items)} of {total} jobs — use --limit to see more (max: 200).")


@jobs.command("show")
@click.argument("job_id")
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Poll until the job reaches a terminal status.",
)
@click.option(
    "--timeout",
    default=_DEFAULT_WAIT_TIMEOUT,
    show_default=True,
    type=click.IntRange(min=1),
    metavar="SECONDS",
    help="Maximum seconds to wait (only with --wait).",
)
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
def show_cmd(
    job_id: str,
    wait: bool,
    timeout: int,
    api_url: str,
    api_key: str | None,
) -> None:
    """Show full detail for JOB_ID. Use --wait to poll until completion."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")

    if wait:
        job = _poll_job(job_id, base_url, headers, timeout_seconds=timeout)
        if not job:
            # KeyboardInterrupt or timeout — _poll_job already printed a message.
            return
        _print_job_detail(job)
        return

    try:
        resp = httpx.get(f"{base_url}/jobs/{job_id}", headers=headers)
    except _CONNECT_FAIL:
        click.echo(_SERVER_NOT_RUNNING_MSG, err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code == 404:
        click.echo(f"Job {job_id} not found.", err=True)
        raise SystemExit(1)

    if resp.status_code != 200:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    job = resp.json()
    _print_job_detail(job)

    if job.get("status") in _EXIT_1_STATUSES:
        raise SystemExit(1)


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
    """Print the current status of JOB_ID (one-shot, no polling).

    Exit codes:
    - 0 for DONE and all in-progress states (PENDING/QUEUED/RUNNING/CANCELLING)
    - 1 for FAILED, FAILED_EXPIRED, CANCELLED, or 404 (job not found)
    """
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")

    try:
        resp = httpx.get(f"{base_url}/jobs/{job_id}", headers=headers)
    except _CONNECT_FAIL:
        click.echo(_SERVER_NOT_RUNNING_MSG, err=True)
        raise SystemExit(1)
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
