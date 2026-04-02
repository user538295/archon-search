"""CollectionProgress and IndexingState dataclasses for RAG background indexing (FEAT-027)."""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger("archon")


def _safe_int(val: object, default: int = 0) -> int:
    try:
        return int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class IndexingStatus(enum.StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


@dataclass
class CollectionProgress:
    status: IndexingStatus
    total_files: int = 0
    processed_files: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    error_count: int = 0


@dataclass
class IndexingState:
    collections: dict[str, CollectionProgress] = field(default_factory=dict)
    last_updated: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def to_dict(state: IndexingState) -> dict:
    """Serialize IndexingState to a JSON-compatible dict."""
    return {
        "last_updated": state.last_updated,
        "collections": {
            name: {
                "status": str(cp.status),
                "total_files": cp.total_files,
                "processed_files": cp.processed_files,
                "started_at": cp.started_at,
                "completed_at": cp.completed_at,
                "error": cp.error,
                "error_count": cp.error_count,
            }
            for name, cp in state.collections.items()
        },
    }


def from_dict(data: dict) -> IndexingState:
    """Deserialize IndexingState from a dict. Never raises — returns empty state on any error."""
    try:
        if not isinstance(data, dict):
            return IndexingState()
        collections_raw = data.get("collections", {})
        if not isinstance(collections_raw, dict):
            return IndexingState()
        collections: dict[str, CollectionProgress] = {}
        for name, raw in collections_raw.items():
            if not isinstance(raw, dict):
                continue
            status_str = raw.get("status", "pending")
            try:
                status = IndexingStatus(status_str)
            except (ValueError, TypeError):
                status = IndexingStatus.PENDING
            collections[name] = CollectionProgress(
                status=status,
                total_files=_safe_int(raw.get("total_files", 0)),
                processed_files=_safe_int(raw.get("processed_files", 0)),
                started_at=raw.get("started_at"),
                completed_at=raw.get("completed_at"),
                error=raw.get("error"),
                error_count=_safe_int(raw.get("error_count", 0)),
            )
        last_updated = data.get("last_updated", datetime.now(UTC).isoformat())
        return IndexingState(collections=collections, last_updated=last_updated)
    except Exception:
        logger.debug("from_dict: failed to parse IndexingState", exc_info=True)
        return IndexingState()
