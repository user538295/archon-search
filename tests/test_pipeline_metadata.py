"""Tests pinning that ``SearchPipeline.ingest_file`` derives metadata at the
call site and propagates ``ingested_by`` to every emitted chunk.

Implements Task 3.3 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
Task 3.1 (C3a): heading enrichment wiring in pipeline.
"""
from __future__ import annotations

import json
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
    is_warm: bool = False

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

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
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
    caplog.set_level(logging.DEBUG, logger="archon_search")

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
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

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
    assert result.status == "ok"

    row = await _read_first_row(connected_store, col_name, result.doc_id)
    assert row["file_type"] == "md"


@pytest.mark.asyncio
async def test_file_type_empty_for_no_extension(connected_store, col_name, tmp_path: Path) -> None:
    pipeline = _make_pipeline(connected_store)
    f = tmp_path / "Makefile"
    f.write_text("all:\n\techo hello\n" * 5)

    result = await pipeline.ingest_file(f, col_name, embedder=pipeline._global_embedder)
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

    result = await pipeline.ingest_file(md_file, col_name, ingested_by="watcher", embedder=pipeline._global_embedder)
    assert result.status == "ok"
    row = await _read_first_row(connected_store, col_name, result.doc_id)
    assert row["ingested_by"] == "watcher"


# ---------------------------------------------------------------------------
# Task 3.1 — C3a heading enrichment wiring
# ---------------------------------------------------------------------------


async def _collect_metadata_from_store(store, col: str, doc_id: str) -> list[dict]:
    """Return the parsed metadata dict for every chunk of *doc_id*."""
    db = store._require_connected()
    table = await db.open_table(col)
    rows = await table.query().where(f"doc_id = '{doc_id}'").to_list()
    assert rows, f"no rows for doc_id={doc_id!r}"
    results = []
    for row in rows:
        raw = row.get("metadata", "{}")
        results.append(json.loads(raw) if isinstance(raw, str) else raw)
    return results


@pytest.mark.asyncio
async def test_ingest_file_heading_metadata_populated(
    connected_store, col_name, tmp_path: Path
) -> None:
    """Ingesting a .md file with headings produces chunks with non-empty _heading / _section_path."""
    pipeline = _make_pipeline(connected_store)
    md_file = tmp_path / "doc.md"
    md_file.write_text(
        "# Installation\n\nInstall the package.\n\n"
        "## macOS\n\nUse Homebrew.\n\n"
        "## Linux\n\nUse apt.\n\n" * 2
    )

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
    assert result.status == "ok"

    all_meta = await _collect_metadata_from_store(connected_store, col_name, result.doc_id)
    # At least some chunks must have a non-empty _heading
    headings = [m.get("_heading", "") for m in all_meta]
    assert any(h != "" for h in headings), f"no heading metadata found; all_meta={all_meta}"
    # Every chunk must have both keys present
    for m in all_meta:
        assert "_heading" in m, f"_heading missing from metadata: {m}"
        assert "_section_path" in m, f"_section_path missing from metadata: {m}"


@pytest.mark.asyncio
async def test_ingest_file_no_heading_empty_strings(
    connected_store, col_name, tmp_path: Path
) -> None:
    """Ingesting a .md file with no headings produces _heading=='' and _section_path==''."""
    pipeline = _make_pipeline(connected_store)
    md_file = tmp_path / "plain.md"
    # Enough text to produce at least one chunk but no headings
    md_file.write_text("This is plain prose with no headings at all.\n" * 10)

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
    assert result.status == "ok"

    all_meta = await _collect_metadata_from_store(connected_store, col_name, result.doc_id)
    for m in all_meta:
        assert m.get("_heading") == "", f"expected empty _heading, got {m.get('_heading')!r}"
        assert m.get("_section_path") == "", f"expected empty _section_path, got {m.get('_section_path')!r}"


@pytest.mark.asyncio
async def test_ingest_file_binary_no_enrichment(
    connected_store, col_name, tmp_path: Path
) -> None:
    """Ingesting a non-front-matter file produces _heading=='' and _section_path=='' in metadata."""
    pipeline = _make_pipeline(connected_store)
    # .json is NOT in _FRONT_MATTER_EXTENSIONS, so heading_table will be []
    json_file = tmp_path / "data.json"
    # Write enough text to produce at least one chunk
    json_file.write_text('{"key": "value", "description": "' + ("text " * 50) + '"}\n')

    result = await pipeline.ingest_file(json_file, col_name, embedder=pipeline._global_embedder)
    assert result.status == "ok"

    all_meta = await _collect_metadata_from_store(connected_store, col_name, result.doc_id)
    for m in all_meta:
        assert m.get("_heading") == "", f"expected empty _heading for non-text file, got {m.get('_heading')!r}"
        assert m.get("_section_path") == "", f"expected empty _section_path for non-text file, got {m.get('_section_path')!r}"


@pytest.mark.asyncio
async def test_enrichment_survives_lancedb_roundtrip(
    connected_store, col_name, tmp_path: Path
) -> None:
    """_heading and _section_path survive json.dumps → LanceDB → parse_metadata round-trip."""
    pipeline = _make_pipeline(connected_store)
    md_file = tmp_path / "structured.md"
    md_file.write_text(
        "# Alpha\n\nIntro text.\n\n"
        "## Beta\n\nSection text.\n\n"
        "### Gamma\n\nDeep text.\n"
    )

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
    assert result.status == "ok"

    all_meta = await _collect_metadata_from_store(connected_store, col_name, result.doc_id)

    # Collect non-empty headings and section paths
    headings_found = {m["_heading"] for m in all_meta if m.get("_heading")}
    paths_found = {m["_section_path"] for m in all_meta if m.get("_section_path")}

    assert headings_found, f"no non-empty _heading after roundtrip; all_meta={all_meta}"

    # Expected headings from the document
    expected_headings = {"Alpha", "Beta", "Gamma"}
    assert headings_found <= expected_headings, (
        f"unexpected heading values: {headings_found - expected_headings}"
    )

    # Section paths must be present and non-empty for all non-empty heading chunks
    assert paths_found, f"no non-empty _section_path after roundtrip; all_meta={all_meta}"

    # Verify correct section path nesting — acceptance criterion from C3a spec:
    # chunks under H1/H2/H3 must carry the expected path string.
    # Build a lookup from heading → section_path for chunks that have a heading.
    heading_to_path = {m["_heading"]: m["_section_path"] for m in all_meta if m.get("_heading")}
    if "Alpha" in heading_to_path:
        assert heading_to_path["Alpha"] == "Alpha", (
            f"expected _section_path='Alpha' for H1 chunk, got {heading_to_path['Alpha']!r}"
        )
    if "Beta" in heading_to_path:
        assert heading_to_path["Beta"] == "Alpha > Beta", (
            f"expected _section_path='Alpha > Beta' for H2 chunk, got {heading_to_path['Beta']!r}"
        )
    if "Gamma" in heading_to_path:
        assert heading_to_path["Gamma"] == "Alpha > Beta > Gamma", (
            f"expected _section_path='Alpha > Beta > Gamma' for H3 chunk, got {heading_to_path['Gamma']!r}"
        )

    # Verify json round-trip consistency: re-serialize and re-parse metadata
    for m in all_meta:
        serialized = json.dumps(m)
        reparsed = json.loads(serialized)
        assert reparsed.get("_heading") == m.get("_heading"), "json roundtrip mutated _heading"
        assert reparsed.get("_section_path") == m.get("_section_path"), "json roundtrip mutated _section_path"
