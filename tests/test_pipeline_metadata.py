"""Tests pinning that ``SearchPipeline.ingest_file`` derives metadata at the
call site and propagates ``ingested_by`` to every emitted chunk.

Implements Task 3.3 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from archon_search._types import ChunkRecord, IngestResult
from archon_search.embedder import Embedder
from archon_search.reranker import Reranker


# Reuse the mock backends pattern from test_pipeline.py (kept local to avoid
# cross-file fixture coupling).
class _MockEmbedderBackend:
    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]


class _MockRerankerBackend:
    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


def _make_pipeline(store):  # type: ignore[no-untyped-def]
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    return SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


async def _read_first_row(store, col: str, doc_id: str) -> dict:
    db = store._require_connected()
    table = await db.open_table(col)
    rows = await table.query().where(f"doc_id = '{doc_id}'").to_list()
    assert rows, f"no rows for doc_id={doc_id}"
    return rows[0]


@pytest.mark.asyncio
async def test_cli_ingest_sets_ingested_by_cli(connected_store, col_name, tmp_path: Path) -> None:
    pipeline = _make_pipeline(connected_store)
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Hello\n\nA short document with words to chunk.\n" * 3)

    result = await pipeline.ingest_file(md_file, col_name)
    assert result.status == "ok"

    row = await _read_first_row(connected_store, col_name, result.doc_id)
    assert row["ingested_by"] == "cli"
    assert row["file_type"] == "md"
    assert row["updated_at"] != ""
    # Parses cleanly as ISO 8601 UTC
    datetime.fromisoformat(row["updated_at"])
    assert row["updated_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_pipeline_falls_back_when_stat_fails(
    connected_store, col_name, tmp_path: Path, monkeypatch, caplog
) -> None:
    pipeline = _make_pipeline(connected_store)
    md_file = tmp_path / "doc.md"
    md_file.write_text("Some content to chunk.\n" * 5)

    real_stat = Path.stat

    def boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(self) == md_file:
            raise OSError("simulated stat failure")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", boom)
    caplog.set_level(logging.DEBUG, logger="archon")

    result = await pipeline.ingest_file(md_file, col_name)
    assert result.status == "ok"
    row = await _read_first_row(connected_store, col_name, result.doc_id)
    # Store-level fallback uses indexed_at when updated_at is empty.
    assert row["updated_at"] == row["indexed_at"]
    assert any("stat()" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_file_type_lowercased_MD(connected_store, col_name, tmp_path: Path) -> None:
    pipeline = _make_pipeline(connected_store)
    md_file = tmp_path / "FOO.MD"
    md_file.write_text("# Hello\n\nA short document.\n" * 3)

    result = await pipeline.ingest_file(md_file, col_name)
    assert result.status == "ok"

    row = await _read_first_row(connected_store, col_name, result.doc_id)
    assert row["file_type"] == "md"


@pytest.mark.asyncio
async def test_file_type_empty_for_no_extension(connected_store, col_name, tmp_path: Path) -> None:
    pipeline = _make_pipeline(connected_store)
    f = tmp_path / "Makefile"
    f.write_text("all:\n\techo hello\n" * 5)

    result = await pipeline.ingest_file(f, col_name)
    assert result.status == "ok"

    row = await _read_first_row(connected_store, col_name, result.doc_id)
    assert row["file_type"] == ""


@pytest.mark.asyncio
async def test_pipeline_ingest_propagates_ingested_by_kwarg(
    connected_store, col_name, tmp_path: Path
) -> None:
    pipeline = _make_pipeline(connected_store)
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Hello\n\nContent.\n" * 3)

    result = await pipeline.ingest_file(md_file, col_name, ingested_by="watcher")
    assert result.status == "ok"
    row = await _read_first_row(connected_store, col_name, result.doc_id)
    assert row["ingested_by"] == "watcher"
