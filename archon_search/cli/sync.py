"""archon-search sync subcommand."""
from __future__ import annotations

import asyncio
from pathlib import Path

import click

from archon_search.config import load_config
from archon_search.pipeline import create_pipeline
from archon_search.progress import IndexingStateStore
from archon_search.sync import SearchCollectionSync


@click.command()
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def sync(config_path: Path | None) -> None:
    """Sync configured collections."""
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        click.echo(f"Error loading config: {exc}", err=True)
        raise SystemExit(1)

    async def _run() -> None:
        pipeline = create_pipeline(cfg)
        try:
            await pipeline.store.connect()
            state_store = IndexingStateStore(Path(cfg.db_path).expanduser())
            sync_runner = SearchCollectionSync(
                pipeline,
                state_store=state_store,
                pinned_collections=cfg.pinned_collections,
                embedding_model=cfg.embedding_model,
                chunk_size=cfg.chunk_size,
                auto_reindex_on_chunk_size_change=cfg.auto_reindex_on_chunk_size_change,
            )
            all_collections = cfg.pinned_collections + cfg.collections
            result = await sync_runner.sync(all_collections)
            click.echo(f"Sync complete: {result}")
        finally:
            await pipeline.store.disconnect()

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error during sync: {exc}", err=True)
        raise SystemExit(1)
