"""archon-search collection subcommands: list, add, remove, info, reindex."""
from __future__ import annotations

import asyncio
from pathlib import Path

import click
import tomlkit

from archon_search.config import get_default_config_path, load_config
from archon_search.pipeline import create_pipeline


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
            config_file.write_text(tomlkit.dumps(doc), encoding="utf-8")

    # Reload config and ingest
    cfg = load_config(config_file)
    from archon_search.sync import path_to_collection_name  # noqa: PLC0415

    collection_name = path_to_collection_name(path)

    async def _run() -> None:
        pipeline = create_pipeline(cfg)
        try:
            await pipeline.store.connect()
            result = await pipeline.ingest_directory(Path(path).expanduser(), collection_name)
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
                config_file.write_text(tomlkit.dumps(doc), encoding="utf-8")


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
            # Clear state to force full reindex
            state_store = IndexingStateStore(Path(cfg.db_path).expanduser())
            state_store.remove_collection(collection_name)
            # Drop old data
            try:
                await pipeline.store.drop_collection(collection_name)
            except Exception:
                pass
            # Reindex
            results = await pipeline.ingest_directory(
                Path(source_path).expanduser(), collection_name, force_regenerate_description=True
            )
            ok = sum(1 for r in results if r.status == "ok")
            errors = sum(1 for r in results if r.status == "error")
            click.echo(f"Reindex complete for '{collection_name}': {ok} ingested, {errors} errors.")
        finally:
            await pipeline.store.disconnect()

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
