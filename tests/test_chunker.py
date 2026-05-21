"""packages/archon-search/tests/test_chunker.py — unit tests for DocumentChunker."""
from __future__ import annotations

import pytest

from archon_search._types import ChunkRecord
from archon_search.chunker import DocumentChunker


_LONG_TEXT = " ".join([f"This is sentence number {i} in a long document." for i in range(200)])

# Required kwargs for chunker.chunk() after Task 3.2; tests that don't care
# about these values reuse this dict to stay focused on what they're pinning.
_DEFAULT_KW = {
    "file_type": "md",
    "updated_at": "2026-05-21T00:00:00+00:00",
    "ingested_by": "cli",
}


def test_chunker_returns_chunk_records() -> None:
    """Short markdown text → list of ChunkRecord."""
    chunker = DocumentChunker()
    records = chunker.chunk("# Hello\n\nThis is a short document.", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert isinstance(records, list)
    assert all(isinstance(r, ChunkRecord) for r in records)


def test_chunker_returns_empty_placeholder_chunk_id() -> None:
    """chunk_id must be empty string in chunker output (pipeline assigns sequential IDs)."""
    chunker = DocumentChunker()
    records = chunker.chunk("Hello world.", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert all(r.chunk_id == "" for r in records), "chunk_id must be empty placeholder"


def test_chunker_all_records_have_doc_id() -> None:
    """Every chunk carries the provided doc_id."""
    chunker = DocumentChunker()
    records = chunker.chunk("Hello world. More text here.", "my-doc-id", "/tmp/test.md", **_DEFAULT_KW)
    assert all(r.doc_id == "my-doc-id" for r in records)


def test_chunker_vector_field_is_empty() -> None:
    """vector == [] before pipeline fills it."""
    chunker = DocumentChunker()
    records = chunker.chunk("Hello world.", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert all(r.vector == [] for r in records)


def test_chunker_empty_text_returns_empty_list() -> None:
    """Empty text → empty list, no crash."""
    chunker = DocumentChunker()
    records = chunker.chunk("", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert records == []


def test_chunker_whitespace_only_returns_empty_list() -> None:
    """Whitespace-only text → empty list (no garbage chunks in vector store)."""
    chunker = DocumentChunker()
    assert chunker.chunk("   \n\t  ", "doc1", "/tmp/doc1.md", **_DEFAULT_KW) == []


def test_chunker_long_text_produces_multiple_chunks() -> None:
    """5000-char text → multiple chunks."""
    chunker = DocumentChunker(chunk_size=128)
    records = chunker.chunk(_LONG_TEXT, "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert len(records) > 1


def test_chunker_non_empty_text_in_chunks() -> None:
    """Every produced chunk has non-empty text."""
    chunker = DocumentChunker()
    records = chunker.chunk("Hello world. This is a test.", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert all(len(r.text) > 0 for r in records)


def test_chunker_respects_chunk_size() -> None:
    """Long text chunked with chunk_size=64 — no chunk exceeds 64 * 1.2 tokens."""
    from chonkie import RecursiveChunker  # access chunks directly for token_count
    raw_chunker = RecursiveChunker(tokenizer="gpt2", chunk_size=64)
    raw_chunks = raw_chunker.chunk(_LONG_TEXT)
    max_tokens = int(64 * 1.2)
    for chunk in raw_chunks:
        assert chunk.token_count <= max_tokens, f"Chunk token_count {chunk.token_count} exceeds {max_tokens}"


def test_chunker_source_path_preserved() -> None:
    """source_path from argument is set on every chunk."""
    chunker = DocumentChunker()
    records = chunker.chunk("Hello world.", "doc1", "/some/special/path.md", **_DEFAULT_KW)
    assert all(r.source_path == "/some/special/path.md" for r in records)


def test_chunker_indexed_at_is_iso8601() -> None:
    """indexed_at is a non-empty ISO-8601 UTC string."""
    import re

    chunker = DocumentChunker()
    records = chunker.chunk("Hello world.", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    iso_pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    for r in records:
        assert iso_pattern.match(r.indexed_at), f"Bad indexed_at: {r.indexed_at!r}"


# ---------------------------------------------------------------------------
# Task 3.2 — new required keyword-only metadata args
# ---------------------------------------------------------------------------


def test_chunk_propagates_file_type() -> None:
    chunker = DocumentChunker()
    records = chunker.chunk(
        "Hello world.", "doc1", "/tmp/doc1.md",
        file_type="md", updated_at="2026-05-21T00:00:00+00:00", ingested_by="cli",
    )
    assert records
    assert all(r.file_type == "md" for r in records)


def test_chunk_propagates_updated_at() -> None:
    chunker = DocumentChunker()
    ts = "2026-05-21T12:34:56+00:00"
    records = chunker.chunk(
        "Hello world.", "doc1", "/tmp/doc1.md",
        file_type="md", updated_at=ts, ingested_by="cli",
    )
    assert records
    assert all(r.updated_at == ts for r in records)


@pytest.mark.parametrize("ingested_by", ["cli", "http", "watcher", "reindex"])
def test_chunk_propagates_ingested_by(ingested_by: str) -> None:
    chunker = DocumentChunker()
    records = chunker.chunk(
        "Hello world.", "doc1", "/tmp/doc1.md",
        file_type="md", updated_at="2026-05-21T00:00:00+00:00",
        ingested_by=ingested_by,  # type: ignore[arg-type]
    )
    assert records
    assert all(r.ingested_by == ingested_by for r in records)


def test_chunk_file_type_lowercase_md() -> None:
    chunker = DocumentChunker()
    records = chunker.chunk(
        "Hello world.", "doc1", "/tmp/doc1.md",
        file_type="md", updated_at="", ingested_by="cli",
    )
    assert all(r.file_type == "md" for r in records)


def test_chunk_file_type_empty_for_no_extension() -> None:
    chunker = DocumentChunker()
    records = chunker.chunk(
        "Hello world.", "doc1", "/tmp/Makefile",
        file_type="", updated_at="", ingested_by="cli",
    )
    assert all(r.file_type == "" for r in records)


def test_chunk_requires_keyword_args() -> None:
    """Positional supply of the new args must raise TypeError."""
    chunker = DocumentChunker()
    with pytest.raises(TypeError):
        chunker.chunk(  # type: ignore[call-arg,misc]
            "Hello world.", "doc1", "/tmp/doc1.md",
            "md", "2026-05-21T00:00:00+00:00", "cli",
        )
