from __future__ import annotations

"""MCP-specific Pydantic schemas for all MCP tool return shapes.

Every MCP tool in mcp.py validates its return value through one of these
schemas before serializing to dict. Field exclusions and renames are
handled here via explicit ``from_result()`` classmethods, so that domain
dataclass changes fail loudly at the schema boundary rather than silently
drifting the MCP contract.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from archon_search._types import IngestErrorCode

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from archon_search._types import (
        ChunkRecord,
        DocumentInfo,
        IngestResult,
        SearchResult,
    )
    from archon_search.collection_meta import CollectionMeta


# ---------------------------------------------------------------------------
# Search response schemas
# ---------------------------------------------------------------------------


class ExcludedCollectionMcpSchema(BaseModel):
    """A collection excluded from a search result (ACL or routing)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    reason: str


class McpSearchResultSchema(BaseModel):
    """One search result item returned by the ``search`` and ``search_with_context`` tools."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    chunk_id: str
    text: str
    score: float
    source_path: str
    file_type: str = ""
    language: str = ""
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: str = "cli"
    metadata: dict[str, str] = {}
    acl: list[str] | None = None
    collection: str = ""

    @classmethod
    def from_result(cls, r: SearchResult) -> McpSearchResultSchema:
        return cls(
            doc_id=r.doc_id,
            chunk_id=r.chunk_id,
            text=r.text,
            score=r.score,
            source_path=r.source_path,
            file_type=r.file_type,
            language=r.language,
            indexed_at=r.indexed_at,
            updated_at=r.updated_at,
            ingested_by=r.ingested_by,
            metadata=r.metadata,
            acl=r.acl,
            collection=r.collection,
        )


class McpSearchResponse(BaseModel):
    """Top-level response for the ``search`` MCP tool."""

    model_config = ConfigDict(extra="forbid")

    results: list[McpSearchResultSchema]
    acl_filtered: bool
    excluded_collections: list[ExcludedCollectionMcpSchema]
    hyde_applied: bool = False
    expansion_used: bool = False
    expansion_warning: str | None = None
    graph_expansion_applied: bool = False
    ppr_entities_matched: int | None = None


# ---------------------------------------------------------------------------
# Context search schemas
# ---------------------------------------------------------------------------


class ContextChunkSchema(BaseModel):
    """One context chunk returned by ``search_with_context``.

    Transient fields (``vector``, ``start_offset``, ``end_offset``,
    ``custom_score``) are excluded.
    """

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    chunk_id: str
    text: str
    source_path: str
    indexed_at: str
    file_type: str = ""
    language: str = ""
    metadata: dict[str, str] = {}
    ingested_by: str = "cli"
    updated_at: str = ""
    acl: list[str] | None = None

    @classmethod
    def from_result(cls, chunk: ChunkRecord) -> ContextChunkSchema:
        return cls(
            doc_id=chunk.doc_id,
            chunk_id=chunk.chunk_id,
            text=chunk.text,
            source_path=chunk.source_path,
            indexed_at=chunk.indexed_at,
            file_type=chunk.file_type,
            language=chunk.language,
            metadata=chunk.metadata,
            ingested_by=chunk.ingested_by,
            updated_at=chunk.updated_at,
            acl=chunk.acl,
        )


class SearchWithContextItemSchema(BaseModel):
    """One item in the ``search_with_context`` results list."""

    model_config = ConfigDict(extra="forbid")

    result: McpSearchResultSchema
    context_before: list[ContextChunkSchema]
    context_after: list[ContextChunkSchema]


class SearchWithContextResponse(BaseModel):
    """Top-level response for the ``search_with_context`` MCP tool."""

    model_config = ConfigDict(extra="forbid")

    results: list[SearchWithContextItemSchema]
    hyde_applied: bool
    expansion_used: bool = False
    expansion_warning: str | None = None


# ---------------------------------------------------------------------------
# Collection meta schemas
# ---------------------------------------------------------------------------


class CollectionListItemSchema(BaseModel):
    """Public ``CollectionMeta`` fields for the ``list_collections`` tool.

    Internal fields (``centroid``, ``centroid_sum``, ``namespace``,
    ``needs_recompute``, ``needs_reindex``, ``reindex_job_id``,
    ``mutations_since_recompute``, ``described_at_doc_count``) and
    ``description_embedding`` are excluded.
    ``active_embedding_model`` is renamed to ``embedding_model``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    doc_count: int = 0
    chunk_count: int = 0
    last_indexed: datetime | None = None
    last_described: datetime | None = None
    embedding_model: str = ""
    pending_embedding_model: str | None = None

    @classmethod
    def from_result(cls, meta: CollectionMeta) -> CollectionListItemSchema:
        return cls(
            name=meta.name,
            description=meta.description,
            doc_count=meta.doc_count,
            chunk_count=meta.chunk_count,
            last_indexed=meta.last_indexed,
            last_described=meta.last_described,
            embedding_model=meta.active_embedding_model,
            pending_embedding_model=meta.pending_embedding_model,
        )


