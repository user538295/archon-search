"""archon-search export CLI command (Task 8.1)."""
from __future__ import annotations

import os
import time

import click
import httpx

from archon_search.key_manager import load_or_generate_key

_DEFAULT_API_URL = "http://localhost:8765"
_POLL_INTERVAL_SECONDS = 2
_TERMINAL_STATUSES = {"DONE", "FAILED", "CANCELLED"}


def _resolve_api_key(api_key: str | None) -> str:
    """Return the API key from the option, env var, or the key file."""
    if api_key:
        return api_key
    env_key = os.environ.get("ARCHON_SEARCH_API_KEY")
    if env_key:
        return env_key
    key, _ = load_or_generate_key()
    return key


@click.command("export")
@click.argument("collection")
@click.option(
    "--output-dir",
    default="",
    metavar="PATH",
    help="Directory to write the archive to (default: server data dir/exports).",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Poll until the job completes and print progress.",
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
def export_cmd(
    collection: str,
    output_dir: str,
    wait: bool,
    api_url: str,
    api_key: str | None,
) -> None:
    """Export COLLECTION to a .tar.gz archive on the server."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}

    body: dict[str, str] = {}
    if output_dir:
        body["output_path"] = output_dir

    url = f"{api_url.rstrip('/')}/collections/{collection}/export"

    resp = httpx.post(url, json=body, headers=headers)

    if resp.status_code != 202:
        click.echo(
            f"Error: server returned {resp.status_code}: {resp.text}", err=True
        )
        raise SystemExit(1)

    job = resp.json()
    job_id: str = job["job_id"]
    click.echo(f"Export job submitted: {job_id}")

    if not wait:
        return

    _poll_job(job_id, api_url, headers)


def _poll_job(job_id: str, api_url: str, headers: dict) -> None:
    """Poll GET /jobs/{job_id} until terminal, printing progress."""
    url = f"{api_url.rstrip('/')}/jobs/{job_id}"

    try:
        while True:
            resp = httpx.get(url, headers=headers)
            if resp.status_code != 200:
                click.echo(
                    f"Error polling job: server returned {resp.status_code}: {resp.text}",
                    err=True,
                )
                raise SystemExit(1)

            job = resp.json()
            status: str = job["status"]
            progress = job.get("progress")

            if progress:
                phase = progress.get("phase", "")
                processed = progress.get("processed", 0)
                total = progress.get("total", 0)
                click.echo(f"[{phase}] {processed}/{total}")

            if status in _TERMINAL_STATUSES:
                break

            time.sleep(_POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        click.echo("Polling stopped — job continues on server")
        return

    if status == "DONE":
        result = job.get("result") or {}
        archive_path = result.get("archive_path", "")
        click.echo(f"Done. Archive: {archive_path}")
    elif status == "FAILED":
        error = job.get("error") or "unknown error"
        click.echo(f"Export FAILED: {error}", err=True)
        raise SystemExit(1)
    else:
        click.echo(f"Job ended with status: {status}")
