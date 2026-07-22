"""archon-search sync subcommand."""
from __future__ import annotations

import click
import httpx

from archon_search.cli.collection import _DEFAULT_API_URL, _resolve_api_key
from archon_search.cli._helpers import _CONNECT_FAIL, _poll_job, _SERVER_NOT_RUNNING_MSG


@click.command()
@click.option("--wait", "wait_flag", is_flag=True, default=False, help="Poll until the sync job completes")
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
def sync(wait_flag: bool, api_url: str, api_key: str | None) -> None:
    """Sync configured collections via the server."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")
    post_url = f"{base_url}/sync"

    try:
        resp = httpx.post(post_url, headers=headers)
    except _CONNECT_FAIL:
        click.echo(_SERVER_NOT_RUNNING_MSG, err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 409:
        try:
            detail = resp.json().get("detail", "sync already in progress")
        except Exception:
            detail = "sync already in progress"
        click.echo(f"Error: {detail}", err=True)
        raise SystemExit(1)

    if resp.status_code != 202:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    job_data = resp.json()
    job_id: str = job_data["job_id"]
    click.echo(f"Sync job submitted: {job_id}. Track progress with: archon-search jobs status {job_id}")

    if wait_flag:
        job = _poll_job(job_id, base_url, headers)
        if job:
            click.echo("Sync complete.")
