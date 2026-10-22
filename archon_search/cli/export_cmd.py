"""archon-search export and import CLI commands (Tasks 8.1, 8.2)."""
from __future__ import annotations

import time

import click
import httpx

from archon_search.cli._helpers import (
    _CONNECT_FAIL,
    _EXIT_JOB_UNSUCCESSFUL,
    _TERMINAL_STATUSES,
    _jobs_status_hint,
    _server_connect_fail_msg,
)
from archon_search.key_manager import load_key
from archon_search.types import JobStatus

_DEFAULT_API_URL = "http://localhost:8765"
_POLL_INTERVAL_SECONDS = 2
_DEFAULT_WAIT_TIMEOUT_SECONDS = 300
_IMPORT_CANCELLED_MSG = (
    "Import CANCELLED: the collection may be partially or fully imported; "
    "re-run the same import command with --force-overwrite to restore a complete copy."
)


def _resolve_api_key(api_key: str | None) -> str:
    """Return the API key from the option, env var, or the key file."""
    if api_key:
        return api_key
    key = load_key()
    if key:
        return key
    click.echo(
        "No API key found. Pass --api-key, set ARCHON_SEARCH_API_KEY, or run the server "
        "once to auto-generate a key file.",
        err=True,
    )
    raise SystemExit(1)


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
    "--timeout",
    "timeout_seconds",
    default=_DEFAULT_WAIT_TIMEOUT_SECONDS,
    show_default=True,
    type=click.IntRange(min=1),
    help=(
        "Maximum seconds to wait for the export job to complete "
        "(only used with --wait). On timeout: exits 0 and prints a recovery hint."
    ),
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
    timeout_seconds: int,
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

    try:
        resp = httpx.post(url, json=body, headers=headers)
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(api_url.rstrip('/')), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

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

    _poll_job(job_id, api_url, headers, timeout_seconds)


