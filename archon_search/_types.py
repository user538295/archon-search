from dataclasses import dataclass, field
from typing import Literal

from archon_search.constants import DEFAULT_NAMESPACE

IngestedBy = Literal["cli", "http", "watcher", "reindex"]
"""Canonical call-site identity for ingest writes.

Four members only. The pre-A1 sentinel ``"archon-search-cli"`` is
normalized to ``"cli"`` at boundaries (header parser, read path) and is
intentionally *not* a member of this Literal — see
``archon_search.constants.LEGACY_INGESTED_BY``.
"""


@dataclass
class ChunkRecord:
    doc_id: str
    chunk_id: str
    text: str
    vector: list[float]
    source_path: str
    indexed_at: str
    # Extended metadata fields
    file_type: str = ""
    language: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    custom_score: float | None = None
    ingested_by: IngestedBy = "cli"
    updated_at: str = ""
    acl: list[str] | None = None


@dataclass
class SearchResult:
    doc_id: str
    chunk_id: str
    text: str
    score: float
    source_path: str
    acl: list[str] | None = None


@dataclass
class DocumentInfo:
    doc_id: str
    source_path: str
    chunk_count: int
    indexed_at: str


@dataclass
class CollectionInfo:
    name: str
    doc_count: int
    chunk_count: int
    namespace: str = DEFAULT_NAMESPACE


@dataclass
class IngestResult:
    doc_id: str
    chunks_created: int
    status: str
    error: str | None = None
