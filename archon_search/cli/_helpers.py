"""Shared CLI helpers for archon-search."""
from __future__ import annotations

import sys
import time

import click
import httpx

from archon_search.platform.service import SearchServiceLifecycle

_POLL_INTERVAL_SECONDS = 2
_TERMINAL_STATUSES = {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}


def _poll_job(job_id: str, base_url: str, headers: dict) -> dict:
    """Poll GET /jobs/{job_id} until terminal, printing progress.

    Returns the final job dict on DONE. Raises SystemExit(1) on FAILED/CANCELLED/FAILED_EXPIRED.
    On KeyboardInterrupt prints 'Polling stopped — job continues on server' and returns {}.
    """
    url = f"{base_url}/jobs/{job_id}"
    status = "UNKNOWN"
    job: dict = {}

    try:
        while True:
            try:
                resp = httpx.get(url, headers=headers)
            except httpx.ConnectError as exc:
                click.echo(f"Error polling job: {exc}", err=True)
                raise SystemExit(1) from exc
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
            status = job["status"]
            progress = job.get("progress")

            if progress:
                phase = progress.get("phase", "")
                processed = progress.get("processed", 0)
                total = progress.get("total", 0)
                click.echo(f"{phase}: {processed}/{total}")

            if status in _TERMINAL_STATUSES:
                break

            time.sleep(_POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        click.echo("Polling stopped — job continues on server")
        return {}

    if status == "DONE":
        return job

    error = job.get("error") or "unknown error"
    click.echo(f"Job {status}: {error}", err=True)
    raise SystemExit(1)


def _get_service() -> SearchServiceLifecycle:
    """Return the platform-appropriate service implementation."""
    if sys.platform == "darwin":
        from archon_search.platform.macos import LaunchdSearchService
        return LaunchdSearchService()
    if sys.platform.startswith("linux"):
        from archon_search.platform.linux import SystemdSearchService
        return SystemdSearchService()
    if sys.platform == "win32":
        from archon_search.platform.windows import WindowsSearchService
        return WindowsSearchService()
    raise NotImplementedError(f"Unsupported platform: {sys.platform}")
