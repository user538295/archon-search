"""Shared CLI helpers for archon-search."""
from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import click
import httpx

from archon_search.config import ConfigError
from archon_search.constants import EMBED_WAIT_SECONDS, INGEST_LOCK_TIMEOUT_S
from archon_search.key_manager import load_key
from archon_search.platform.service import SearchServiceLifecycle
from archon_search.types import JobStatus

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2
_TERMINAL_STATUSES = {
    JobStatus.DONE,
    JobStatus.FAILED,
    JobStatus.FAILED_EXPIRED,
    JobStatus.CANCELLED,
}
# Process exit code for `export --wait` / `import --wait` / `backup --now --wait` when a job ends in a
# terminal status other than DONE.
_EXIT_JOB_UNSUCCESSFUL = 2
# ConnectTimeout (bound-not-listening) and ConnectError (refused) both mean the port is
# unreachable; _server_connect_fail_msg() decides whether that is "stopped" or "still starting".
_SERVER_NOT_RUNNING_MSG = "archon-search serve is not running. Start it first with: archon-search serve"
_SERVER_STARTING_MSG = (
    "archon-search is starting up. "
    "Please wait for it to finish loading models, then retry."
)
_CONNECT_FAIL = (httpx.ConnectError, httpx.ConnectTimeout)
# Pydantic prefixes the message of a validator's ValueError with this; the CLI prints the message alone.
_PYDANTIC_VALUE_ERROR_PREFIX = "Value error, "
_CONTAINER_MSG = (
    "Service management is not available in container mode. "
    "Use 'archon-search serve' to run the server."
)
# The default every CLI ``--api-url`` option uses.
_DEFAULT_API_URL = "http://localhost:8765"
# Slack, beyond the server's collection-lock wait, for the server to act on the request once it holds the lock.
_LOCK_WAIT_RESPONSE_MARGIN_S = 10.0
# httpx timeout of a call to a route that may wait up to INGEST_LOCK_TIMEOUT_S for a collection lock before it
# answers (POST /collections/, DELETE /collections/{name}, POST /ingest, the reindex / reindex-metadata / migrate /
# rebuild-communities routes, PUT and DELETE /collections/{name}/description): httpx's 5 s default would report
# "Error contacting server" while the server goes on to act on the request.
_LOCK_WAITING_REQUEST_TIMEOUT_S = INGEST_LOCK_TIMEOUT_S + _LOCK_WAIT_RESPONSE_MARGIN_S
# httpx timeout of PUT /collections/{name}/description: the route embeds the new text (up to EMBED_WAIT_SECONDS, a cold
# embedder load included) BEFORE it takes the collection lock, so it can take that long on top of the lock wait.
_DESCRIPTION_SET_REQUEST_TIMEOUT_S = EMBED_WAIT_SECONDS + _LOCK_WAITING_REQUEST_TIMEOUT_S


@contextmanager
def _exit_on_config_error() -> Iterator[None]:
    """Turn a `ConfigError` raised in the block into ``Error: <msg>`` on stderr + exit 1."""
    try:
        yield
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


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


def _clean(value: str) -> str:
    """Replace non-printable characters with spaces so one value stays on one line.

    Echoed fields (a job ``collection``, a collection ``description``, ...) are free-form,
    server- or document-derived data. An embedded newline would otherwise render one row
    as several physical lines (breaking ``jobs list``'s ``--limit`` budget, or forging a
    ``key: value`` line), and terminal escapes such as ``\\x1b`` could drive the operator's
    terminal. Every character ``str.splitlines()`` breaks on is non-printable, so this
    covers them all.

    Substitution is 1:1, so the result is length-preserving and callers' column widths
    are unaffected.

    Applied to stdout; the ``err=True`` error paths are each followed by
    ``SystemExit(1)``, so no row can follow them, and an operator debugging a
    failure wants the server's bytes verbatim. The exceptions are the server's words on stderr:
    the error text of ``collection set-description`` / ``clear-description`` /
    ``describe`` and of ``search`` (a detail, an unexpected body, a 409 message, the notes after the
    results), and the job-polling errors of ``_poll_job_to_end``.
    """
    return "".join(ch if ch.isprintable() else " " for ch in value)


