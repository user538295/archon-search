"""``archon-search maintenance`` CLI group (D5 FE-1).

Provides two operator-facing subcommands:

* ``archon-search maintenance status [--json]`` — print current maintenance
  state.  Reads the on-disk ``.maintenance-state.json`` (offline-capable);
  when the server is reachable, merges live data from ``GET /status``.
* ``archon-search maintenance run [--wait]`` — ``POST /maintenance/trigger``
  to kick off an immediate pass.  With ``--wait``, polls ``GET /status``
  until ``maintenance.last_run_at`` changes (or a timeout is reached).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import click
import httpx

from archon_search.config import ConfigError, load_config
from archon_search.key_manager import load_key
from archon_search.paths import get_data_dir
from archon_search.cli._helpers import _CONNECT_FAIL, _SERVER_NOT_RUNNING_MSG, _server_connect_fail_msg

_DEFAULT_API_URL = "http://localhost:8765"
_POLL_INTERVAL_SECONDS = 2
_DEFAULT_WAIT_TIMEOUT_SECONDS = 120
_STATE_FILE_NAME = ".maintenance-state.json"


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


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group("maintenance")
def maintenance_cmd() -> None:
    """Manage scheduled maintenance passes."""


# ---------------------------------------------------------------------------
# status subcommand
# ---------------------------------------------------------------------------


@maintenance_cmd.command("status")
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
def status_subcommand(as_json: bool, api_url: str, api_key: str | None) -> None:
    """Print current maintenance state (offline-capable)."""
    data = _gather_status(api_url, api_key)
    if as_json:
        click.echo(json.dumps(data["payload"], indent=2, sort_keys=True))
        return
    _print_status_text(data)


def _gather_status(api_url: str, api_key: str | None) -> dict[str, Any]:
    """Read offline state first, then try the server for live data."""
    data_dir = get_data_dir()
    enabled, interval_hours = _read_config_for_status()

    state = _read_state_file(data_dir)
    last_run_at: str | None = state.get("last_run_at")
    next_run_at: str | None = state.get("next_run_at")
    collection_health: dict[str, Any] = state.get("collection_health", {})
    # model_validation is server-global (D6) — only available from GET /status,
    # never persisted to the on-disk maintenance state file.
    model_validation: dict[str, Any] | None = None
    # E2a BE-8 — expired chunk count and last prune timestamp from server
    expired_chunk_count: int = 0
    last_expired_pruned_at: str | None = None

    server_reachable = False
    server_payload = _fetch_server_status(api_url, api_key)
    if server_payload is not None:
        server_reachable = True
        mv = server_payload.get("model_validation")
        if isinstance(mv, dict):
            model_validation = mv
        maintenance_obj = server_payload.get("maintenance") or {}
        if "enabled" in maintenance_obj:
            enabled = bool(maintenance_obj["enabled"])
        if "interval_hours" in maintenance_obj:
            interval_hours = int(maintenance_obj["interval_hours"])
        if maintenance_obj.get("last_run_at") is not None:
            last_run_at = maintenance_obj["last_run_at"]
        if maintenance_obj.get("next_run_at") is not None:
            next_run_at = maintenance_obj["next_run_at"]
        if "expired_chunk_count" in maintenance_obj:
            expired_chunk_count = int(maintenance_obj["expired_chunk_count"])
        last_expired_pruned_at = maintenance_obj.get("last_expired_pruned_at")
        # Server collection_health is authoritative — it's namespace-scoped.
        server_health = maintenance_obj.get("collection_health")
        if server_health:
            # Normalize list-of-dicts from server into {ns/col: entry} dict.
            collection_health = {
                entry.get("collection", ""): entry for entry in server_health
            }

    payload: dict[str, Any] = {
        "enabled": enabled,
        "interval_hours": interval_hours,
        "last_run_at": last_run_at,
        "next_run_at": next_run_at,
        "collection_health": collection_health,
        "expired_chunk_count": expired_chunk_count,
        "last_expired_pruned_at": last_expired_pruned_at,
    }
    if model_validation is not None:
        payload["model_validation"] = model_validation
    return {
        "payload": payload,
        "server_reachable": server_reachable,
    }


def _read_config_for_status() -> tuple[bool, int]:
    """Load config offline; tolerate missing/broken config files."""
    try:
        config = load_config()
    except (ConfigError, OSError, ValueError):
        return False, 0
    m = config.maintenance
    enabled = m.interval_hours > 0
    return enabled, m.interval_hours


def _read_state_file(data_dir: Path) -> dict[str, Any]:
    state_file = data_dir / _STATE_FILE_NAME
    if not state_file.exists():
        return {}
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _fetch_server_status(
    api_url: str, api_key: str | None
) -> dict[str, Any] | None:
    """Return ``/status`` JSON or None if the server is unreachable."""
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
        # ponytail: broad catch is intentional — status is best-effort/offline-capable;
        # unlike `run`, any transport error (including ReadTimeout) means "unavailable".
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _print_status_text(data: dict[str, Any]) -> None:
    payload = data["payload"]
    server_reachable: bool = data["server_reachable"]

    if payload["enabled"]:
        click.echo(
            f"Maintenance: enabled (interval={payload['interval_hours']}h)"
        )
    else:
        click.echo("Maintenance: disabled")

    if server_reachable:
        click.echo(f"Last run:  {payload['last_run_at'] or 'never'}")
        click.echo(f"Next run:  {payload['next_run_at'] or 'unknown'}")
        # E2a BE-8 — expired chunk count and last prune timestamp
        expired_count = payload.get("expired_chunk_count", 0)
        last_pruned = payload.get("last_expired_pruned_at") or "never"
        click.echo(f"Expired chunks (live): {expired_count}")
        click.echo(f"Last expired pruned:   {last_pruned}")
    else:
        click.echo("Last run:  [server unavailable]")
        click.echo("Next run:  [server unavailable]")

    model_validation = payload.get("model_validation")
    if isinstance(model_validation, dict):
        _print_model_validation(model_validation)

    collection_health: dict[str, Any] = payload.get("collection_health", {})
    if not collection_health:
        click.echo("No maintenance history.")
        return

    click.echo("\nCollection health:")
    for col_key, entry in sorted(collection_health.items()):
        fts_at = entry.get("fts_optimized_at") or "never"
        orphans = entry.get("orphans_removed_last_run", 0)
        last_error = entry.get("last_error")
        chunks = entry.get("meta_chunk_count", 0)
        click.echo(
            f"  {col_key}: chunks={chunks}, fts_optimized={fts_at},"
            f" orphans_removed={orphans}"
        )
        if last_error:
            click.echo(f"    last_error: {last_error}")


def _print_model_validation(mv: dict[str, Any]) -> None:
    """Render the server-global model validation block (D6 S13)."""
    click.echo("\nModel validation:")
    embedder_ok = mv.get("embedder_ok")
    reranker_ok = mv.get("reranker_ok")
    validated_at = mv.get("validated_at") or "pending"
    click.echo(f"  embedder_ok: {_fmt_ok(embedder_ok)}")
    click.echo(f"  reranker_ok: {_fmt_ok(reranker_ok)}")
    click.echo(f"  validated_at: {validated_at}")
    warnings = mv.get("provider_warnings") or []
    for warning in warnings:
        click.echo(f"    warning: {warning}")


def _fmt_ok(value: Any) -> str:
    """Format a nullable bool probe result for human-readable output."""
    if value is None:
        return "pending"
    return "yes" if value else "no"


# ---------------------------------------------------------------------------
# run subcommand
# ---------------------------------------------------------------------------


@maintenance_cmd.command("run")
@click.option(
    "--wait",
    is_flag=True,
    default=False,
    help="Poll GET /status until maintenance.last_run_at changes.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    default=_DEFAULT_WAIT_TIMEOUT_SECONDS,
    show_default=True,
    type=click.IntRange(min=1),
    help=(
        "Maximum seconds to wait for the maintenance pass to complete "
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
def run_subcommand(
    wait: bool, timeout_seconds: int, api_url: str, api_key: str | None
) -> None:
    """Trigger an immediate maintenance pass.

    With ``--wait``, polls until the pass completes.
    Exits 0 on success or timeout; exits 2 when the pass completed with errors.
    """
    try:
        key = _resolve_api_key(api_key)
    except Exception as exc:
        click.echo(f"Error resolving API key: {exc}", err=True)
        raise SystemExit(1) from exc

    headers = {"Authorization": f"Bearer {key}"}
    trigger_url = f"{api_url.rstrip('/')}/maintenance/trigger"

    # Capture the baseline BEFORE triggering so we don't miss a pass that
    # completes instantly (empty collection set, test env, etc.).
    original_last_run_at: str | None = None
    if wait:
        status_url = f"{api_url.rstrip('/')}/status"
        try:
            original_last_run_at = _get_last_run_at(status_url, headers)
        except _CONNECT_FAIL:
            click.echo(_server_connect_fail_msg(api_url.rstrip('/')), err=True)
            raise SystemExit(0)

    try:
        resp = httpx.post(trigger_url, headers=headers)
    except _CONNECT_FAIL:
        # ponytail: narrow connect-fail catch before broad HTTPError — ReadTimeout must NOT
        # be misreported as "server not running"; ConnectTimeout (no listener) is fine.
        click.echo(_server_connect_fail_msg(api_url.rstrip('/')), err=True)
        raise SystemExit(0)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code != 202:
        click.echo(
            f"Error: server returned {resp.status_code}: {resp.text}", err=True
        )
        raise SystemExit(1)

    try:
        body = resp.json()
    except ValueError:
        body = {}

    status_value: str = body.get("status", "triggered")
    if status_value == "already_triggered":
        click.echo("Maintenance pass already in progress (already_triggered).")
    else:
        click.echo("Maintenance pass triggered.")

    if not wait:
        return

    _wait_for_pass(api_url, headers, original_last_run_at, timeout_seconds)


def _wait_for_pass(
    api_url: str,
    headers: dict[str, str],
    original_last_run_at: str | None,
    timeout_seconds: int = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> None:
    """Poll GET /status until maintenance.last_run_at changes, then print result.

    ``original_last_run_at`` must be captured BEFORE the POST trigger is sent
    to avoid a race where a fast-completing pass is missed.

    Exit codes:
    - Exits 0 on successful completion (last_run_at changed, no errors).
    - Exits 0 on timeout — prints a recovery hint on stderr so the operator
      knows how to follow up (breaking change from D5 which exited 1).
    - Exits 2 when the pass completed but at least one collection reported an
      error in its ``last_error`` field.
    """
    status_url = f"{api_url.rstrip('/')}/status"
    max_polls = max(1, timeout_seconds // _POLL_INTERVAL_SECONDS)

    click.echo(f"Waiting for maintenance pass to complete (current: {original_last_run_at})...")

    for _ in range(max_polls):
        time.sleep(_POLL_INTERVAL_SECONDS)
        try:
            current_last_run_at, has_errors = _get_maintenance_state(status_url, headers)
        except _CONNECT_FAIL:
            continue  # transient loss of connectivity mid-poll; keep waiting
        if current_last_run_at is None:
            # Could not reach server or maintenance=null.
            continue
        if current_last_run_at != original_last_run_at:
            if has_errors:
                click.echo(
                    "Maintenance pass completed with errors. "
                    "Run 'archon-search maintenance status' to see details.",
                    err=True,
                )
                raise SystemExit(2)
            click.echo(f"Maintenance pass complete. last_run_at={current_last_run_at}")
            return

    click.echo(
        f"Timed out after {timeout_seconds}s waiting for maintenance pass to complete. "
        "Poll with 'archon-search maintenance status' to check progress.",
        err=True,
    )
    raise SystemExit(0)


def _get_maintenance_state(
    status_url: str, headers: dict[str, str]
) -> tuple[str | None, bool]:
    """Fetch GET /status and return (maintenance.last_run_at, has_errors).

    ``has_errors`` is True when any collection in ``collection_health`` has a
    non-null ``last_error`` field in the most recent response, indicating the
    maintenance pass completed with at least one failure.

    Returns ``(None, False)`` on transient errors (5xx) or unparseable payloads
    — the caller should continue polling.
    Raises ``SystemExit(1)`` on fatal errors (4xx, network failures).
    """
    try:
        resp = httpx.get(status_url, headers=headers, timeout=5.0)
    except _CONNECT_FAIL:
        raise  # propagate so callers can distinguish server-down from other errors
    except httpx.HTTPError as exc:
        click.echo(f"Error polling server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code != 200:
        if resp.status_code >= 500:
            # Transient server error — log a warning and let the loop continue.
            click.echo(
                f"Warning: server returned {resp.status_code} while polling (transient, retrying...)",
                err=True,
            )
            return None, False
        # 4xx errors (auth failure, not found) are fatal.
        click.echo(
            f"Error: server returned {resp.status_code} while polling", err=True
        )
        raise SystemExit(1)

    try:
        payload = resp.json()
    except ValueError:
        return None, False

    maintenance = payload.get("maintenance")
    if maintenance is None:
        return None, False

    last_run_at: str | None = maintenance.get("last_run_at")
    collection_health: list[dict] = maintenance.get("collection_health") or []
    has_errors = any(
        entry.get("last_error") is not None for entry in collection_health
    )
    return last_run_at, has_errors


def _get_last_run_at(status_url: str, headers: dict[str, str]) -> str | None:
    """Fetch GET /status and return maintenance.last_run_at, or None on transient error.

    Note: ``httpx.ConnectError`` propagates to the caller (re-raised by
    ``_get_maintenance_state``) so the caller can distinguish server-down
    from other transient errors.

    Thin wrapper around ``_get_maintenance_state`` for callers that only need
    the timestamp (e.g., capturing the baseline before POST /maintenance/trigger).
    """
    last_run_at, _ = _get_maintenance_state(status_url, headers)
    return last_run_at
