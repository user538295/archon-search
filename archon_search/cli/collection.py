"""archon-search collection subcommands: list, add, remove, info, reindex."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import click
import httpx
import tomlkit

from archon_search.cli._helpers import _poll_job
from archon_search.config import get_default_config_path, load_config
from archon_search.embedder import make_embedder
from archon_search.key_manager import load_or_generate_key
from archon_search.observability import bind_stage_recorder, new_correlation_id
from archon_search.pipeline import create_pipeline

_DEFAULT_API_URL = "http://localhost:8765"
_POLL_INTERVAL_SECONDS = 2
_TERMINAL_STATUSES = {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}


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
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def add(path: str, config_path: Path | None) -> None:
    """Add a path to collections and ingest it."""
    config_file = config_path or get_default_config_path()

    try:
        cfg = load_config(config_file)
    except Exception as exc:
        click.echo(f"Error loading config: {exc}", err=True)
        raise SystemExit(1)

    # Add to config if not already present
    if path not in cfg.collections:
        if config_file.exists():
            doc = tomlkit.parse(config_file.read_text(encoding="utf-8"))
        else:
            doc = tomlkit.document()

        if "collections" not in doc:
            doc.add("collections", tomlkit.table())

        existing = list(doc["collections"].get("collections", []))  # type: ignore[union-attr]
        if path not in existing:
            existing.append(path)
            doc["collections"]["collections"] = existing  # type: ignore[index]
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text(tomlkit.dumps(doc), encoding="utf-8")  # noqa: durable-write

    # Reload config and ingest
    cfg = load_config(config_file)
    from archon_search.sync import path_to_collection_name  # noqa: PLC0415

    collection_name = path_to_collection_name(path)

    async def _run() -> None:
        pipeline = create_pipeline(cfg)
        timings_enabled = getattr(getattr(cfg, "observability", None), "stage_timings_enabled", True)
        try:
            await pipeline.store.connect()
            if timings_enabled:
                cid = new_correlation_id()
                with bind_stage_recorder() as recorder:
                    t0 = time.perf_counter()
                    result = await pipeline.ingest_directory(Path(path).expanduser(), collection_name, embedder=pipeline._global_embedder)
                    recorder.record("total", (time.perf_counter() - t0) * 1000.0)
                    logger.info(
                        "stage timings",
                        extra={
                            "event_type": "stage_timings",
                            "correlation_id": cid,
                            "endpoint": "ingest",
                            "collection": collection_name,
                            "stage_timings_ms": recorder.stage_sums_ms,
                        },
                    )
            else:
                result = await pipeline.ingest_directory(Path(path).expanduser(), collection_name, embedder=pipeline._global_embedder)
            click.echo(f"Added collection '{collection_name}': {len(result)} files ingested")
        finally:
            await pipeline.store.disconnect()

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@collection.command("remove")
@click.argument("path")
@click.option("--dry-run", is_flag=True, default=False, help="Print what would be done without executing")
@click.option("--force", is_flag=True, default=False, help="Proceed even if service is running")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def remove(path: str, dry_run: bool, force: bool, config_path: Path | None) -> None:
    """Remove a collection."""
    if dry_run and force:
        click.echo("Error: --dry-run and --force are mutually exclusive", err=True)
        raise SystemExit(1)

    config_file = config_path or get_default_config_path()

    try:
        cfg = load_config(config_file)
    except Exception as exc:
        click.echo(f"Error loading config: {exc}", err=True)
        raise SystemExit(1)

    resolved = Path(path).expanduser().resolve()
    in_pinned = any(Path(p).expanduser().resolve() == resolved for p in cfg.pinned_collections)
    in_collections = any(Path(p).expanduser().resolve() == resolved for p in cfg.collections)

    # Pinned-only check: in pinned but NOT in collections
    if in_pinned and not in_collections:
        click.echo(
            f"Error: '{path}' is a pinned collection. Remove it from pinned_collections first.",
            err=True,
        )
        raise SystemExit(1)

    if dry_run:
        click.echo(f"[dry-run] Would remove collection for path: {path}")
        return

    from archon_search.sync import path_to_collection_name  # noqa: PLC0415

    collection_name = path_to_collection_name(path)

    async def _run() -> None:
        pipeline = create_pipeline(cfg)
        try:
            await pipeline.store.connect()
            await pipeline.store.drop_collection(collection_name)
            click.echo(f"Removed collection '{collection_name}'.")
        finally:
            await pipeline.store.disconnect()

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    # Remove from config
    if config_file.exists():
        doc = tomlkit.parse(config_file.read_text(encoding="utf-8"))
        if "collections" in doc:
            existing = list(doc["collections"].get("collections", []))  # type: ignore[union-attr]
            if path in existing:
                existing.remove(path)
                doc["collections"]["collections"] = existing  # type: ignore[index]
                config_file.write_text(tomlkit.dumps(doc), encoding="utf-8")  # noqa: durable-write


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
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def reindex_metadata_cmd(
    collection_name: str,
    dry_run: bool,
    normalize_timestamps: bool,
    config_path: Path | None,
) -> None:
    """Backfill metadata fields (file_type, updated_at, ingested_by) on an existing collection.

    Reads each row, refreshes file_type from source_path extension and
    updated_at from mtime, and rewrites legacy ``"archon-search-cli"`` ->
    ``"reindex"``. When --normalize-timestamps is on (default), indexed_at and
    updated_at are also rewritten to fixed-width UTC where they do not already
    conform. Holds a per-collection lock for the duration; ingest into the same
    collection is blocked until reindex finishes.
    """
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        click.echo(f"Error loading config: {exc}", err=True)
        raise SystemExit(1)

    async def _run() -> None:
        pipeline = create_pipeline(cfg)
        try:
            await pipeline.store.connect()

            def _on_progress(processed: int, total: int) -> None:
                click.echo(f"reindex-metadata: {collection_name} - {processed}/{total}")

            result = await pipeline.store.reindex_metadata(
                collection_name,
                dry_run=dry_run,
                normalize_timestamps=normalize_timestamps,
                progress_cb=_on_progress,
            )
            click.echo(
                f"reindex-metadata: {collection_name} - done. "
                f"processed={result.processed}, updated={result.updated}, "
                f"ts_normalized={result.ts_normalized}, "
                f"warnings={len(result.warnings)}"
            )
            if result.warnings:
                click.echo("warnings:")
                for w in result.warnings:
                    click.echo(f"  - {w}")
        finally:
            try:
                await pipeline.store.disconnect()
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


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
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def reindex(collection_name: str, config_path: Path | None) -> None:
    """Force full reindex of a collection."""
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        click.echo(f"Error loading config: {exc}", err=True)
        raise SystemExit(1)

    async def _run() -> None:
        from archon_search.progress import IndexingStateStore  # noqa: PLC0415
        from archon_search.sync import path_to_collection_name as p2cn  # noqa: PLC0415

        # Find source path for this collection
        source_path: str | None = None
        for p in cfg.pinned_collections + cfg.collections:
            if p2cn(p) == collection_name:
                source_path = p
                break
        if source_path is None:
            click.echo(f"Error: collection '{collection_name}' not found in config.", err=True)
            raise SystemExit(1)

        pipeline = create_pipeline(cfg)
        try:
            await pipeline.store.connect()

            # Resolve per-collection embedder from CollectionMeta
            meta = await pipeline.store.get_collection_meta(collection_name)
            if meta is not None and meta.pending_embedding_model:
                embedder = make_embedder(meta.pending_embedding_model)
            elif meta is not None and meta.active_embedding_model:
                embedder = make_embedder(meta.active_embedding_model)
                if cfg.embedding_model and meta.active_embedding_model != cfg.embedding_model:
                    logger.warning("using per-collection model %s for %s", meta.active_embedding_model, collection_name)
            else:
                embedder = pipeline._global_embedder

            # Clear state to force full reindex
            state_store = IndexingStateStore(Path(cfg.db_path).expanduser())
            state_store.remove_collection(collection_name)
            # Drop old data
            try:
                await pipeline.store.drop_collection(collection_name)
            except Exception:
                pass
            # Reindex — failure must NOT write state back
            timings_enabled = getattr(getattr(cfg, "observability", None), "stage_timings_enabled", True)
            if timings_enabled:
                cid = new_correlation_id()
                with bind_stage_recorder() as recorder:
                    t0 = time.perf_counter()
                    results = await pipeline.ingest_directory(
                        Path(source_path).expanduser(), collection_name, force_regenerate_description=True,
                        embedder=embedder,
                    )
                    recorder.record("total", (time.perf_counter() - t0) * 1000.0)
                    logger.info(
                        "stage timings",
                        extra={
                            "event_type": "stage_timings",
                            "correlation_id": cid,
                            "endpoint": "ingest",
                            "collection": collection_name,
                            "stage_timings_ms": recorder.stage_sums_ms,
                        },
                    )
            else:
                results = await pipeline.ingest_directory(
                    Path(source_path).expanduser(), collection_name, force_regenerate_description=True,
                    embedder=embedder,
                )
            ok = sum(1 for r in results if r.status == "ok")
            errors = sum(1 for r in results if r.status == "error")
            click.echo(f"Reindex complete for '{collection_name}': {ok} ingested, {errors} errors.")

            # Promote pending → active on success (model-change reindex)
            if meta is not None and meta.pending_embedding_model:
                meta.active_embedding_model = meta.pending_embedding_model
                meta.pending_embedding_model = None
                meta.needs_reindex = False
                meta.reindex_job_id = None
                await pipeline.store.update_collection_meta(meta)
        finally:
            await pipeline.store.disconnect()

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
