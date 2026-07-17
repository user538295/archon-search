"""archon-search collection subcommands: list, add, remove, info, reindex."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import click
import httpx

from archon_search.cli._helpers import _poll_job
from archon_search.config import load_config
from archon_search.key_manager import load_or_generate_key
from archon_search.pipeline import create_pipeline

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

logger = logging.getLogger(__name__)


@click.group()
def collection() -> None:
    """Manage collections."""


@collection.command("list")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def list_cmd(config_path: Path | None) -> None:
    """List all collections."""
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        click.echo(f"Error loading config: {exc}", err=True)
        raise SystemExit(1)

    async def _run() -> None:
        pipeline = create_pipeline(cfg)
        try:
            await pipeline.store.connect()
            collections = await pipeline.store.list_collections()
            if not collections:
                click.echo("No collections found.")
            else:
                for c in collections:
                    click.echo(f"{c.name}  docs={c.doc_count}  chunks={c.chunk_count}")
        finally:
            await pipeline.store.disconnect()

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@collection.command("add")
@click.argument("path")
@click.option("--wait", "wait_flag", is_flag=True, default=False, help="Poll until the ingest job completes")
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
def add(path: str, wait_flag: bool, api_url: str, api_key: str | None) -> None:
    """Add a path to collections and ingest it."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")
    post_url = f"{base_url}/collections/"

    try:
        resp = httpx.post(post_url, json={"path": path}, headers=headers)
    except httpx.ConnectError:
        click.echo(
            "archon-search serve is not running. Start it first.",
            err=True,
        )
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 409:
        try:
            detail = resp.json().get("detail", "collection already registered")
        except Exception:
            detail = "collection already registered"
        click.echo(f"Error: {detail}", err=True)
        raise SystemExit(1)

    if resp.status_code != 202:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    job_data = resp.json()
    job_id: str = job_data["job_id"]
    collection_name: str = job_data["collection"]
    click.echo(f"Add collection job submitted: {job_id}. Collection: '{collection_name}'")
    click.echo(f"Track progress with: archon-search jobs status {job_id}")

    if wait_flag:
        job = _poll_job(job_id, base_url, headers)
        if job:
            click.echo(f"Collection '{collection_name}' ingested successfully.")


