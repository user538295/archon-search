"""BE-2: Entity tests for TTL + scoping fields.

Tests:
- test_chunk_record_default_expires_at_is_none
- test_chunk_record_default_scopes_is_none
- test_collection_meta_default_ttl_seconds_is_none
- test_collection_meta_explicit_ttl_seconds_roundtrips
- test_document_info_default_scopes_is_empty_list
"""
from archon_search._types import ChunkRecord, DocumentInfo
from archon_search.collection_meta import CollectionMeta


def _make_chunk_record() -> ChunkRecord:
    return ChunkRecord(
        doc_id="d" * 64,
        chunk_id="d" * 64 + "-000000",
        text="hello",
        vector=[0.1, 0.2],
        source_path="/tmp/x.txt",
        indexed_at="2024-01-01T00:00:00.000000Z",
    )


def test_chunk_record_default_expires_at_is_none():
    """ChunkRecord.expires_at defaults to None (no expiry)."""
    chunk = _make_chunk_record()
    assert chunk.expires_at is None


def test_chunk_record_default_scopes_is_none():
    """ChunkRecord.scopes defaults to None (shared/global)."""
    chunk = _make_chunk_record()
    assert chunk.scopes is None


def test_collection_meta_default_ttl_seconds_is_none():
    """CollectionMeta.default_ttl_seconds defaults to None (no collection-level TTL)."""
    meta = CollectionMeta(name="my-collection")
    assert meta.default_ttl_seconds is None


def test_collection_meta_explicit_ttl_seconds_roundtrips():
    """CollectionMeta.default_ttl_seconds holds an explicitly-set value unchanged."""
    meta = CollectionMeta(name="my-collection", default_ttl_seconds=3600)
    assert meta.default_ttl_seconds == 3600


def test_document_info_default_scopes_is_empty_list():
    """DocumentInfo.scopes defaults to [] (no scope tags)."""
    doc = DocumentInfo(
        doc_id="x",
        source_path="/p",
        chunk_count=1,
        indexed_at="2024-01-01T00:00:00.000000Z",
    )
    assert doc.scopes == []
