"""CollectionMeta dataclass for RAG routing."""
from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Literal, Protocol, get_args

from archon_search.constants import DEFAULT_NAMESPACE

# C3: where a description came from, and the outcome of the last attempt to write one. ``pending`` is a memory-only
# overlay owned by the description lifecycle and applied at the API edge: the store never holds or reports it.
DescriptionSource = Literal["generated", "manual"]
DescriptionStoredStatus = Literal["ok", "failed"]
DESCRIPTION_SOURCE_GENERATED: DescriptionSource = "generated"
DESCRIPTION_SOURCE_MANUAL: DescriptionSource = "manual"
DESCRIPTION_STATUS_OK: DescriptionStoredStatus = "ok"
DESCRIPTION_STATUS_FAILED: DescriptionStoredStatus = "failed"
DESCRIPTION_SOURCES: tuple[str, ...] = get_args(DescriptionSource)
DESCRIPTION_STORED_STATUSES: tuple[str, ...] = get_args(DescriptionStoredStatus)

#: What a reader sees: the stored status with ``pending`` laid over it while a generation is in flight.
type DescriptionStatus = Literal["ok", "failed", "pending"]
DESCRIPTION_STATUS_PENDING: DescriptionStatus = "pending"


class DescriptionStatusOverlay(Protocol):
    """The reader side of the description lifecycle: ``pending`` laid over a stored status."""

    def overlay_status(self, collection: str, stored: DescriptionStoredStatus | None) -> DescriptionStatus | None: ...


def check_stored_description_value(column: str, value: str | None, allowed: tuple[str, ...]) -> None:
    """Refuse a *value* of *column* that the store cannot hold: ``pending`` is memory only, and anything outside
    the contract is refused rather than written. ``None`` (no value) is always storable."""
    if value is not None and value not in allowed:
        raise ValueError(f"{column} {value!r} cannot be stored; expected one of {allowed}")


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
    community_rebuild_job_id: str | None = None
    metadata_reindex_job_id: str | None = None
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
    description_source: DescriptionSource | None = None
    """C3: ``generated`` or ``manual``; None = no description. A legacy row with a description reads ``generated``."""
    description_status: DescriptionStoredStatus | None = None
    """C3: stored outcome of the last description attempt, ``ok`` or ``failed``; None = no description and no failed
    attempt. A legacy row with a description reads ``ok``. ``pending`` is never stored."""


def visible_description_status(meta: CollectionMeta, overlay: DescriptionStatusOverlay | None) -> DescriptionStatus | None:
    """The description status a reader sees for *meta*: the stored value, or ``pending`` while the lifecycle has a
    generation of the collection in flight. *overlay* is ``None`` while ``[description]`` is off, which reads the
    stored value as it is. Never writes: ``pending`` lives in memory only (C3)."""
    stored = meta.description_status
    return stored if overlay is None else overlay.overlay_status(meta.name, stored)
