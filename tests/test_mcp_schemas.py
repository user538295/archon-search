"""Tests for archon_search.server.mcp_schemas — MCP-specific Pydantic schemas."""
from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from archon_search._types import (
    ChunkRecord,
    SearchResult,
)


# ---------------------------------------------------------------------------
# Task 1.1 — Search response schemas
# ---------------------------------------------------------------------------


def test_mcp_schemas_has_future_annotations():
    """mcp_schemas.py must start with 'from __future__ import annotations'."""
    import archon_search.server.mcp_schemas as m

    src = inspect.getsource(m)
    assert src.startswith("from __future__ import annotations")


def test_mcp_search_result_schema_fields():
    from archon_search.server.mcp_schemas import McpSearchResultSchema

    expected = {
        "doc_id",
        "chunk_id",
        "text",
        "score",
        "source_path",
        "file_type",
        "language",
        "indexed_at",
        "updated_at",
        "ingested_by",
        "metadata",
        "acl",
        "collection",
    }
    assert set(McpSearchResultSchema.model_fields.keys()) == expected


def test_mcp_search_result_schema_from_result():
    from archon_search.server.mcp_schemas import McpSearchResultSchema

    r = SearchResult(
        doc_id="abc123",
        chunk_id="abc123-000000",
        text="hello world",
        score=0.95,
        source_path="/docs/foo.md",
        file_type="md",
        language="en",
        indexed_at="2024-01-01T00:00:00.000000Z",
        updated_at="2024-01-02T00:00:00.000000Z",
        ingested_by="cli",
        metadata={"key": "val"},
        acl=["admin"],
        collection="default",
    )
    schema = McpSearchResultSchema.from_result(r)
    assert schema.doc_id == "abc123"
    assert schema.chunk_id == "abc123-000000"
    assert schema.text == "hello world"
    assert schema.score == 0.95
    assert schema.source_path == "/docs/foo.md"
    assert schema.file_type == "md"
    assert schema.language == "en"
    assert schema.indexed_at == "2024-01-01T00:00:00.000000Z"
    assert schema.updated_at == "2024-01-02T00:00:00.000000Z"
    assert schema.ingested_by == "cli"
    assert schema.metadata == {"key": "val"}
    assert schema.acl == ["admin"]
    assert schema.collection == "default"


def test_mcp_search_result_schema_extra_forbid():
    from archon_search.server.mcp_schemas import McpSearchResultSchema

    with pytest.raises(ValidationError):
        McpSearchResultSchema.model_validate(
            {
                "doc_id": "x",
                "chunk_id": "x-000000",
                "text": "hi",
                "score": 1.0,
                "source_path": "/x",
                "surprise": "boom",
            }
        )


def test_mcp_search_response_fields():
    from archon_search.server.mcp_schemas import McpSearchResponse

    assert set(McpSearchResponse.model_fields.keys()) == {
        "results",
        "acl_filtered",
        "excluded_collections",
        "hyde_applied",
        "expansion_used",
        "expansion_warning",
        "graph_expansion_applied",
    }


def test_mcp_search_response_hyde_applied_defaults_false():
    from archon_search.server.mcp_schemas import McpSearchResponse

    response = McpSearchResponse(results=[], acl_filtered=False, excluded_collections=[])
    assert response.hyde_applied is False


def test_mcp_search_response_extra_forbid():
    from archon_search.server.mcp_schemas import McpSearchResponse

    with pytest.raises(ValidationError):
        McpSearchResponse.model_validate(
            {
                "results": [],
                "acl_filtered": False,
                "excluded_collections": [],
                "unexpected": 99,
            }
        )


def test_excluded_collection_schema_extra_forbid():
    from archon_search.server.mcp_schemas import ExcludedCollectionMcpSchema

    with pytest.raises(ValidationError):
        ExcludedCollectionMcpSchema.model_validate(
            {"name": "col", "reason": "acl", "extra_field": "bad"}
        )


# ---------------------------------------------------------------------------
# Task 1.2 — Context search schemas
# ---------------------------------------------------------------------------


