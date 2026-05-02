"""archon-search ingest subcommand."""
from __future__ import annotations

import asyncio
from pathlib import Path

import click

from archon_search.config import load_config
from archon_search.pipeline import create_pipeline


@click.command()
@click.option("--path", "ingest_path", required=True, type=click.Path(path_type=Path), help="Directory to ingest")
@click.option("--collection", default=None, help="Collection name (defaults to path basename)")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def ingest(ingest_path: Path, collection: str | None, config_path: Path | None) -> None:
    """Ingest documents from a directory into a collection."""
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        click.echo(f"Error loading config: {exc}", err=True)
        raise SystemExit(1)

    collection_name = collection or ingest_path.name

    async def _run() -> None:
        pipeline = create_pipeline(cfg)
        try:
            await pipeline.store.connect()
            result = await pipeline.ingest(str(ingest_path), collection_name=collection_name)
            click.echo(f"Ingest complete: {result}")
        finally:
            await pipeline.store.disconnect()

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error during ingest: {exc}", err=True)
        raise SystemExit(1)
