"""archon-search ingest subcommand."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import click

from archon_search.config import load_config
from archon_search.observability import bind_stage_recorder, new_correlation_id
from archon_search.pipeline import create_pipeline

logger = logging.getLogger(__name__)


@click.command()
@click.option("--path", "ingest_path", default=None, type=click.Path(path_type=Path), help="Directory to ingest")
@click.option("--collection", default=None, help="Collection name (defaults to path basename)")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def ingest(ingest_path: Path | None, collection: str | None, config_path: Path | None) -> None:
    """Ingest documents from a directory into a collection."""
    if ingest_path is None:
        ingest_path = Path.home() / ".archon-search" / "history" / "sessions"
        click.echo(f"No --path given, using default: {ingest_path}")

    try:
        cfg = load_config(config_path)
    except Exception as exc:
        click.echo(f"Error loading config: {exc}", err=True)
        raise SystemExit(1)

    collection_name = collection or ingest_path.name

    async def _run() -> None:
        pipeline = create_pipeline(cfg)
        timings_enabled = getattr(getattr(cfg, "observability", None), "stage_timings_enabled", True)
        try:
            await pipeline.store.connect()
            if timings_enabled:
                cid = new_correlation_id()
                with bind_stage_recorder() as recorder:
                    t0 = time.perf_counter()
                    results = await pipeline.ingest_directory(ingest_path, collection_name, embedder=pipeline._global_embedder)
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
                results = await pipeline.ingest_directory(ingest_path, collection_name, embedder=pipeline._global_embedder)
            ok = sum(1 for r in results if r.status == "ok")
            errors = sum(1 for r in results if r.status == "error")
            click.echo(f"Ingest complete: {ok} ingested, {errors} errors.")
        finally:
            await pipeline.store.disconnect()

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error during ingest: {exc}", err=True)
        raise SystemExit(1)