def test_context_chunk_schema_fields():
    from archon_search.server.mcp_schemas import ContextChunkSchema

    expected = {
        "doc_id",
        "chunk_id",
        "text",
        "source_path",
        "indexed_at",
        "file_type",
        "language",
        "metadata",
        "ingested_by",
        "updated_at",
        "acl",
    }
    actual = set(ContextChunkSchema.model_fields.keys())
    assert actual == expected
    # Must NOT contain transient fields
    assert "vector" not in actual
    assert "start_offset" not in actual
    assert "end_offset" not in actual
    assert "custom_score" not in actual
    # Must contain language
    assert "language" in actual


def test_context_chunk_schema_from_result_excludes_transient():
    from archon_search.server.mcp_schemas import ContextChunkSchema

    chunk = ChunkRecord(
        doc_id="d1",
        chunk_id="d1-000000",
        text="some text",
        vector=[0.1, 0.2],
        source_path="/path/file.md",
        indexed_at="2024-01-01T00:00:00.000000Z",
        file_type="md",
        language="en",
        metadata={},
        custom_score=0.9,
        ingested_by="cli",
        updated_at="2024-01-01T00:00:00.000000Z",
        acl=None,
        start_offset=5,
        end_offset=10,
    )
    schema = ContextChunkSchema.from_result(chunk)
    assert not hasattr(schema, "vector")
    assert not hasattr(schema, "start_offset")
    assert not hasattr(schema, "end_offset")
    assert not hasattr(schema, "custom_score")
    assert schema.doc_id == "d1"
    assert schema.text == "some text"


def test_context_chunk_schema_extra_forbid():
    from archon_search.server.mcp_schemas import ContextChunkSchema

    with pytest.raises(ValidationError):
        ContextChunkSchema.model_validate(
            {
                "doc_id": "x",
                "chunk_id": "x-000000",
                "text": "t",
                "source_path": "/x",
                "indexed_at": "2024-01-01T00:00:00.000000Z",
                "bad_field": True,
            }
        )


def test_search_with_context_item_schema_fields():
    from archon_search.server.mcp_schemas import SearchWithContextItemSchema

    assert set(SearchWithContextItemSchema.model_fields.keys()) == {
        "result",
        "context_before",
        "context_after",
    }


def test_search_with_context_item_schema_extra_forbid():
    from archon_search.server.mcp_schemas import (
        ContextChunkSchema,
        McpSearchResultSchema,
        SearchWithContextItemSchema,
    )

    result = McpSearchResultSchema(
        doc_id="x",
        chunk_id="x-000000",
        text="t",
        score=1.0,
        source_path="/x",
    )
    with pytest.raises(ValidationError):
        SearchWithContextItemSchema.model_validate(
            {
                "result": result.model_dump(),
                "context_before": [],
                "context_after": [],
                "extra": "bad",
            }
        )


def test_search_with_context_response_fields():
    from archon_search.server.mcp_schemas import SearchWithContextResponse

    assert set(SearchWithContextResponse.model_fields.keys()) == {
        "results",
        "hyde_applied",
        "expansion_used",
        "expansion_warning",
    }


def test_search_with_context_response_extra_forbid():
    from archon_search.server.mcp_schemas import SearchWithContextResponse

    with pytest.raises(ValidationError):
        SearchWithContextResponse.model_validate(
            {"results": [], "hyde_applied": False, "extra_field": 1}
        )


# ---------------------------------------------------------------------------
# Task 1.3 — Collection meta schemas
# ---------------------------------------------------------------------------

COLLECTION_INTERNAL_FIELDS = {
    "centroid",
    "centroid_sum",
    "namespace",
    "needs_recompute",
    "needs_reindex",
    "reindex_job_id",
    "mutations_since_recompute",
    "described_at_doc_count",
    "active_embedding_model",
}

COLLECTION_PUBLIC_FIELDS = {
    "name",
    "description",
    "doc_count",
    "chunk_count",
    "last_indexed",
    "last_described",
    "embedding_model",
    "pending_embedding_model",
}