@collection.command("remove")
@click.argument("name")
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
def remove(name: str, api_url: str, api_key: str | None) -> None:
    """Remove a collection (proxies DELETE /collections/{name}; requires server running)."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")
    delete_url = f"{base_url}/collections/{name}"

    try:
        resp = httpx.delete(delete_url, headers=headers)
    except httpx.ConnectError:
        click.echo(
            "archon-search serve is not running. Start it first.",
            err=True,
        )
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 409:
        click.echo(
            f"Cannot remove '{name}': collection is pinned-only. Un-pin it first.",
            err=True,
        )
        raise SystemExit(1)

    if resp.status_code == 503:
        click.echo(
            f"Cannot remove '{name}': the server has a write in progress on this collection."
            " Retry after the active job completes.",
            err=True,
        )
        raise SystemExit(1)

    if resp.status_code == 404:
        click.echo(f"Error: collection '{name}' not found.", err=True)
        raise SystemExit(1)

    if resp.status_code != 200:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    click.echo(f"Removed collection '{name}'.")


@collection.command("info")
@click.argument("collection_name")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def info(collection_name: str, config_path: Path | None) -> None:
    """Show details for a collection."""
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        click.echo(f"Error loading config: {exc}", err=True)
        raise SystemExit(1)

    async def _run() -> None:
        pipeline = create_pipeline(cfg)
        try:
            await pipeline.store.connect()
            meta = await pipeline.store.get_collection_meta(collection_name)
            if meta is None:
                click.echo(f"Error: collection '{collection_name}' not found.", err=True)
                raise SystemExit(1)
            click.echo(str(meta))
        finally:
            await pipeline.store.disconnect()

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@collection.command("reindex-metadata")
@click.argument("collection_name")
@click.option("--dry-run", is_flag=True, default=False, help="Report counts without writing")
@click.option(
    "--normalize-timestamps/--no-normalize-timestamps",
    default=True,
    help="Rewrite indexed_at/updated_at to fixed-width UTC (YYYY-MM-DDTHH:MM:SS.ffffffZ)",
)
@click.option("--wait", "wait_flag", is_flag=True, default=False, help="Poll until the reindex-metadata job completes")
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
def reindex_metadata_cmd(
    collection_name: str,
    dry_run: bool,
    normalize_timestamps: bool,
    wait_flag: bool,
    api_url: str,
    api_key: str | None,
) -> None:
    """Backfill metadata fields (file_type, updated_at, ingested_by) on an existing collection.

    Routes through the archon-search server (POST /collections/{name}/reindex-metadata).
    Use --wait to poll until the job completes.
    """
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")
    post_url = f"{base_url}/collections/{collection_name}/reindex-metadata"
    body = {"dry_run": dry_run, "normalize_timestamps": normalize_timestamps}

    try:
        resp = httpx.post(post_url, json=body, headers=headers)
    except httpx.ConnectError:
        click.echo(
            "archon-search serve is not running. Start it first.",
            err=True,
        )
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 404:
        click.echo(f"Error: collection '{collection_name}' not found.", err=True)
        raise SystemExit(1)

    if resp.status_code == 409:
        try:
            detail = resp.json().get("detail", "metadata reindex already in progress")
        except Exception:
            detail = "metadata reindex already in progress"
        click.echo(f"Error: {detail}", err=True)
        raise SystemExit(1)

    if resp.status_code != 202:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    job_data = resp.json()
    job_id: str = job_data["job_id"]
    click.echo(f"Reindex-metadata job submitted: {job_id}. Track progress with: archon-search jobs status {job_id}")

    if wait_flag:
        job = _poll_job(job_id, base_url, headers)
        if job:
            result = job.get("result") or {}
            click.echo(
                f"Reindex-metadata complete for '{collection_name}'. "
                f"processed={result.get('processed', 0)}, "
                f"updated={result.get('updated', 0)}, "
                f"skipped={result.get('skipped', 0)}, "
                f"ts_normalized={result.get('ts_normalized', 0)}"
            )
            warnings = result.get("warnings") or []
            if warnings:
                click.echo("warnings:")
                for w in warnings:
                    click.echo(f"  - {w}")


@collection.command("migrate")
@click.argument("collection_name")
@click.option("--dry-run", is_flag=True, default=False, help="Print pending migrations without applying (default behaviour)")
@click.option("--apply", "apply_flag", is_flag=True, default=False, help="Apply migrations (synchronous for in-place; async job for rewrite)")
@click.option("--backup-first", "backup_first", is_flag=True, default=False, help="Confirm you have a backup (required for rewrite migrations)")
@click.option("--wait", "wait_flag", is_flag=True, default=False, help="When a rewrite job is created (202), poll until done and print progress")
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
def migrate_cmd(
    collection_name: str,
    dry_run: bool,
    apply_flag: bool,
    backup_first: bool,
    wait_flag: bool,
    api_url: str,
    api_key: str | None,
) -> None:
    """Show pending schema migrations for a collection.

    Without flags (or with --dry-run) prints the list of pending migrations
    and exits without modifying anything. Use --apply to apply in-place
    migrations synchronously. Use --apply --backup-first for rewrite migrations.
    Add --wait to poll the rewrite job until completion.
    """
    if dry_run and apply_flag:
        click.echo("Error: --dry-run and --apply are mutually exclusive.", err=True)
        raise SystemExit(1)

    if dry_run and backup_first:
        click.echo("Error: --backup-first has no effect with --dry-run (use --apply --backup-first).", err=True)
        raise SystemExit(1)

    if backup_first and not apply_flag:
        click.echo("Error: --backup-first requires --apply.", err=True)
        raise SystemExit(1)

    if wait_flag and not apply_flag:
        click.echo("Error: --wait requires --apply.", err=True)
        raise SystemExit(1)

    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")

    if apply_flag:
        post_url = f"{base_url}/collections/{collection_name}/migrate"
        body: dict = {"dry_run": False, "backup_confirmed": backup_first}
        try:
            resp = httpx.post(post_url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            click.echo(f"Error contacting server: {exc}", err=True)
            raise SystemExit(1)

        if resp.status_code == 404:
            click.echo(f"Error: collection '{collection_name}' not found.", err=True)
            raise SystemExit(1)

        if resp.status_code == 202:
            # Rewrite job created — optionally poll
            job_data = resp.json()
            job_id: str = job_data["job_id"]
            click.echo(f"Migration job submitted: {job_id}")
            if wait_flag:
                _poll_migration_job(job_id, base_url, headers)
            return

        if resp.status_code != 200:
            click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
            raise SystemExit(1)

        data = resp.json()
        applied = data.get("migrations_applied", [])
        if not applied:
            click.echo(f"Collection '{collection_name}' is up to date — no migrations applied.")
        else:
            click.echo(f"Applied {len(applied)} migration(s) to '{collection_name}':")
            for name in applied:
                click.echo(f"  - {name}")
        return

    url = f"{base_url}/collections/{collection_name}/migrations/pending"

    try:
        resp = httpx.get(url, headers=headers)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 404:
        click.echo(f"Error: collection '{collection_name}' not found.", err=True)
        raise SystemExit(1)

    if resp.status_code != 200:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    data = resp.json()
    pending = data.get("pending", [])

    if not pending:
        click.echo(f"Collection '{collection_name}' is up to date — no pending migrations.")
        return

    click.echo(f"Pending migrations for '{collection_name}' (schema_version={data.get('schema_version', 0)}):")
    for spec in pending:
        click.echo(f"  - {spec['name']}  kind={spec['kind']}  {spec['description']}")


def _poll_migration_job(job_id: str, base_url: str, headers: dict) -> None:
    """Poll GET /jobs/{job_id} until terminal, printing progress. Exits 1 on FAILED/CANCELLED."""
    job = _poll_job(job_id, base_url, headers)
    if not job:
        # KeyboardInterrupt path — _poll_job already printed the message
        return
    click.echo("Migration complete.")


@collection.command("reindex")
@click.argument("collection_name")
@click.option("--wait", "wait_flag", is_flag=True, default=False, help="Poll until the reindex job completes")
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
def reindex(collection_name: str, wait_flag: bool, api_url: str, api_key: str | None) -> None:
    """Force full reindex of a collection."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")
    post_url = f"{base_url}/collections/{collection_name}/reindex"

    try:
        resp = httpx.post(post_url, headers=headers)
    except httpx.ConnectError:
        click.echo(
            "archon-search serve is not running. Start it first.",
            err=True,
        )
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 404:
        click.echo(f"Error: collection '{collection_name}' not found.", err=True)
        raise SystemExit(1)

    if resp.status_code != 202:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    job_data = resp.json()
    job_id: str = job_data["job_id"]
    click.echo(f"Reindex job submitted: {job_id}")

    if wait_flag:
        job = _poll_job(job_id, base_url, headers)
        if job:
            click.echo(f"Reindex complete for '{collection_name}'.")
