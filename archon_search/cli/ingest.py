"""archon-search ingest subcommand."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from pathlib import Path

import click

from archon_search.config import load_config
from archon_search.observability import bind_stage_recorder, new_correlation_id
from archon_search.paths import get_data_dir
from archon_search.pipeline import create_pipeline

logger = logging.getLogger(__name__)

# Files larger than this threshold receive a pre-parse notice on stderr.
# Independent of max_file_mb — this is a UX hint for slow parses.
_LARGE_FILE_NOTICE_MB = 10


@click.command()
@click.option("--path", "ingest_path", default=None, type=click.Path(path_type=Path), help="File or directory to ingest")
@click.option("--collection", default=None, help="Collection name (defaults to path basename or file stem)")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def ingest(ingest_path: Path | None, collection: str | None, config_path: Path | None) -> None:
    """Ingest documents from a file or directory into a collection."""
    if ingest_path is None:
        # Resolved lazily via get_data_dir() so ARCHON_SEARCH_DATA_DIR
        # redirects the default ingest path at call time (C9 Task 2.6).
        ingest_path = get_data_dir() / "history" / "sessions"
        click.echo(f"No --path given, using default: {ingest_path}")

    try:
        cfg = load_config(config_path)
    except Exception as exc:
        click.echo(f"Error loading config: {exc}", err=True)
        raise SystemExit(1)

    is_single_file = ingest_path.is_file()

    if is_single_file:
        # Single-file mode: collection defaults to the file's stem (no extension)
        collection_name = collection or ingest_path.stem
        # Large-file notice: print to stderr before handing off to the parser
        try:
            file_size_mb = math.ceil(os.path.getsize(ingest_path) / (1024 * 1024))
            if file_size_mb > _LARGE_FILE_NOTICE_MB:
                click.echo(
                    f"Parsing large file ({file_size_mb} MB); this may take a while…",
                    err=True,
                )
        except OSError:
            pass  # Can't stat the file; skip the notice
    else:
        # Directory mode (or default history path): collection defaults to the directory name
        collection_name = collection or ingest_path.name

    def _print_dir_summary(results: list) -> None:
        ok = sum(1 for r in results if r.status == "ok")
        errors = sum(1 for r in results if r.status == "error")
        for r in results:
            for warning in r.warnings:
                click.echo(f"Warning: {warning}", err=True)
        click.echo(f"Ingest complete: {ok} ingested, {errors} errors.")

    async def _run() -> None:
        pipeline = create_pipeline(cfg)
        timings_enabled = getattr(getattr(cfg, "observability", None), "stage_timings_enabled", True)
        try:
            await pipeline.store.connect()
            if is_single_file:
                result = await pipeline.ingest_file(
                    ingest_path, collection_name, embedder=pipeline._global_embedder
                )
                for warning in result.warnings:
                    click.echo(f"Warning: {warning}", err=True)
                if result.status == "error":
                    msg = result.error or "Ingest failed."
                    click.echo(f"Error: {msg}", err=True)
                    raise SystemExit(1)
                click.echo(f"Ingest complete: 1 ingested, 0 errors.")
            elif timings_enabled:
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
                _print_dir_summary(results)
            else:
                results = await pipeline.ingest_directory(ingest_path, collection_name, embedder=pipeline._global_embedder)
                _print_dir_summary(results)
        finally:
            await pipeline.store.disconnect()

    try:
        asyncio.run(_run())
    except SystemExit:
        raise
    except Exception as exc:
        click.echo(f"Error during ingest: {exc}", err=True)
        raise SystemExit(1)
