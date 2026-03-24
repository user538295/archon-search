from archon.rag._types import (
    ChunkRecord,
    CollectionInfo,
    DocumentInfo,
    IngestResult,
    SearchResult,
)


def test_chunk_record_fields() -> None:
    record = ChunkRecord(
        doc_id="doc-1",
        chunk_id="doc-1-000000",
        text="hello world",
        vector=[0.1, 0.2, 0.3],
        source_path="/tmp/file.md",
        indexed_at="2026-03-24T00:00:00Z",
    )
    assert record.doc_id == "doc-1"
    assert record.chunk_id == "doc-1-000000"
    assert record.text == "hello world"
    assert record.vector == [0.1, 0.2, 0.3]
    assert record.source_path == "/tmp/file.md"
    assert record.indexed_at == "2026-03-24T00:00:00Z"


def test_chunk_record_chunk_id_format() -> None:
    doc_id = "doc-42"
    idx = 7
    chunk_id = f"{doc_id}-{idx:06d}"
    assert chunk_id == "doc-42-000007"


def test_ingest_result_defaults() -> None:
    result = IngestResult(doc_id="doc-1", chunks_created=3, status="ok")
    assert result.error is None


def test_search_result_fields() -> None:
    result = SearchResult(
        doc_id="doc-1",
        chunk_id="doc-1-000000",
        text="some text",
        score=0.95,
        source_path="/tmp/file.md",
    )
    assert result.doc_id == "doc-1"
    assert result.score == 0.95


def test_document_info_fields() -> None:
    info = DocumentInfo(
        doc_id="doc-1",
        source_path="/tmp/file.md",
        chunk_count=5,
        indexed_at="2026-03-24T00:00:00Z",
    )
    assert info.chunk_count == 5


def test_collection_info_fields() -> None:
    info = CollectionInfo(name="default", doc_count=10, chunk_count=100)
    assert info.name == "default"
    assert info.chunk_count == 100