class CollectionDetailSchema(BaseModel):
    """Public ``CollectionMeta`` fields for ``get_collection_meta`` and ``update_collection`` tools.

    Same exclusions as ``CollectionListItemSchema``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    doc_count: int = 0
    chunk_count: int = 0
    last_indexed: datetime | None = None
    last_described: datetime | None = None
    embedding_model: str = ""
    pending_embedding_model: str | None = None

    @classmethod
    def from_result(cls, meta: CollectionMeta) -> CollectionDetailSchema:
        return cls(
            name=meta.name,
            description=meta.description,
            doc_count=meta.doc_count,
            chunk_count=meta.chunk_count,
            last_indexed=meta.last_indexed,
            last_described=meta.last_described,
            embedding_model=meta.active_embedding_model,
            pending_embedding_model=meta.pending_embedding_model,
        )


class CollectionMetaMcpSchema(BaseModel):
    """Public ``CollectionMeta`` fields for the ``get_collections_meta`` tool.

    Same as ``CollectionDetailSchema`` plus an optional ``description_embedding``
    field (exposed only when ``include_description_embedding=True``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    doc_count: int = 0
    chunk_count: int = 0
    last_indexed: datetime | None = None
    last_described: datetime | None = None
    embedding_model: str = ""
    pending_embedding_model: str | None = None
    description_embedding: list[float] | None = None

    @classmethod
    def from_result(
        cls,
        meta: CollectionMeta,
        *,
        include_description_embedding: bool = False,
    ) -> CollectionMetaMcpSchema:
        return cls(
            name=meta.name,
            description=meta.description,
            doc_count=meta.doc_count,
            chunk_count=meta.chunk_count,
            last_indexed=meta.last_indexed,
            last_described=meta.last_described,
            embedding_model=meta.active_embedding_model,
            pending_embedding_model=meta.pending_embedding_model,
            description_embedding=meta.description_embedding
            if include_description_embedding
            else None,
        )


# ---------------------------------------------------------------------------
# Ingest, document, and delete schemas
# ---------------------------------------------------------------------------


class IngestResultSchema(BaseModel):
    """``IngestResult`` with ``needs_recompute`` excluded."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    chunks_created: int
    status: str
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    code: IngestErrorCode | None = None

    @classmethod
    def from_result(cls, r: IngestResult) -> IngestResultSchema:
        return cls(
            doc_id=r.doc_id,
            chunks_created=r.chunks_created,
            status=r.status,
            error=r.error,
            warnings=r.warnings,
            code=r.code,
        )


class DocumentInfoSchema(BaseModel):
    """All public ``DocumentInfo`` fields."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    source_path: str
    chunk_count: int
    indexed_at: str
    scopes: list[str] = []

    @classmethod
    def from_result(cls, r: DocumentInfo) -> DocumentInfoSchema:
        return cls(
            doc_id=r.doc_id,
            source_path=r.source_path,
            chunk_count=r.chunk_count,
            indexed_at=r.indexed_at,
            scopes=r.scopes,
        )


class DeleteDocumentSchema(BaseModel):
    """Response shape for the ``delete_document`` MCP tool."""

    model_config = ConfigDict(extra="forbid")

    deleted: int