def _make_collection_meta(active_embedding_model: str = "model-x"):
    from archon_search.collection_meta import CollectionMeta

    return CollectionMeta(
        name="test-col",
        description="A test collection",
        active_embedding_model=active_embedding_model,
        centroid=[0.1, 0.2],
        centroid_sum=[0.3, 0.4],
        mutations_since_recompute=5,
        needs_recompute=True,
        needs_reindex=False,
        reindex_job_id=None,
        namespace="default",
        described_at_doc_count=10,
        doc_count=20,
        chunk_count=100,
        description_embedding=[0.1, 0.2],
    )


def test_collection_list_item_schema_fields():
    from archon_search.server.mcp_schemas import CollectionListItemSchema

    actual = set(CollectionListItemSchema.model_fields.keys())
    assert actual == COLLECTION_PUBLIC_FIELDS
    for f in COLLECTION_INTERNAL_FIELDS:
        assert f not in actual, f"Internal field {f!r} leaked into CollectionListItemSchema"


def test_collection_list_item_schema_from_result_field_mapping():
    from archon_search.server.mcp_schemas import CollectionListItemSchema

    meta = _make_collection_meta("model-x")
    schema = CollectionListItemSchema.from_result(meta)
    assert schema.embedding_model == "model-x"
    assert not hasattr(schema, "active_embedding_model")


def test_collection_list_item_schema_extra_forbid():
    from archon_search.server.mcp_schemas import CollectionListItemSchema

    with pytest.raises(ValidationError):
        CollectionListItemSchema.model_validate({"name": "col", "bad": 1})


def test_collection_detail_schema_fields():
    from archon_search.server.mcp_schemas import CollectionDetailSchema

    actual = set(CollectionDetailSchema.model_fields.keys())
    assert actual == COLLECTION_PUBLIC_FIELDS
    for f in COLLECTION_INTERNAL_FIELDS:
        assert f not in actual, f"Internal field {f!r} leaked into CollectionDetailSchema"


def test_collection_detail_schema_from_result():
    from archon_search.server.mcp_schemas import CollectionDetailSchema

    meta = _make_collection_meta("bert-base")
    schema = CollectionDetailSchema.from_result(meta)
    assert schema.embedding_model == "bert-base"
    assert schema.name == "test-col"


def test_collection_detail_schema_extra_forbid():
    from archon_search.server.mcp_schemas import CollectionDetailSchema

    with pytest.raises(ValidationError):
        CollectionDetailSchema.model_validate({"name": "col", "bad_field": "x"})


def test_collection_meta_mcp_schema_fields():
    from archon_search.server.mcp_schemas import CollectionMetaMcpSchema

    expected = COLLECTION_PUBLIC_FIELDS | {"description_embedding"}
    assert set(CollectionMetaMcpSchema.model_fields.keys()) == expected


def test_collection_meta_mcp_schema_description_embedding_excluded_by_default():
    from archon_search.server.mcp_schemas import CollectionMetaMcpSchema

    meta = _make_collection_meta()
    schema = CollectionMetaMcpSchema.from_result(meta, include_description_embedding=False)
    assert schema.description_embedding is None


def test_collection_meta_mcp_schema_description_embedding_included():
    from archon_search.server.mcp_schemas import CollectionMetaMcpSchema

    meta = _make_collection_meta()
    meta.description_embedding = [0.1, 0.2]
    schema = CollectionMetaMcpSchema.from_result(meta, include_description_embedding=True)
    assert schema.description_embedding == [0.1, 0.2]


def test_collection_meta_mcp_schema_extra_forbid():
    from archon_search.server.mcp_schemas import CollectionMetaMcpSchema

    with pytest.raises(ValidationError):
        CollectionMetaMcpSchema.model_validate({"name": "col", "bad": 1})


# ---------------------------------------------------------------------------
# Task 1.4 — Ingest, document, and delete schemas
# ---------------------------------------------------------------------------


