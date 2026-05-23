from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    language: str | None = None
    """filterable: detected language (reserved; populated by C2)."""
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
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: IngestedBy = "cli"
    metadata: dict[str, str] = field(default_factory=dict)
    language: str | None = None  # A2 addition; extractor in C2
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


def normalize_iso_utc(dt: "datetime | str") -> str:
    """Return YYYY-MM-DDTHH:MM:SS.ffffffZ (6-digit microseconds, always Z UTC).

    Accepts datetime objects (naive treated as UTC; aware converted to UTC)
    and ISO-8601 strings (including +00:00, missing tz, variable-precision).
    """
    if isinstance(dt, str):
        from datetime import datetime as _dt  # noqa: PLC0415
        dt_str = dt.strip()
        # Remove trailing Z and replace with +00:00 for fromisoformat compatibility
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        try:
            parsed = _dt.fromisoformat(dt_str)
        except ValueError:
            # fallback: treat as UTC naive, strip tz suffix
            dt_str_clean = dt_str.split("+")[0].split("-")[0] if "T" not in dt_str else dt_str.replace("Z", "").split("+")[0]
            try:
                parsed = _dt.fromisoformat(dt_str_clean)
            except ValueError:
                parsed = _dt.fromisoformat(dt.strip().replace("Z", "").split("+")[0])
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    # datetime object
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
