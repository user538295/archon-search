"""CollectionProgress and IndexingState dataclasses for RAG background indexing (FEAT-027)."""
from __future__ import annotations

import enum
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

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
    processed_paths: list[str] = field(default_factory=list)


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
                "processed_paths": cp.processed_paths,
            }
            for name, cp in state.collections.items()
        },
    }


class IndexingStateStore:
    """Persistent store for IndexingState, backed by a JSON file with atomic writes.

    Note: concurrent callers must provide external synchronization. This class is not
    thread-safe on its own — locks live at RagCollectionSync level, not here.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._state_file = state_dir / ".indexing_state.json"

    def read(self) -> IndexingState | None:
        """Read and deserialize state file. Returns None if missing, unreadable, or corrupt."""
        try:
            content = self._state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("IndexingStateStore: failed to read state file: %s", exc)
            return None
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning("IndexingStateStore: corrupt JSON in state file: %s", exc)
            return None
        except Exception as exc:
            logger.warning("IndexingStateStore: unexpected read error: %s", exc)
            return None
        return from_dict(data)

    def write(self, state: IndexingState) -> None:
        """Serialize and atomically write state to disk. Re-raises on failure."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_file.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(json.dumps(to_dict(state), indent=2), encoding="utf-8")
            os.replace(tmp_path, self._state_file)
        except Exception as exc:
            logger.error("IndexingStateStore: failed to write state file: %s", exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def update_collection(self, name: str, progress: CollectionProgress) -> None:
        """Update a single collection entry, creating state if absent."""
        state = self.read() or IndexingState()
        state.collections[name] = progress
        state.last_updated = datetime.now(UTC).isoformat()
        self.write(state)

    def remove_collection(self, name: str) -> None:
        """Remove a collection entry. No-op if missing or state file absent."""
        state = self.read()
        if state is None or name not in state.collections:
            return
        del state.collections[name]
        state.last_updated = datetime.now(UTC).isoformat()
        self.write(state)


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
            raw_paths = raw.get("processed_paths", [])
            if isinstance(raw_paths, list) and all(isinstance(p, str) for p in raw_paths):
                processed_paths = raw_paths
            else:
                processed_paths = []
            collections[name] = CollectionProgress(
                status=status,
                total_files=_safe_int(raw.get("total_files", 0)),
                processed_files=_safe_int(raw.get("processed_files", 0)),
                started_at=raw.get("started_at"),
                completed_at=raw.get("completed_at"),
                error=raw.get("error"),
                error_count=_safe_int(raw.get("error_count", 0)),
                processed_paths=processed_paths,
            )
        last_updated = data.get("last_updated", datetime.now(UTC).isoformat())
        return IndexingState(collections=collections, last_updated=last_updated)
    except Exception:
        logger.debug("from_dict: failed to parse IndexingState", exc_info=True)
        return IndexingState()
