"""tests/rag/test_types.py — unit tests for shared RAG dataclasses."""
from __future__ import annotations

import pytest

from archon.search._types import (
    ChunkRecord,
    CollectionInfo,
    DocumentInfo,
    IngestResult,
    SearchResult,
)


def test_chunk_record_fields() -> None:
    """ChunkRecord instantiation and attribute access."""
    cr = ChunkRecord(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        vector=[0.1, 0.2],
        source_path="/tmp/doc.md",
        indexed_at="2026-01-01T00:00:00Z",
    )
    assert cr.doc_id == "abc"
    assert cr.chunk_id == "abc-000000"
    assert cr.text == "hello"
    assert cr.vector == [0.1, 0.2]
    assert cr.source_path == "/tmp/doc.md"
    assert cr.indexed_at == "2026-01-01T00:00:00Z"


def test_chunk_record_chunk_id_format() -> None:
    """chunk_id follows {doc_id}-{idx:06d} pattern."""
    doc_id = "a" * 64
    chunk_id = f"{doc_id}-{42:06d}"
    cr = ChunkRecord(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text="x",
        vector=[],
        source_path="f",
        indexed_at="2026-01-01T00:00:00Z",
    )
    assert cr.chunk_id == f"{'a' * 64}-000042"


def test_chunk_record_empty_vector_allowed() -> None:
    """vector defaults to empty before pipeline fills it."""
    cr = ChunkRecord(
        doc_id="x",
        chunk_id="",
        text="t",
        vector=[],
        source_path="p",
        indexed_at="2026-01-01T00:00:00Z",
    )
    assert cr.vector == []


def test_chunk_record_empty_chunk_id_allowed() -> None:
    """chunk_id = '' is valid in chunker output (pipeline assigns IDs later)."""
    cr = ChunkRecord(
        doc_id="d",
        chunk_id="",
        text="t",
        vector=[],
        source_path="p",
        indexed_at="2026-01-01T00:00:00Z",
    )
    assert cr.chunk_id == ""


def test_search_result_fields() -> None:
    sr = SearchResult(
        doc_id="d",
        chunk_id="d-000000",
        text="result",
        score=0.87,
        source_path="/path",
    )
    assert sr.score == pytest.approx(0.87)
    assert sr.source_path == "/path"


def test_document_info_fields() -> None:
    di = DocumentInfo(
        doc_id="d",
        source_path="/path/to/doc.md",
        chunk_count=5,
        indexed_at="2026-01-01T00:00:00Z",
    )
    assert di.chunk_count == 5


def test_collection_info_fields() -> None:
    ci = CollectionInfo(name="my-col", doc_count=2, chunk_count=10)
    assert ci.name == "my-col"
    assert ci.chunk_count == 10


def test_ingest_result_defaults() -> None:
    """error defaults to None."""
    ir = IngestResult(doc_id="d", chunks_created=3, status="ok")
    assert ir.error is None


def test_ingest_result_error_field() -> None:
    ir = IngestResult(doc_id="d", chunks_created=0, status="error", error="oops")
    assert ir.error == "oops"
    assert ir.status == "error"