def _json_body(resp: httpx.Response) -> dict:
    """The JSON object of *resp*, or ``{}`` when the body is not JSON or not an object (a proxy's error page)."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _validation_item_text(item: dict) -> str:
    """One FastAPI ``{"loc", "msg"}`` item as text: the field name before a built-in message ("top_k: Input should be
    greater than or equal to 1"), the bare message for a value error (our validators already name the field) or an
    item with no field. The leading ``body`` and list indexes are dropped from ``loc``."""
    message = str(item.get("msg", ""))
    if not message:
        return ""
    if message.startswith(_PYDANTIC_VALUE_ERROR_PREFIX):
        return message.removeprefix(_PYDANTIC_VALUE_ERROR_PREFIX)
    loc = item.get("loc")
    parts = [str(part) for part in loc if isinstance(part, str)] if isinstance(loc, list) else []
    if parts and parts[0] == "body":
        parts = parts[1:]
    return f"{'.'.join(parts)}: {message}" if parts else message


def _server_message(detail: object, fallback: str, *, empty_text: str) -> str:
    """The words of a ``detail``: FastAPI's list of ``{"msg": ...}`` items (the validator prefix dropped), a coded
    ``{"code": ..., "message": ...}`` object (its message), a plain string, or *fallback* when the body had none.

    *empty_text* is the caller's own text when the detail and the body leave nothing printable, so each command says
    its own words.
    """
    if isinstance(detail, list):
        messages = [_validation_item_text(item) for item in detail if isinstance(item, dict)]
        detail = "; ".join(message for message in messages if message)
    elif isinstance(detail, dict):
        detail = detail.get("message")
    text = _clean(str(detail)) if detail else _clean(fallback)
    return text if text.strip() else empty_text


def _response_message(resp: httpx.Response, *, empty_text: str) -> str:
    """The cleaned words of an error response: its JSON ``detail`` (see ``_server_message``), else the body text."""
    return _server_message(_json_body(resp).get("detail"), resp.text, empty_text=empty_text)


def _send_or_exit(base_url: str, send: Callable[[], httpx.Response]) -> httpx.Response:
    """Return ``send()``'s response; a connect failure or another transport error exits 1 with its message."""
    try:
        return send()
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(base_url), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)


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

    When the probe *fails* (connection refused, timeout, non-JSON body):
    - Custom ``--api-url`` target (not ``_DEFAULT_API_URL``): return
      ``_SERVER_NOT_RUNNING_MSG`` immediately — never consult the local service
      manager; it describes a different server (S530).
    - Default local URL (``_DEFAULT_API_URL``): fall through to the managed-
      service check — the port may be temporarily closed while the process is
      alive and loading models (c7829cbd sequential-add window).

    When the probe *succeeds* but the body is not usable (``checks=null``,
    non-dict body, no ``models`` key): return ``_SERVER_NOT_RUNNING_MSG``
    immediately for any ``base_url`` — the target IS reachable but is not a
    healthy archon-search instance; the service manager is never consulted
    here (C1-I-16).

    The managed-service check (launchd / systemd / Windows) is only reached when
    ``base_url`` is absent, or for the default local URL after a probe failure.
    A failure there (``NotImplementedError`` on an unsupported platform, a
    missing ``launchctl``) falls back to 'not running': the message is a hint,
    never a hard diagnosis.
    """
    if base_url is not None:
        _is_custom = base_url.rstrip("/") != _DEFAULT_API_URL
        try:
            resp = httpx.get(f"{base_url}/ready", timeout=2)
            body = resp.json()
            raw_checks = body.get("checks") if isinstance(body, dict) else None
            checks: dict = raw_checks if isinstance(raw_checks, dict) else {}
            usable = checks.get("models") is not None
        except (httpx.HTTPError, ValueError):
            logger.debug("_server_connect_fail_msg: /ready probe failed", exc_info=True)
            if _is_custom:
                # Custom --api-url: never consult local service manager (S530).
                return _SERVER_NOT_RUNNING_MSG
            # Default local URL: probe failed but server process may be alive and
            # loading models. Fall through to managed-service check (c7829cbd).
        else:
            # Probe returned an HTTP response.
            if (
                usable
                and resp.status_code == 503
                and checks.get("storage") == "ok"
                and (checks.get("models") == "pending" or checks.get("sync") == "pending")
            ):
                return _SERVER_STARTING_MSG
            # Not usable or not in starting-up state. Never consult the service
            # manager: the probe succeeded, so the target IS reachable (C1-I-16).
            return _SERVER_NOT_RUNNING_MSG
    # Reached when base_url is None, or for the default local URL after probe failure.
    try:
        if _get_service().status().running:
            return _SERVER_STARTING_MSG
    except Exception:  # noqa: BLE001, S110 — best-effort status hint; any lifecycle-probe failure falls back to "not running"
        pass
    return _SERVER_NOT_RUNNING_MSG


def _jobs_status_hint(job_id: str) -> str:
    """Return the ``--wait`` timeout recovery hint pointing at ``archon-search jobs status``."""
    return f"Check status: 'archon-search jobs status {job_id}' with the same --api-url/--api-key"


def _poll_job_to_end(
    job_id: str,
    base_url: str,
    headers: dict,
    timeout_seconds: int | None = None,
) -> dict:
    """Poll GET /jobs/{job_id} until terminal, printing progress, and return the final job dict in any terminal status.

    Raises SystemExit(1) when the server cannot be reached or answers unexpectedly. On KeyboardInterrupt prints
    'Polling stopped — job continues on server' and returns {}. On timeout (when timeout_seconds is set) prints a hint
    and returns {}. The caller decides what a FAILED or CANCELLED job means.
    """
    url = f"{base_url}/jobs/{job_id}"
    max_polls = (max(1, timeout_seconds // _POLL_INTERVAL_SECONDS)
                 if timeout_seconds is not None else None)
    status: str | None = "UNKNOWN"
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
                    f"Error polling job: server returned {resp.status_code}: {_clean(resp.text)}",
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
                phase = _clean(str(progress.get("phase", "")))
                processed = progress.get("processed", 0)
                total = progress.get("total", 0)
                click.echo(f"{phase}: {processed}/{total}")

            if status in _TERMINAL_STATUSES:
                break

            time.sleep(_POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        click.echo("Polling stopped — job continues on server")
        return {}

    return job


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
    job = _poll_job_to_end(job_id, base_url, headers, timeout_seconds)
    if job and job.get("status") != JobStatus.DONE:
        error = job.get("error") or "unknown error"
        click.echo(f"Job {job.get('status')}: {error}", err=True)
        raise SystemExit(1)
    return job


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
