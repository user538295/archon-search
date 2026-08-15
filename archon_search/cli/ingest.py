"""archon-search ingest subcommand."""
from __future__ import annotations

from pathlib import Path

import click
import httpx

from archon_search.cli._helpers import _CONNECT_FAIL, _poll_job, _server_connect_fail_msg
from archon_search.cli.collection import _DEFAULT_API_URL, _resolve_api_key
from archon_search.sync import path_to_collection_name


@click.command()
@click.option("--path", "ingest_path", default=None, type=click.Path(path_type=Path), help="File or directory to ingest")
@click.option("--collection", default=None, help="Collection name (defaults to path basename or file stem)")
@click.option("--wait", "wait_flag", is_flag=True, default=False, help="Poll until the ingest job completes")
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
def ingest(ingest_path: Path | None, collection: str | None, wait_flag: bool, api_url: str, api_key: str | None) -> None:
    """Ingest documents from a file or directory into a collection."""
    if ingest_path is None:
        click.echo("Error: --path is required.", err=True)
        raise SystemExit(1)

    collection_name = collection or path_to_collection_name(str(ingest_path))

    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}", "X-Ingested-By": "cli"}
    base_url = api_url.rstrip("/")
    post_url = f"{base_url}/ingest"

    body: dict = {"collection": collection_name, "path": str(Path(ingest_path).expanduser().resolve())}

    try:
        resp = httpx.post(post_url, json=body, headers=headers)
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(base_url), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code != 202:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    job_data = resp.json()
    job_id: str = job_data["job_id"]
    click.echo(f"Ingest job submitted: {job_id}. Collection: '{collection_name}'")
    click.echo(f"Track progress with: archon-search jobs status {job_id}")

    if wait_flag:
        job = _poll_job(job_id, base_url, headers)
        if job:
            click.echo(f"Ingest complete for '{collection_name}'.")
