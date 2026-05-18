"""TDD tests for ACL field additions to ChunkRecord and SearchResult (Task 1.2)."""

from archon_search._types import ChunkRecord, SearchResult


def _make_chunk_record(**kwargs) -> ChunkRecord:
    defaults = {
        "doc_id": "doc1",
        "chunk_id": "chunk1",
        "text": "hello world",
        "vector": [0.1, 0.2],
        "source_path": "/tmp/file.md",
        "indexed_at": "2024-01-01T00:00:00",
    }
    defaults.update(kwargs)
    return ChunkRecord(**defaults)


def _make_search_result(**kwargs) -> SearchResult:
    defaults = {
        "doc_id": "doc1",
        "chunk_id": "chunk1",
        "text": "hello world",
        "score": 0.95,
        "source_path": "/tmp/file.md",
    }
    defaults.update(kwargs)
    return SearchResult(**defaults)


def test_chunk_record_default_acl_is_none() -> None:
    chunk = _make_chunk_record()
    assert chunk.acl is None


def test_search_result_default_acl_is_none() -> None:
    result = _make_search_result()
    assert result.acl is None


def test_chunk_record_acl_deny_all() -> None:
    chunk = _make_chunk_record(acl=[])
    assert chunk.acl == []
