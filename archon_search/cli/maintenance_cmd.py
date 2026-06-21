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
import os
import time
from pathlib import Path
from typing import Any

import click
import httpx

from archon_search.config import ConfigError, load_config
from archon_search.key_manager import load_or_generate_key
from archon_search.paths import get_data_dir

_DEFAULT_API_URL = "http://localhost:8765"
_POLL_INTERVAL_SECONDS = 2
_WAIT_MAX_POLLS = 60  # 60 × 2 s = 2 min default timeout for --wait
_STATE_FILE_NAME = ".maintenance-state.json"


def _resolve_api_key(api_key: str | None) -> str:
    """Return the API key from the option, env var, or the key file."""
    if api_key:
        return api_key
    env_key = os.environ.get("ARCHON_SEARCH_API_KEY")
    if env_key:
        return env_key
    key, _ = load_or_generate_key()
    return key


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

    server_reachable = False
    server_payload = _fetch_server_status(api_url, api_key)
    if server_payload is not None:
        server_reachable = True
        maintenance_obj = server_payload.get("maintenance") or {}
        if "enabled" in maintenance_obj:
            enabled = bool(maintenance_obj["enabled"])
        if "interval_hours" in maintenance_obj:
            interval_hours = int(maintenance_obj["interval_hours"])
        if maintenance_obj.get("last_run_at") is not None:
            last_run_at = maintenance_obj["last_run_at"]
        if maintenance_obj.get("next_run_at") is not None:
            next_run_at = maintenance_obj["next_run_at"]
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
    }
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
        key = _resolve_api_key(api_key)
    except Exception:  # noqa: BLE001 — offline mode is the whole point
        return None
    headers = {"Authorization": f"Bearer {key}"}
    url = f"{api_url.rstrip('/')}/status"
    try:
        resp = httpx.get(url, headers=headers, timeout=2.0)
    except httpx.HTTPError:
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
    else:
        click.echo("Last run:  [server unavailable]")
        click.echo("Next run:  [server unavailable]")

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
def run_subcommand(wait: bool, api_url: str, api_key: str | None) -> None:
    """Trigger an immediate maintenance pass.

    With ``--wait``, polls until the pass completes.
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
        original_last_run_at = _get_last_run_at(status_url, headers)

    try:
        resp = httpx.post(trigger_url, headers=headers)
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

    _wait_for_pass(api_url, headers, original_last_run_at)


def _wait_for_pass(
    api_url: str, headers: dict[str, str], original_last_run_at: str | None
) -> None:
    """Poll GET /status until maintenance.last_run_at changes, then print result.

    ``original_last_run_at`` must be captured BEFORE the POST trigger is sent
    to avoid a race where a fast-completing pass is missed.
    """
    status_url = f"{api_url.rstrip('/')}/status"

    click.echo(f"Waiting for maintenance pass to complete (current: {original_last_run_at})...")

    for _ in range(_WAIT_MAX_POLLS):
        time.sleep(_POLL_INTERVAL_SECONDS)
        current_last_run_at = _get_last_run_at(status_url, headers)
        if current_last_run_at is None:
            # Could not reach server or maintenance=null.
            continue
        if current_last_run_at != original_last_run_at:
            click.echo(f"Maintenance pass complete. last_run_at={current_last_run_at}")
            return

    click.echo("Timed out waiting for maintenance pass to complete.", err=True)
    raise SystemExit(1)


def _get_last_run_at(status_url: str, headers: dict[str, str]) -> str | None:
    """Fetch GET /status and return maintenance.last_run_at, or None on error."""
    try:
        resp = httpx.get(status_url, headers=headers, timeout=5.0)
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
            return None
        # 4xx errors (auth failure, not found) are fatal.
        click.echo(
            f"Error: server returned {resp.status_code} while polling", err=True
        )
        raise SystemExit(1)

    try:
        payload = resp.json()
    except ValueError:
        return None

    maintenance = payload.get("maintenance")
    if maintenance is None:
        return None
    return maintenance.get("last_run_at")
