"""``archon-search graph`` CLI group (E1b BE-4, converted to an HTTP proxy in GBC110 BE-8).

Provides:

* ``archon-search graph build-communities <collection>``
  Proxies to ``POST /graph/{collection}/rebuild-communities`` on the running
  server, which enqueues an async Leiden community-detection job and persists
  the result to the ``_archon_graph_{ns}__{col}_communities`` LanceDB table.
  Use ``--wait`` to poll the job to completion.
"""
from __future__ import annotations

import click
import httpx

from archon_search.cli._helpers import _poll_job, _SERVER_NOT_RUNNING_MSG
from archon_search.cli.collection import (
    _DEFAULT_API_URL,
    _resolve_api_key,
)


@click.group("graph")
def graph_cmd() -> None:
    """Graph management commands (E1b)."""


@graph_cmd.command("build-communities")
@click.argument("collection")
@click.option("--wait", "wait_flag", is_flag=True, default=False, help="Poll the job until it reaches a terminal status.")
@click.option(
    "--namespace",
    "-n",
    default="default",
    show_default=True,
    help="Namespace the collection belongs to.",
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
def build_communities_cmd(
    collection: str,
    wait_flag: bool,
    namespace: str,
    api_url: str,
    api_key: str | None,
) -> None:
    """Build Leiden communities for COLLECTION via the running server.

    POSTs to ``/graph/{collection}/rebuild-communities`` and prints the
    returned ``job_id``. With ``--wait``, polls ``GET /jobs/{id}`` until the
    job reaches a terminal status (``DONE``/``FAILED``/``FAILED_EXPIRED``/
    ``CANCELLED``), exiting 0 on ``DONE`` and non-zero otherwise.

    Exit codes:
    - 0 on success (job submitted, or --wait and job reached DONE)
    - non-zero on any error, including the server not running
    """
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")

    try:
        resp = httpx.post(
            f"{base_url}/graph/{collection}/rebuild-communities",
            headers=headers,
            params={"namespace": namespace},
        )
    except httpx.ConnectError:
        click.echo(_SERVER_NOT_RUNNING_MSG, err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code != 202:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    job_data = resp.json()
    job_id: str = job_data["job_id"]
    actual_ns: str = job_data.get("namespace") or namespace
    click.echo(f"Community rebuild job submitted: {job_id} (namespace: {actual_ns})")

    if wait_flag:
        _poll_rebuild_job(job_id, base_url, headers)


def _poll_rebuild_job(job_id: str, base_url: str, headers: dict) -> None:
    """Poll GET /jobs/{job_id} until terminal. Exits 0 on DONE, non-zero otherwise."""
    job = _poll_job(job_id, base_url, headers)
    if not job:
        # KeyboardInterrupt path — _poll_job already printed the message
        return
    result = job.get("result") or {}
    count = result.get("communities_built") if isinstance(result, dict) else None
    ns: str = job.get("namespace") or "default"
    if count is not None:
        click.echo(f"Community rebuild complete: {count} communities built (namespace: {ns}).")
    else:
        click.echo(f"Community rebuild complete (namespace: {ns}).")
