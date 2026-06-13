"""``archon-search backup`` CLI group (D2 Task 5.1).

Provides two operator-facing commands:

* ``archon-search backup --now [--wait]`` — POST ``/backup/trigger`` to enqueue
  scheduled backups for every eligible collection in the caller's namespace.
  With ``--wait``, polls the resulting jobs until terminal.
* ``archon-search backup status [--json]`` — print current backup state.
  Reads the on-disk ``.backup-state.json`` and counts archives in the backup
  directory directly so the command is usable even when the server is offline;
  when the server is reachable, merges ``last_tick_at`` and ``next_run_at``
  from ``GET /status``.
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
_TERMINAL_STATUSES = {"DONE", "FAILED", "CANCELLED"}
_STATE_FILE_NAME = ".backup-state.json"


def _resolve_api_key(api_key: str | None) -> str:
    """Return the API key from the option, env var, or the key file."""
    if api_key:
        return api_key
    env_key = os.environ.get("ARCHON_SEARCH_API_KEY")
    if env_key:
        return env_key
    key, _ = load_or_generate_key()
    return key


@click.group("backup", invoke_without_command=True)
@click.option("--now", is_flag=True, default=False,
              help="Trigger immediate backup of all non-excluded collections.")
@click.option("--wait", is_flag=True, default=False,
              help="Poll until all triggered backup jobs complete (requires --now).")
@click.option("--api-url", default=_DEFAULT_API_URL, show_default=True,
              help="Base URL of the archon-search server.")
@click.option("--api-key", default=None,
              help="API key (falls back to ARCHON_SEARCH_API_KEY env var or the key file).")
@click.pass_context
def backup_cmd(
    ctx: click.Context,
    now: bool,
    wait: bool,
    api_url: str,
    api_key: str | None,
) -> None:
    """Manage scheduled backups."""
    if ctx.invoked_subcommand is not None:
        return
    if not now:
        click.echo(ctx.get_help())
        return
    _trigger_backup(api_url, api_key, wait)


def _trigger_backup(api_url: str, api_key: str | None, wait: bool) -> None:
    """POST /backup/trigger and optionally poll resulting jobs."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    url = f"{api_url.rstrip('/')}/backup/trigger"

    try:
        resp = httpx.post(url, headers=headers)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code != 202:
        click.echo(
            f"Error: server returned {resp.status_code}: {resp.text}", err=True
        )
        raise SystemExit(1)

    body = resp.json()
    queued: list[str] = body.get("queued", [])
    skipped: list[dict[str, str]] = body.get("skipped", [])

    if queued:
        click.echo("Queued jobs:")
        for job_id in queued:
            click.echo(job_id)
    else:
        click.echo("No jobs queued.")

    if skipped:
        click.echo("Skipped collections:")
        for item in skipped:
            click.echo(f"  {item.get('collection', '?')}: {item.get('reason', '?')}")

    if not wait or not queued:
        return

    _wait_for_jobs(queued, api_url, headers)


def _wait_for_jobs(job_ids: list[str], api_url: str, headers: dict[str, str]) -> None:
    """Poll each job until terminal; exit 1 if any FAILED."""
    failed: list[str] = []
    pending = set(job_ids)
    while pending:
        for job_id in list(pending):
            url = f"{api_url.rstrip('/')}/jobs/{job_id}"
            try:
                resp = httpx.get(url, headers=headers)
            except httpx.HTTPError as exc:
                click.echo(f"Error polling job {job_id}: {exc}", err=True)
                raise SystemExit(1) from exc
            if resp.status_code != 200:
                click.echo(
                    f"Error polling job {job_id}: server returned {resp.status_code}",
                    err=True,
                )
                raise SystemExit(1)
            job = resp.json()
            status: str = job.get("status", "")
            if status in _TERMINAL_STATUSES:
                pending.discard(job_id)
                if status == "FAILED":
                    err = job.get("error") or "unknown error"
                    click.echo(f"Backup FAILED for {job_id}: {err}", err=True)
                    failed.append(job_id)
                elif status == "CANCELLED":
                    click.echo(f"Backup cancelled: {job_id}")
        if pending:
            time.sleep(_POLL_INTERVAL_SECONDS)

    if failed:
        raise SystemExit(1)
    click.echo("Backup completed for all collections")


# ---------------------------------------------------------------------------
# status subcommand
# ---------------------------------------------------------------------------


@backup_cmd.command("status")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit machine-readable JSON instead of human-readable text.")
@click.option("--api-url", default=_DEFAULT_API_URL, show_default=True,
              help="Base URL of the archon-search server.")
@click.option("--api-key", default=None,
              help="API key (falls back to ARCHON_SEARCH_API_KEY env var or the key file).")
def status_subcommand(as_json: bool, api_url: str, api_key: str | None) -> None:
    """Print current backup state (offline-capable)."""
    data = _gather_status(api_url, api_key)
    if as_json:
        click.echo(json.dumps(data["payload"], indent=2, sort_keys=True))
        return
    _print_status_text(data)


