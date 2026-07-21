import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
      ``indexed_at``, ``acl``, ``acl_source``, ``acl_sidecar_path``, ``acl_warning``.
    - **filterable** — A2 query-side filter dimensions:
      ``file_type``, ``language``, ``updated_at``, ``metadata``.
    - **ranking** — scoring inputs:
      ``custom_score`` (reserved; A1 schema-only).
    - **audit** — call-site identity for writes:
      ``ingested_by``.

    Attributes:
        acl_source: Provenance of the ACL rule — one of ``'frontmatter'``,
            ``'sidecar'``, or ``'collection_default'``; ``None`` for pre-G15
            chunks. Typed ``str`` (not ``Literal``) at entity level; enum
            enforced at wire layer (planned G15 BE-5 / ``AclGateSchema``).
        acl_sidecar_path: Relative path to the ``.acl`` sidecar file (relative
            to ``collection_root`` when available, else basename-only); ``None``
            when not sidecar-sourced.
        acl_warning: Structured warnings emitted during ACL loading (e.g.
            fail-open cases); empty list = no issues.
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
    expires_at: str | None = None
    """system: ISO 8601 UTC expiry timestamp; null = never expires (E2a)."""
    scopes: list[str] | None = None
    """system: scope tags for per-caller filtering; null = shared/global (E2a)."""
    start_offset: int = -1
    """transient: character offset of chunk start in the post-front-matter text. Not persisted to LanceDB."""
    end_offset: int = -1
    """transient: character offset of chunk end (exclusive) in the post-front-matter text. Not persisted to LanceDB."""
    acl_source: str | None = None
    """system: provenance of the ACL rule — 'frontmatter', 'sidecar', or 'collection_default'; null for pre-G15 chunks.
    Typed str (not Literal) at entity level — unlike IngestedBy — because AclGateSchema (planned G15 BE-5) enforces
    the enum at the wire boundary, and nullable utf8 persistence allows values outside the enum in pre-G15 rows."""
    acl_sidecar_path: str | None = None
    """system: relative path to the .acl sidecar file (relative to collection_root when available, else basename-only); null when not sidecar-sourced."""
    acl_warning: list[str] = field(default_factory=list)
    """system: structured warnings emitted during ACL loading (e.g. fail-open cases); empty list = no issues."""


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
    acl_source: str | None = None
    """Provenance of the ACL rule — 'frontmatter', 'sidecar', or 'collection_default'; null for pre-G15 chunks.
    Typed str (not Literal) at entity level; enum enforced at wire layer (planned G15 BE-5 / AclGateSchema)."""
    acl_sidecar_path: str | None = None
    """Relative path to the .acl sidecar file; null when not sidecar-sourced."""
    acl_warning: list[str] = field(default_factory=list)
    """Structured warnings emitted during ACL loading; empty list = no issues."""


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
    scopes: list[str] = field(default_factory=list)
    """system: set-union of scope tags across all chunks for this document."""


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
    warnings: list[str] = field(default_factory=list)
    code: Literal["file_too_large"] | None = None


class IngestError(Exception):
    """Raised (or used to construct messages) when an ingest pre-check rejects a file.

    Currently the only code is ``"file_too_large"``.  ``pipeline.ingest_file()``
    instantiates this to produce the human-readable message and returns an error
    ``IngestResult`` directly — it does *not* raise ``IngestError``.
    """

    code: Literal["file_too_large"] = "file_too_large"

    def __init__(self, *, file_size_mb: int, limit_mb: int) -> None:
        self.message = (
            f"File size {file_size_mb} MB exceeds the configured limit of {limit_mb} MB "
            f"(`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file."
        )
        super().__init__(self.message)


def _file_exceeds_limit(path: Path, max_file_mb: int) -> bool:
    """Return True if *path* is strictly larger than *max_file_mb* megabytes.

    ``max_file_mb == 0`` means no limit — always returns False.
    Follows symlinks (``os.path.getsize`` dereferences symlinks by design).
    Boundary: strictly greater-than (``size > limit``), so a file exactly at
    the limit is accepted.
    """
    if max_file_mb <= 0:
        return False
    size_bytes = os.path.getsize(path)
    limit_bytes = max_file_mb * 1024 * 1024
    return size_bytes > limit_bytes
