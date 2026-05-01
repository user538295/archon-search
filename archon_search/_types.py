from dataclasses import dataclass


@dataclass
class ChunkRecord:
    doc_id: str
    chunk_id: str
    text: str
    vector: list[float]
    source_path: str
    indexed_at: str


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