def _wait_for_terminal_job(
    job_id: str,
    api_url: str,
    headers: dict,
    timeout_seconds: int,
    noun: str,
) -> dict | None:
    """Poll GET /jobs/{job_id} until terminal, printing progress; shared by export and import.

    Returns the terminal job dict, or None after Ctrl-C (the job continues on the server).

    Exit codes:
    - Exits 0 on timeout — prints a recovery hint naming the ``noun`` job.
    - Exits 1 on fatal errors (network, auth, HTTP errors).
    """
    url = f"{api_url.rstrip('/')}/jobs/{job_id}"
    max_polls = max(1, timeout_seconds // _POLL_INTERVAL_SECONDS)

    try:
        for _ in range(max_polls):
            try:
                resp = httpx.get(url, headers=headers)
            except _CONNECT_FAIL:
                click.echo(_server_connect_fail_msg(api_url.rstrip('/')), err=True)
                raise SystemExit(1)
            except httpx.HTTPError as exc:
                click.echo(f"Error polling job: {exc}", err=True)
                raise SystemExit(1) from exc
            if resp.status_code != 200:
                click.echo(
                    f"Error polling job: server returned {resp.status_code}: {resp.text}",
                    err=True,
                )
                raise SystemExit(1)

            job = resp.json()
            progress = job.get("progress")

            if progress:
                phase = progress.get("phase", "")
                processed = progress.get("processed", 0)
                total = progress.get("total", 0)
                click.echo(f"[{phase}] {processed}/{total}")

            if job["status"] in _TERMINAL_STATUSES:
                return job

            time.sleep(_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        click.echo("Polling stopped — job continues on server")
        return None

    click.echo(
        f"Timed out after {timeout_seconds}s waiting for {noun} job to complete. "
        f"{_jobs_status_hint(job_id)}.",
        err=True,
    )
    raise SystemExit(0)


def _poll_job(
    job_id: str,
    api_url: str,
    headers: dict,
    timeout_seconds: int = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> None:
    """Wait for an export job and report its outcome.

    Exit codes:
    - Exits 0 on success (DONE) or timeout — prints a recovery hint on timeout.
    - Exits 2 on FAILED, FAILED_EXPIRED or CANCELLED — no archive was produced.
    - Exits 1 on fatal errors (network, auth, HTTP errors).
    """
    job = _wait_for_terminal_job(job_id, api_url, headers, timeout_seconds, "export")
    if job is None:
        return

    status = job["status"]
    if status == JobStatus.DONE:
        result = job.get("result") or {}
        archive_path = result.get("archive_path", "")
        click.echo(f"Done. Archive: {archive_path}")
    elif status == JobStatus.CANCELLED:
        click.echo("Export CANCELLED: no archive was produced.", err=True)
        raise SystemExit(_EXIT_JOB_UNSUCCESSFUL)
    else:
        error = job.get("error") or "unknown error"
        click.echo(f"Export FAILED: {error}", err=True)
        raise SystemExit(_EXIT_JOB_UNSUCCESSFUL)


@click.command("import")
@click.argument("collection")
@click.argument("path")
@click.option(
    "--force-overwrite/--no-force-overwrite",
    default=False,
    help="Overwrite the collection if it already exists.",
)
@click.option(
    "--ignore-schema-version/--no-ignore-schema-version",
    default=False,
    help="Import even if the archive schema version does not match.",
)
@click.option(
    "--on-error",
    type=click.Choice(["fail", "skip"]),
    default="fail",
    show_default=True,
    help="How to handle corrupt lines in the archive.",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Poll until the job completes and print progress.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    default=_DEFAULT_WAIT_TIMEOUT_SECONDS,
    show_default=True,
    type=click.IntRange(min=1),
    help=(
        "Maximum seconds to wait for the import job to complete "
        "(only used with --wait). On timeout: exits 0 and prints a recovery hint."
    ),
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
def import_cmd(
    collection: str,
    path: str,
    force_overwrite: bool,
    ignore_schema_version: bool,
    on_error: str,
    wait: bool,
    timeout_seconds: int,
    api_url: str,
    api_key: str | None,
) -> None:
    """Import COLLECTION from a .tar.gz archive at PATH."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}

    body: dict = {
        "path": path,
        "force_overwrite": force_overwrite,
        "ignore_schema_version": ignore_schema_version,
        "on_error": on_error,
    }

    url = f"{api_url.rstrip('/')}/collections/{collection}/import"

    try:
        resp = httpx.post(url, json=body, headers=headers)
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(api_url.rstrip('/')), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code == 409:
        click.echo(
            f"Error: collection '{collection}' already exists. Use --force-overwrite to overwrite.",
            err=True,
        )
        raise SystemExit(1)

    if resp.status_code != 202:
        click.echo(
            f"Error: server returned {resp.status_code}: {resp.text}", err=True
        )
        raise SystemExit(1)

    job = resp.json()
    job_id: str = job["job_id"]
    click.echo(f"Import job submitted: {job_id}")

    if not wait:
        return

    _poll_import_job(job_id, api_url, headers, timeout_seconds)


def _poll_import_job(
    job_id: str,
    api_url: str,
    headers: dict,
    timeout_seconds: int = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> None:
    """Wait for an import job and report its outcome.

    Exit codes:
    - Exits 0 on success (DONE) or timeout — prints a recovery hint on timeout.
    - Exits 2 on FAILED, FAILED_EXPIRED or CANCELLED, as export and backup do.
    - Exits 1 on fatal errors (network, auth, HTTP errors).
    """
    job = _wait_for_terminal_job(job_id, api_url, headers, timeout_seconds, "import")
    if job is None:
        return

    status = job["status"]
    if status == JobStatus.DONE:
        result = job.get("result") or {}
        imported = result.get("imported", 0)
        skipped = result.get("skipped", 0)
        total_in_archive = result.get("total_in_archive", 0)
        click.echo(
            f"Done. imported={imported}, skipped={skipped}, total={total_in_archive}"
        )
        if skipped > 0:
            click.echo(
                f"Warning: {skipped} document(s) were skipped due to errors.",
                err=True,
            )
    elif status == JobStatus.CANCELLED:
        click.echo(_IMPORT_CANCELLED_MSG, err=True)
        raise SystemExit(_EXIT_JOB_UNSUCCESSFUL)
    else:
        error = job.get("error") or "unknown error"
        click.echo(f"Import FAILED: {error}", err=True)
        raise SystemExit(_EXIT_JOB_UNSUCCESSFUL)
