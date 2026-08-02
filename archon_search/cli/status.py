"""archon-search status subcommand."""
from __future__ import annotations

import json
import os
from typing import Any

import click
import httpx

from archon_search.cli._helpers import _get_service
from archon_search.key_manager import load_key

_DEFAULT_API_URL = "http://localhost:8765"


def _fetch_server_status(api_url: str, api_key: str | None) -> dict[str, Any] | None:
    """Return GET /status JSON payload, or None if unreachable / non-200.

    Returns a dict with a special ``_auth_failed`` key set to True when the
    server responds with 401 (so callers can emit a clear auth-failure message
    rather than silently omitting the telemetry section).
    """
    try:
        key = api_key or load_key()
    except (ValueError, OSError):
        return None
    if key is None:
        return None
    headers = {"Authorization": f"Bearer {key}"}
    url = f"{api_url.rstrip('/')}/status"
    try:
        resp = httpx.get(url, headers=headers, timeout=2.0)
    except httpx.HTTPError:
        return None
    if resp.status_code == 401:
        return {"_auth_failed": True}
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


# ponytail: only key-gated providers; ollama and claude_cli are keyless
# (key_available always True server-side — query_expansion_protocol.py:26)
_PROVIDER_ENV_VAR: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _warn_if_expansion_key_absent(sub: Any, label: str) -> None:
    """Warn on stderr when a feature sub-object reports key_available=False."""
    if not isinstance(sub, dict):
        return
    if sub.get("key_available") is not False:
        return
    provider = sub.get("provider") or "anthropic"
    env_var = _PROVIDER_ENV_VAR.get(provider, f"the '{provider}' API key")
    click.echo(
        f"Warning: {label} enabled but {env_var} is not set — "
        "expansion will fall back to plain search.",
        err=True,
    )


def _print_expansion_key_warnings(server_payload: dict[str, Any]) -> None:
    """Emit a stderr warning when HyDE or RAG Fusion is enabled but the API key is absent.

    Only warns when the feature is configured (non-null sub-object) and
    ``key_available`` is explicitly ``False``.  ``None`` (feature disabled)
    and ``True`` (key present) are both silent.
    """
    _warn_if_expansion_key_absent(server_payload.get("hyde"), "HyDE")
    _warn_if_expansion_key_absent(server_payload.get("rag_fusion"), "RAG Fusion")


def _print_failed_expired_count(server_payload: dict[str, Any]) -> None:
    """Print a count and re-ingest hint when FAILED_EXPIRED ingest jobs exist."""
    count = server_payload.get("failed_expired_ingest_count", 0) or 0
    if count > 0:
        click.echo(
            f"\n{count} ingest job(s) expired without completing. "
            "Re-ingest the affected files to recover: archon-search ingest --path <path>"
        )


def _print_collections(server_payload: dict[str, Any]) -> None:
    """Render each collection's name, cached doc_count, and path from GET /status.

    Printed before the ``telemetry is None`` early-return so it still shows when
    telemetry is disabled (the default). An empty path (no matching config entry)
    is rendered as ``(no configured path)`` rather than a blank tail.
    """
    collections = server_payload.get("collections") or []
    if not collections:
        return
    click.echo("\nCollections:")
    for col in collections:
        name = col.get("name", "")
        doc_count = col.get("doc_count", 0)
        path = col.get("path") or "(no configured path)"
        click.echo(f"  {name}: {doc_count} document(s) — {path}")


def _print_telemetry_status(telemetry: dict[str, Any]) -> None:
    """Render the telemetry sub-object from GET /status."""
    enabled = telemetry.get("enabled", False)
    hash_doc_ids_enabled = telemetry.get("hash_doc_ids_enabled", False)
    click.echo(f"\nTelemetry: {'enabled' if enabled else 'disabled'}")
    click.echo(f"  hash_doc_ids_enabled: {hash_doc_ids_enabled}")


_ACTIVE_JOB_DISPLAY_CAP = 50


