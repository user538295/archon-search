from dataclasses import dataclass, field


@dataclass
class ChunkRecord:
    doc_id: str
    chunk_id: str
    text: str
    vector: list[float]
    source_path: str
    indexed_at: str
    # Extended metadata fields (FEAT-038 Task 6.1)
    file_type: str = ""
    language: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    custom_score: float | None = None
    ingested_by: str = "archon-search-cli"
    updated_at: str = ""


@dataclass
class SearchResult:
    doc_id: str
    chunk_id: str
    text: str
    score: float
    source_path: str


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


@dataclass
class IngestResult:
    doc_id: str
    chunks_created: int
    status: str
    error: str | None = None