def test_ingest_result_schema_fields():
    from archon_search.server.mcp_schemas import IngestResultSchema

    actual = set(IngestResultSchema.model_fields.keys())
    assert actual == {"doc_id", "chunks_created", "status", "error", "warnings", "code"}
    assert "needs_recompute" not in actual


def test_mcp_ingest_result_schema_includes_warnings():
    """IngestResultSchema.from_result() includes the warnings field."""
    from archon_search._types import IngestResult
    from archon_search.server.mcp_schemas import IngestResultSchema

    r = IngestResult(
        doc_id="d1",
        chunks_created=3,
        status="ok",
        error=None,
        needs_recompute=False,
        warnings=["ACL sidecar /tmp/doc.md.acl exceeds 64 KB limit (70000 bytes); ACL not applied"],
    )
    schema = IngestResultSchema.from_result(r)
    assert hasattr(schema, "warnings")
    assert schema.warnings == r.warnings
    assert len(schema.warnings) == 1
    assert "64 KB" in schema.warnings[0]


def test_mcp_ingest_result_schema_warnings_default_empty():
    """IngestResultSchema.from_result() with no warnings produces empty list."""
    from archon_search._types import IngestResult
    from archon_search.server.mcp_schemas import IngestResultSchema

    r = IngestResult(doc_id="d1", chunks_created=1, status="ok")
    schema = IngestResultSchema.from_result(r)
    assert schema.warnings == []


def test_ingest_result_schema_from_result_excludes_needs_recompute():
    from archon_search.server.mcp_schemas import IngestResultSchema
    from archon_search._types import IngestResult

    r = IngestResult(
        doc_id="d1",
        chunks_created=5,
        status="ok",
        error=None,
        needs_recompute=True,
    )
    schema = IngestResultSchema.from_result(r)
    assert not hasattr(schema, "needs_recompute")
    assert schema.doc_id == "d1"
    assert schema.chunks_created == 5
    assert schema.status == "ok"
    assert schema.error is None


def test_ingest_result_schema_extra_forbid():
    from archon_search.server.mcp_schemas import IngestResultSchema

    with pytest.raises(ValidationError):
        IngestResultSchema.model_validate(
            {"doc_id": "d1", "chunks_created": 1, "status": "ok", "bad": True}
        )


def test_document_info_schema_fields():
    from archon_search.server.mcp_schemas import DocumentInfoSchema

    assert set(DocumentInfoSchema.model_fields.keys()) == {
        "doc_id",
        "source_path",
        "chunk_count",
        "indexed_at",
    }


def test_document_info_schema_from_result():
    from archon_search.server.mcp_schemas import DocumentInfoSchema
    from archon_search._types import DocumentInfo

    doc = DocumentInfo(
        doc_id="d1",
        source_path="/path/file.md",
        chunk_count=3,
        indexed_at="2024-01-01T00:00:00.000000Z",
    )
    schema = DocumentInfoSchema.from_result(doc)
    assert schema.doc_id == "d1"
    assert schema.source_path == "/path/file.md"
    assert schema.chunk_count == 3
    assert schema.indexed_at == "2024-01-01T00:00:00.000000Z"


def test_document_info_schema_extra_forbid():
    from archon_search.server.mcp_schemas import DocumentInfoSchema

    with pytest.raises(ValidationError):
        DocumentInfoSchema.model_validate(
            {
                "doc_id": "d1",
                "source_path": "/x",
                "chunk_count": 1,
                "indexed_at": "2024-01-01T00:00:00.000000Z",
                "extra": "bad",
            }
        )


def test_delete_document_schema_fields():
    from archon_search.server.mcp_schemas import DeleteDocumentSchema

    assert set(DeleteDocumentSchema.model_fields.keys()) == {"deleted"}


def test_delete_document_schema_extra_forbid():
    from archon_search.server.mcp_schemas import DeleteDocumentSchema

    with pytest.raises(ValidationError):
        DeleteDocumentSchema.model_validate({"deleted": 1, "extra": "bad"})