def _fetch_active_job_counts(api_url: str, api_key: str | None) -> tuple[int, int] | None:
    """Return (running_count, pending_count) from GET /jobs, or None on any error.

    Makes two GET /jobs calls (one per status) with limit=1 and reads the
    ``total`` field, which the server computes before pagination — so the count
    is accurate regardless of how many jobs exist.  A 401 on /jobs silently
    returns None; the caller treats this the same as "no active jobs."
    """
    try:
        key = api_key or load_key()
    except (ValueError, OSError):
        return None
    if key is None:
        return None
    headers = {"Authorization": f"Bearer {key}"}
    base = api_url.rstrip("/")
    try:
        r_resp = httpx.get(f"{base}/jobs", params=[("status", "RUNNING"), ("limit", 1)], headers=headers, timeout=2.0)
        p_resp = httpx.get(f"{base}/jobs", params=[("status", "PENDING"), ("limit", 1)], headers=headers, timeout=2.0)
    except httpx.HTTPError:
        return None
    if r_resp.status_code != 200 or p_resp.status_code != 200:
        return None
    try:
        # Coerce to int: a non-int total (e.g. "5") would crash at the > comparison below.
        running = int(r_resp.json().get("total", 0) or 0)
        pending = int(p_resp.json().get("total", 0) or 0)
    except (ValueError, AttributeError, TypeError):
        return None
    return running, pending


def _print_active_jobs(api_url: str, api_key: str | None) -> None:
    """Print job queue summary when any active jobs exist.

    Unlike its ``_print_*(server_payload)`` siblings, this function makes its
    own HTTP calls — necessary because ``GET /status`` readiness.jobs is not
    namespace-filtered.
    """
    counts = _fetch_active_job_counts(api_url, api_key)
    if counts is None:
        return
    running, pending = counts
    if running + pending == 0:
        return
    r_str = f"{_ACTIVE_JOB_DISPLAY_CAP}+" if running > _ACTIVE_JOB_DISPLAY_CAP else str(running)
    p_str = f"{_ACTIVE_JOB_DISPLAY_CAP}+" if pending > _ACTIVE_JOB_DISPLAY_CAP else str(pending)
    click.echo(f"\nJobs: {r_str} running, {p_str} queued — run `archon-search jobs list` for details")


def _print_graph_gc_status(server_payload: dict[str, Any]) -> None:
    """Render the graph GC status (stale_mention_count and last_graph_gc_at) from GET /status.

    BE-10 — display graph.stale_mention_count and maintenance.last_graph_gc_at.
    Only displays when graph is enabled and at least one field is non-null/non-zero.
    """
    graph = server_payload.get("graph")
    maintenance = server_payload.get("maintenance")

    # Return early if graph is not enabled
    if graph is None:
        return

    stale_mention_count = graph.get("stale_mention_count", 0) or 0
    last_graph_gc_at = maintenance.get("last_graph_gc_at") if maintenance else None

    # Only display if we have something to show (stale mentions or GC timestamp)
    if stale_mention_count > 0 or last_graph_gc_at is not None:
        click.echo("\nGraph:")
        if stale_mention_count > 0:
            click.echo(f"  stale_mention_count: {stale_mention_count}")
        if last_graph_gc_at is not None:
            click.echo(f"  last_graph_gc_at: {last_graph_gc_at}")


@click.command()
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of human-readable text.",
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
def status(as_json: bool, api_url: str, api_key: str | None) -> None:
    """Show archon-search service status."""
    try:
        svc_status = _get_service().status()
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if as_json:
        server_payload = _fetch_server_status(api_url, api_key)
        if server_payload is not None and server_payload.get("_auth_failed"):
            server_payload = {"auth_failed": True}
        payload = {
            "running": svc_status.running,
            "pid": svc_status.pid,
            "uptime_seconds": svc_status.uptime_seconds,
            "server": server_payload,
        }
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    container_mode = os.environ.get("ARCHON_SEARCH_CONTAINER") == "1"

    if svc_status.running:
        pid_part = f" (PID {svc_status.pid}" if svc_status.pid is not None else ""
        uptime_part = (
            f", uptime {svc_status.uptime_seconds:.0f}s)"
            if svc_status.uptime_seconds is not None
            else (")" if pid_part else "")
        )
        click.echo(f"running{pid_part}{uptime_part}")
    elif not container_mode:
        click.echo("stopped")

    server_payload = _fetch_server_status(api_url, api_key)
    if server_payload is None:
        # Server unreachable — omit the telemetry section entirely (S12b).
        return

    if server_payload.get("_auth_failed"):
        click.echo(
            "\nTelemetry: [401 Unauthorized — check your API key]"
        )
        return

    _print_expansion_key_warnings(server_payload)
    _print_failed_expired_count(server_payload)
    _print_graph_gc_status(server_payload)
    _print_active_jobs(api_url, api_key)
    _print_collections(server_payload)

    telemetry = server_payload.get("telemetry")
    if telemetry is None:
        # Telemetry disabled on server — omit section.
        return

    _print_telemetry_status(telemetry)
