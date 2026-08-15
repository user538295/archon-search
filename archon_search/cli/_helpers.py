"""Shared CLI helpers for archon-search."""
from __future__ import annotations

import logging
import sys
import time

import click
import httpx

from archon_search.platform.service import SearchServiceLifecycle

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2
_TERMINAL_STATUSES = {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}
# ConnectTimeout (bound-not-listening) and ConnectError (refused) both mean the port is
# unreachable; _server_connect_fail_msg() decides whether that is "stopped" or "still starting".
_SERVER_NOT_RUNNING_MSG = "archon-search serve is not running. Start it first with: archon-search serve"
_SERVER_STARTING_MSG = (
    "archon-search is starting up. "
    "Please wait for it to finish loading models, then retry."
)
_CONNECT_FAIL = (httpx.ConnectError, httpx.ConnectTimeout)
_CONTAINER_MSG = (
    "Service management is not available in container mode. "
    "Use 'archon-search serve' to run the server."
)


def _server_connect_fail_msg(base_url: str | None = None) -> str:
    """Return the appropriate message when a connection attempt fails.

    When ``base_url`` is given, ``{base_url}/ready`` is probed first. The probe is
    authoritative only when it answers with a usable ``checks`` object — one that
    actually carries a ``models`` verdict. Then a 503 with ``storage == "ok"`` and
    either ``models == "pending"`` or ``sync == "pending"`` means "still starting
    up" (the latter covers the startup-sync window, where models is already
    "ok"), and works for every launch mode including a foreground ``archon-search
    serve`` that no service manager knows about; anything else means the server
    will not become ready on its own. Storage must be OK too: a server whose
    storage check failed also answers 503 with pending models, but telling the
    operator to wait for it would be wrong.

    ``usable`` keys off ``checks.get("models") is not None`` only — every real
    ``/ready`` body always includes a ``models`` key (``ReadinessChecks``
    defaults it), so a body carrying only ``sync`` never occurs in practice, and
    widening the gate would just accept more malformed bodies as "usable".

    Every other outcome — a connection failure (the port is not bound yet), a
    non-JSON body, a non-dict body (``null``, an array, a bare string), or a
    ``"checks": null`` payload — carries no usable signal, so it falls through to
    the *managed* service check (launchd / systemd / Windows). That is also the
    only path taken when ``base_url`` is absent. A failure there
    (``NotImplementedError`` on an unsupported platform, a missing ``launchctl``)
    falls back to 'not running': the message is a hint, never a hard diagnosis.
    """
    if base_url is not None:
        try:
            resp = httpx.get(f"{base_url}/ready", timeout=2)
            body = resp.json()
            checks = body.get("checks") if isinstance(body, dict) else None
            usable = isinstance(checks, dict) and checks.get("models") is not None
        except (httpx.HTTPError, ValueError):
            # httpx.HTTPError covers connection failures (ConnectError,
            # ConnectTimeout are subclasses); ValueError covers a non-JSON
            # response body from resp.json(). Both mean the probe itself is
            # unusable — fall through to the managed-service check. Anything
            # else is an internal defect and must propagate, not be silently
            # treated as "server down".
            logger.debug("_server_connect_fail_msg: /ready probe failed", exc_info=True)
            usable = False
        if usable:
            if (
                resp.status_code == 503
                and checks.get("storage") == "ok"
                and (checks.get("models") == "pending" or checks.get("sync") == "pending")
            ):
                return _SERVER_STARTING_MSG
            return _SERVER_NOT_RUNNING_MSG
    try:
        if _get_service().status().running:
            return _SERVER_STARTING_MSG
    except Exception:
        pass
    return _SERVER_NOT_RUNNING_MSG


def _poll_job(
    job_id: str,
    base_url: str,
    headers: dict,
    timeout_seconds: int | None = None,
) -> dict:
    """Poll GET /jobs/{job_id} until terminal, printing progress.

    Returns the final job dict on DONE. Raises SystemExit(1) on FAILED/CANCELLED/FAILED_EXPIRED.
    On KeyboardInterrupt prints 'Polling stopped — job continues on server' and returns {}.
    On timeout (when timeout_seconds is set) prints a hint and returns {}.
    """
    url = f"{base_url}/jobs/{job_id}"
    max_polls = (max(1, timeout_seconds // _POLL_INTERVAL_SECONDS)
                 if timeout_seconds is not None else None)
    status = "UNKNOWN"
    job: dict = {}
    polls = 0

    try:
        while True:
            polls += 1
            if max_polls is not None and polls > max_polls:
                click.echo(
                    f"Timed out after {timeout_seconds}s. Job {job_id} continues on server.",
                    err=True,
                )
                return {}

            try:
                resp = httpx.get(url, headers=headers)
            except _CONNECT_FAIL:
                click.echo(_server_connect_fail_msg(base_url), err=True)
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
            status = job.get("status")
            if status is None:
                click.echo("Error polling job: response missing 'status' field", err=True)
                raise SystemExit(1)
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