def _gather_status(api_url: str, api_key: str | None) -> dict[str, Any]:
    """Read offline state first, then try the server for last_tick/next_run."""
    data_dir = get_data_dir()
    enabled, interval_hours, excluded, output_dir = _read_config_for_status(data_dir)

    state = _read_state_file(data_dir)
    backups_root = output_dir or (data_dir / "backups")

    # Build per-collection rows from on-disk state. Keys look like "{ns}/{col}".
    collection_status: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for key, ts in sorted(state.items()):
        if "/" in key:
            ns, col = key.split("/", 1)
        else:
            ns, col = "default", key
        archive_count = _count_archives(backups_root, ns, col)
        collection_status.append(
            {
                "collection": col,
                "namespace": ns,
                "last_backup_at": ts,
                "archive_count": archive_count,
            }
        )
        seen.add((ns, col))

    # Also surface collections that have archives on disk but no state entry.
    if backups_root.exists():
        for ns_dir in sorted(p for p in backups_root.iterdir() if p.is_dir()):
            ns = ns_dir.name
            for archive in ns_dir.glob("*.backup.*.tar.gz"):
                col = archive.name.split(".backup.", 1)[0]
                if (ns, col) in seen:
                    continue
                seen.add((ns, col))
                collection_status.append(
                    {
                        "collection": col,
                        "namespace": ns,
                        "last_backup_at": None,
                        "archive_count": _count_archives(backups_root, ns, col),
                    }
                )

    last_tick_at: str | None = None
    next_run_at: str | None = None
    server_reachable = False

    server_payload = _fetch_server_status(api_url, api_key)
    if server_payload is not None:
        server_reachable = True
        backup_obj = server_payload.get("backup") or {}
        last_tick_at = backup_obj.get("last_tick_at")
        next_run_at = backup_obj.get("next_run_at")
        # Prefer authoritative server values when present.
        if "enabled" in backup_obj:
            enabled = bool(backup_obj["enabled"])
        if "interval_hours" in backup_obj:
            interval_hours = int(backup_obj["interval_hours"])
        if "collections_excluded" in backup_obj:
            excluded = list(backup_obj["collections_excluded"])
        server_collection_status = backup_obj.get("collection_status")
        if server_collection_status:
            # Server view is authoritative for the caller's namespace.
            collection_status = [
                {
                    "collection": item.get("collection", ""),
                    "namespace": "default",
                    "last_backup_at": item.get("last_backup_at"),
                    "archive_count": item.get("archive_count", 0),
                }
                for item in server_collection_status
            ]

    payload = {
        "enabled": enabled,
        "interval_hours": interval_hours,
        "last_tick_at": last_tick_at,
        "next_run_at": next_run_at,
        "collections_excluded": excluded,
        "collection_status": [
            {
                "collection": row["collection"],
                "last_backup_at": row["last_backup_at"],
                "archive_count": row["archive_count"],
            }
            for row in collection_status
        ],
    }
    return {"payload": payload, "rows": collection_status,
            "server_reachable": server_reachable}


def _read_config_for_status(
    data_dir: Path,
) -> tuple[bool, int, list[str], Path | None]:
    """Load config offline; tolerate missing/broken config files."""
    _ = data_dir  # data_dir is consumed by load_config via env var or default.
    try:
        config = load_config()
    except (ConfigError, OSError, ValueError):
        return False, 0, [], None
    backup = config.backup
    enabled = backup.interval_hours > 0
    output_dir = Path(backup.output_dir) if backup.output_dir else None
    return enabled, backup.interval_hours, list(backup.exclude), output_dir


def _read_state_file(data_dir: Path) -> dict[str, str]:
    state_file = data_dir / _STATE_FILE_NAME
    if not state_file.exists():
        return {}
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _count_archives(backups_root: Path, ns: str, col: str) -> int:
    ns_dir = backups_root / ns
    if not ns_dir.exists():
        return 0
    return len(list(ns_dir.glob(f"{col}.backup.*.tar.gz")))


def _fetch_server_status(
    api_url: str, api_key: str | None
) -> dict[str, Any] | None:
    """Return ``/status`` JSON or None if the server is unreachable."""
    try:
        key = _resolve_api_key(api_key)
    except Exception:  # noqa: BLE001 — offline mode is the whole point.
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
    rows = data["rows"]
    server_reachable: bool = data["server_reachable"]

    if payload["enabled"]:
        # Resolve keep from config for the header line if available.
        try:
            config = load_config()
            keep = config.backup.keep
        except Exception:  # noqa: BLE001
            keep = None
        keep_str = f", keep={keep}" if keep is not None else ""
        click.echo(
            f"Backup: enabled (interval={payload['interval_hours']}h{keep_str})"
        )
    else:
        click.echo("Backup: disabled")

    if server_reachable:
        click.echo(f"Last tick: {payload['last_tick_at'] or 'never'}")
        click.echo(f"Next run:  {payload['next_run_at'] or 'unknown'}")
    else:
        click.echo("Last tick: [server unavailable]")
        click.echo("Next run:  [server unavailable]")

    if payload["collections_excluded"]:
        click.echo(f"Excluded: {', '.join(payload['collections_excluded'])}")

    if not rows:
        click.echo("No collections tracked yet.")
        return

    for row in rows:
        ts = row["last_backup_at"] or "never"
        count = row["archive_count"]
        ns = row.get("namespace", "default")
        prefix = f"{ns}/" if ns and ns != "default" else ""
        click.echo(
            f"  {prefix}{row['collection']}: last backup {ts}, {count} archive(s)"
        )
