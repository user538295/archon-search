from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from archon_search.constants import DEFAULT_NAMESPACE


def normalize_iso_utc(dt: datetime | str) -> str:
    """Return a fixed-width ISO-8601 UTC string: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``.

    Accepts:
    - ``datetime``: naive → treated as UTC; aware → converted to UTC.
    - ``str``: ISO-8601 forms accepted by ``datetime.fromisoformat``, including
      ``Z`` suffix, ``+00:00`` offset, or variable-precision microseconds.
    """
    if isinstance(dt, str):
        # Normalize 'Z' suffix to '+00:00' for consistent fromisoformat parsing.
        normalised = (dt.removesuffix("Z") + "+00:00") if dt.endswith("Z") else dt
        dt = datetime.fromisoformat(normalised)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

IngestedBy = Literal["cli", "http", "watcher", "reindex"]
"""Canonical call-site identity for ingest writes.

Four members only. The pre-A1 sentinel ``"archon-search-cli"`` is
normalized to ``"cli"`` at boundaries (header parser, read path) and is
intentionally *not* a member of this Literal — see
``archon_search.constants.LEGACY_INGESTED_BY``.
"""


@dataclass
class ChunkRecord:
    """One LanceDB chunk row.

    Field partitions (authoritative; the persistence doc points here):

    - **system** — identity, content, position, embedding, lifecycle:
      ``doc_id``, ``chunk_id``, ``text``, ``vector``, ``source_path``,
      ``indexed_at``, ``acl``.
    - **filterable** — A2 query-side filter dimensions:
      ``file_type``, ``language``, ``updated_at``, ``metadata``.
    - **ranking** — scoring inputs:
      ``custom_score`` (reserved; A1 schema-only).
    - **audit** — call-site identity for writes:
      ``ingested_by``.
    """

    doc_id: str
    """system: stable hash of the source path (64 hex chars)."""
    chunk_id: str
    """system: ``{doc_id}-{idx:06d}`` — order within the doc."""
    text: str
    """system: the chunked text body itself."""
    vector: list[float]
    """system: dense embedding (length = embedder.embedding_dim)."""
    source_path: str
    """system: absolute path on disk; used by reindex to refresh metadata."""
    indexed_at: str
    """system: ISO 8601 UTC timestamp set by the chunker at ingest time."""
    # Extended metadata fields
    file_type: str = ""
    """filterable: source file extension (lowercased, no leading dot)."""
    language: str = ""
    """filterable: detected language code; "" = untagged (pre-C2 legacy), "unknown" = below threshold."""
    metadata: dict[str, str] = field(default_factory=dict)
    """filterable: free-form key/value pairs from front matter (bounded)."""
    custom_score: float | None = None
    """ranking: reserved scoring input; A1 keeps it nullable + schema-only."""
    ingested_by: IngestedBy = "cli"
    """audit: which call site emitted this row (cli/http/watcher/reindex).
    Legacy ``"archon-search-cli"`` is normalized at boundaries."""
    updated_at: str = ""
    """filterable: file mtime (ISO 8601 UTC); falls back to indexed_at."""
    acl: list[str] | None = None
    """system: namespace tokens that must intersect a caller's tokens."""


@dataclass
class SearchResult:
    doc_id: str
    chunk_id: str
    text: str
    score: float
    source_path: str
    file_type: str = ""
    language: str = ""  # A2 addition (extractor lands in C2)
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: IngestedBy = "cli"
    metadata: dict[str, str] = field(default_factory=dict)
    acl: list[str] | None = None
    collection: str = ""


@dataclass
class ExcludedCollection:
    name: str
    reason: str


@dataclass
class FanoutTimings:
    leg_times: dict[str, float]
    rerank_time_ms: float


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
    needs_recompute: bool = False
