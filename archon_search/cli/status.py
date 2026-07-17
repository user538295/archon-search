"""archon-search status subcommand."""
from __future__ import annotations

import os
from typing import Any

import click
import httpx

from archon_search.cli._helpers import _get_service
from archon_search.key_manager import load_or_generate_key

_DEFAULT_API_URL = "http://localhost:8765"


def _resolve_api_key(api_key: str | None) -> str:
    """Return the API key from the option, env var, or the key file."""
    if api_key:
        return api_key
    env_key = os.environ.get("ARCHON_SEARCH_API_KEY")
    if env_key:
        return env_key
    key, _ = load_or_generate_key()
    return key


def _fetch_server_status(api_url: str, api_key: str | None) -> dict[str, Any] | None:
    """Return GET /status JSON payload, or None if unreachable / non-200.

    Returns a dict with a special ``_auth_failed`` key set to True when the
    server responds with 401 (so callers can emit a clear auth-failure message
    rather than silently omitting the telemetry section).
    """
    try:
        key = _resolve_api_key(api_key)
    except Exception:  # noqa: BLE001 — offline mode
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


def _print_expansion_key_warnings(server_payload: dict[str, Any]) -> None:
    """Emit a stderr warning when HyDE or RAG Fusion is enabled but the API key is absent.

    Only warns when the feature is configured (non-null sub-object) and
    ``key_available`` is explicitly ``False``.  ``None`` (feature disabled)
    and ``True`` (key present) are both silent.
    """
    hyde = server_payload.get("hyde")
    if hyde is not None and hyde.get("key_available") is False:
        click.echo(
            "Warning: HyDE enabled but ANTHROPIC_API_KEY is not set — "
            "expansion will fall back to plain search.",
            err=True,
        )
    rag_fusion = server_payload.get("rag_fusion")
    if rag_fusion is not None and rag_fusion.get("key_available") is False:
        click.echo(
            "Warning: RAG Fusion enabled but ANTHROPIC_API_KEY is not set — "
            "expansion will fall back to plain search.",
            err=True,
        )


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
def status(api_url: str, api_key: str | None) -> None:
    """Show archon-search service status."""
    try:
        svc_status = _get_service().status()
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if svc_status.running:
        pid_part = f" (PID {svc_status.pid}" if svc_status.pid is not None else ""
        uptime_part = (
            f", uptime {svc_status.uptime_seconds:.0f}s)"
            if svc_status.uptime_seconds is not None
            else (")" if pid_part else "")
        )
        click.echo(f"running{pid_part}{uptime_part}")
    else:
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
    _print_collections(server_payload)

    telemetry = server_payload.get("telemetry")
    if telemetry is None:
        # Telemetry disabled on server — omit section.
        return

    _print_telemetry_status(telemetry)
