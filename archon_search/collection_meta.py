"""CollectionMeta dataclass for FEAT-022 RAG routing."""
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
    doc_count: int = 0
    chunk_count: int = 0
    embedding_model: str = ""
    last_indexed: datetime | None = None
    last_described: datetime | None = None
    described_at_doc_count: int | None = None
    namespace: str = DEFAULT_NAMESPACE
