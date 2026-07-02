"""CollectionMeta dataclass for RAG routing."""
from __future__ import annotations

import dataclasses
from datetime import datetime

from archon_search.constants import DEFAULT_NAMESPACE


@dataclasses.dataclass
class CollectionMeta:
    """Metadata for a single RAG collection, including routing artifacts."""

    name: str
    description: str | None = None
    centroid: list[float] | None = None
    # B5 incremental maintenance — NOT persisted until Task 1.2 adds schema columns.
    centroid_sum: list[float] | None = None
    mutations_since_recompute: int = 0
    needs_recompute: bool = False
    doc_count: int = 0
    chunk_count: int = 0
    active_embedding_model: str = ""
    pending_embedding_model: str | None = None
    needs_reindex: bool = False
    reindex_job_id: str | None = None
    last_indexed: datetime | None = None
    last_described: datetime | None = None
    described_at_doc_count: int | None = None
    namespace: str = DEFAULT_NAMESPACE
    description_embedding: list[float] | None = None
    schema_version: int = 0
    default_ttl_seconds: int | None = None
    """E2a: optional default TTL in seconds for new chunks in this collection.
    None = no automatic expiry. Valid range when set: [1, 2_147_483_647].
    Read by the pipeline at ingest time (BE-3); set via PATCH route (BE-4)."""
