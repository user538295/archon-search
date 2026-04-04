"""Tests for archon.rag.progress — CollectionProgress and IndexingState dataclasses."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from archon.search.progress import (
    CollectionProgress,
    IndexingState,
    IndexingStateStore,
    IndexingStatus,
    from_dict,
    to_dict,
)
# compute_eta_seconds imported locally in TestComputeEtaSeconds to test it in isolation


class TestDataclasses:
    def test_indexing_status_is_str_enum(self) -> None:
        assert IndexingStatus.PENDING == "pending"
        assert IndexingStatus.IN_PROGRESS == "in_progress"
        assert IndexingStatus.DONE == "done"
        assert IndexingStatus.FAILED == "failed"
        assert isinstance(IndexingStatus.PENDING, str)

    def test_collection_progress_defaults(self) -> None:
        cp = CollectionProgress(status=IndexingStatus.PENDING)
        assert cp.total_files == 0
        assert cp.processed_files == 0
        assert cp.started_at is None
        assert cp.completed_at is None
        assert cp.error is None
        assert cp.error_count == 0

    def test_indexing_state_construction(self) -> None:
        cp = CollectionProgress(
            status=IndexingStatus.DONE,
            total_files=5,
            processed_files=5,
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:01:00Z",
        )
        state = IndexingState(
            collections={"my_col": cp},
            last_updated="2026-01-01T00:01:00Z",
        )
        assert "my_col" in state.collections
        assert state.collections["my_col"].status == IndexingStatus.DONE
        assert state.last_updated == "2026-01-01T00:01:00Z"

    def test_to_dict_serialization(self) -> None:
        cp = CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=10,
            processed_files=3,
            started_at="2026-01-01T00:00:00Z",
            error="some error",
            error_count=2,
        )
        state = IndexingState(
            collections={"col1": cp},
            last_updated="2026-01-01T00:05:00Z",
        )
        d = to_dict(state)
        assert d["last_updated"] == "2026-01-01T00:05:00Z"
        col = d["collections"]["col1"]
        assert col["status"] == "in_progress"  # serialized as string
        assert col["total_files"] == 10
        assert col["processed_files"] == 3
        assert col["started_at"] == "2026-01-01T00:00:00Z"
        assert col["completed_at"] is None
        assert col["error"] == "some error"
        assert col["error_count"] == 2
        assert col["processed_paths"] == []

    def test_from_dict_valid(self) -> None:
        d = {
            "last_updated": "2026-01-01T00:01:00Z",
            "collections": {
                "my_col": {
                    "status": "done",
                    "total_files": 7,
                    "processed_files": 7,
                    "started_at": "2026-01-01T00:00:00Z",
                    "completed_at": "2026-01-01T00:01:00Z",
                    "error": None,
                    "error_count": 0,
                }
            },
        }
        state = from_dict(d)
        assert state.last_updated == "2026-01-01T00:01:00Z"
        col = state.collections["my_col"]
        assert col.status == IndexingStatus.DONE
        assert col.total_files == 7
        assert col.processed_files == 7

    def test_from_dict_malformed(self) -> None:
        # Garbage input — must not raise, return empty state
        result = from_dict("not a dict")  # type: ignore[arg-type]
        assert isinstance(result, IndexingState)
        assert result.collections == {}

        result2 = from_dict(None)  # type: ignore[arg-type]
        assert isinstance(result2, IndexingState)
        assert result2.collections == {}

        result3 = from_dict({"collections": "bad"})
        assert isinstance(result3, IndexingState)
        assert result3.collections == {}

    def test_from_dict_missing_fields(self) -> None:
        # Only status provided — other fields get defaults
        d = {
            "last_updated": "2026-01-01T00:00:00Z",
            "collections": {
                "col": {"status": "pending"}
            },
        }
        state = from_dict(d)
        col = state.collections["col"]
        assert col.status == IndexingStatus.PENDING
        assert col.total_files == 0
        assert col.processed_files == 0
        assert col.started_at is None
        assert col.completed_at is None
        assert col.error is None
        assert col.error_count == 0

    def test_from_dict_unknown_status(self) -> None:
        d = {
            "last_updated": "2026-01-01T00:00:00Z",
            "collections": {
                "col": {"status": "nonexistent_status"}
            },
        }
        state = from_dict(d)
        assert state.collections["col"].status == IndexingStatus.PENDING

    def test_from_dict_partial_corruption(self) -> None:
        # One valid dict entry + one invalid (non-dict) entry — valid one preserved, invalid skipped
        d = {
            "collections": {
                "good": {"status": "done"},
                "bad": 42,
            }
        }
        state = from_dict(d)
        assert "good" in state.collections
        assert state.collections["good"].status == IndexingStatus.DONE
        assert "bad" not in state.collections

    def test_from_dict_non_int_fields(self) -> None:
        # null and string values for int fields should default to 0
        d = {
            "collections": {
                "col": {
                    "status": "pending",
                    "total_files": None,
                    "error_count": "bad",
                }
            }
        }
        state = from_dict(d)
        col = state.collections["col"]
        assert col.total_files == 0
        assert col.error_count == 0

    def test_from_dict_non_string_status(self) -> None:
        # Non-string status values (int, None) should default to PENDING
        for bad_status in (42, None):
            d = {"collections": {"col": {"status": bad_status}}}
            state = from_dict(d)
            assert state.collections["col"].status == IndexingStatus.PENDING

    def test_collection_progress_processed_paths_default(self) -> None:
        cp = CollectionProgress(status=IndexingStatus.PENDING)
        assert cp.processed_paths == []

    def test_to_dict_includes_processed_paths(self) -> None:
        cp = CollectionProgress(
            status=IndexingStatus.DONE,
            processed_paths=["/a/file.md", "/b/file.md"],
        )
        state = IndexingState(collections={"col": cp})
        d = to_dict(state)
        assert d["collections"]["col"]["processed_paths"] == ["/a/file.md", "/b/file.md"]

    def test_from_dict_parses_processed_paths(self) -> None:
        d = {
            "collections": {
                "col": {
                    "status": "done",
                    "processed_paths": ["/a/file.md", "/b/file.md"],
                }
            }
        }
        state = from_dict(d)
        assert state.collections["col"].processed_paths == ["/a/file.md", "/b/file.md"]

    def test_from_dict_invalid_processed_paths_type_falls_back(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "processed_paths": 42}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].processed_paths == []

    def test_from_dict_mixed_type_processed_paths_falls_back(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "processed_paths": ["/valid", 42, None]}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].processed_paths == []

    def test_from_dict_extra_fields_ignored(self) -> None:
        d = {
            "last_updated": "2026-01-01T00:00:00Z",
            "future_field": "something",
            "collections": {
                "col": {
                    "status": "done",
                    "unknown_new_field": 42,
                }
            },
        }
        state = from_dict(d)
        assert state.collections["col"].status == IndexingStatus.DONE
        assert not hasattr(state.collections["col"], "unknown_new_field")


class TestIndexingStateStore:
    def test_read_missing_file(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        result = store.read()
        assert result is None

    def test_read_corrupt_json(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        store = IndexingStateStore(tmp_path)
        store._state_file.write_text("not valid json")
        import logging
        with caplog.at_level(logging.WARNING, logger="archon"):
            result = store.read()
        assert result is None
        assert any(r.name == "archon" and r.levelno == logging.WARNING for r in caplog.records)

    def test_read_empty_file(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        store._state_file.write_text("")
        result = store.read()
        assert result is None

    def test_write_creates_dir_and_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "subdir" / "nested"
        store = IndexingStateStore(state_dir)
        state = IndexingState()
        store.write(state)
        assert state_dir.exists()
        assert store._state_file.exists()
        content = store._state_file.read_text()
        data = json.loads(content)  # should not raise
        assert "collections" in data
        assert "last_updated" in data

    def test_write_atomic_uses_tmp(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        state = IndexingState()
        tmp_file = store._state_file.with_suffix(".json.tmp")
        with patch("os.replace") as mock_replace:
            store.write(state)
            mock_replace.assert_called_once_with(tmp_file, store._state_file)

    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        cp = CollectionProgress(
            status=IndexingStatus.DONE,
            total_files=10,
            processed_files=10,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:01:00+00:00",
            error=None,
            error_count=0,
            processed_paths=["/a/file.md", "/b/file.md"],
        )
        state = IndexingState(
            collections={"my_col": cp},
            last_updated="2026-01-01T00:01:00+00:00",
        )
        store.write(state)
        result = store.read()
        assert result is not None
        assert result.last_updated == "2026-01-01T00:01:00+00:00"
        assert "my_col" in result.collections
        col = result.collections["my_col"]
        assert col.status == IndexingStatus.DONE
        assert col.total_files == 10
        assert col.processed_files == 10
        assert col.started_at == "2026-01-01T00:00:00+00:00"
        assert col.completed_at == "2026-01-01T00:01:00+00:00"
        assert col.error is None
        assert col.error_count == 0
        assert col.processed_paths == ["/a/file.md", "/b/file.md"]

    def test_update_collection_existing_state(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        cp_a = CollectionProgress(status=IndexingStatus.DONE, total_files=5, processed_files=5)
        cp_b = CollectionProgress(status=IndexingStatus.PENDING)
        initial = IndexingState(collections={"col_a": cp_a, "col_b": cp_b})
        store.write(initial)

        new_cp = CollectionProgress(status=IndexingStatus.IN_PROGRESS, total_files=10, processed_files=3)
        store.update_collection("col_b", new_cp)

        result = store.read()
        assert result is not None
        assert result.collections["col_a"].status == IndexingStatus.DONE
        assert result.collections["col_b"].status == IndexingStatus.IN_PROGRESS
        assert result.collections["col_b"].processed_files == 3

    def test_update_collection_empty_state(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        cp = CollectionProgress(status=IndexingStatus.PENDING, total_files=7)
        store.update_collection("new_col", cp)

        result = store.read()
        assert result is not None
        assert "new_col" in result.collections
        assert result.collections["new_col"].total_files == 7

    def test_remove_collection_present(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        state = IndexingState(collections={
            "col_a": CollectionProgress(status=IndexingStatus.DONE),
            "col_b": CollectionProgress(status=IndexingStatus.PENDING),
        })
        store.write(state)

        store.remove_collection("col_a")

        result = store.read()
        assert result is not None
        assert "col_a" not in result.collections
        assert "col_b" in result.collections

    def test_remove_collection_absent(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        state = IndexingState(collections={"col_a": CollectionProgress(status=IndexingStatus.DONE)})
        store.write(state)

        store.remove_collection("nonexistent")

        result = store.read()
        assert result is not None
        assert "col_a" in result.collections

    def test_remove_collection_no_state_file(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        # Should not raise — no-op
        store.remove_collection("col_a")

    def test_write_then_read_roundtrip_with_phase4_fields(self, tmp_path: Path) -> None:
        """New Phase 4 fields survive write() -> read() through actual disk I/O."""
        store = IndexingStateStore(tmp_path)
        cp = CollectionProgress(
            status=IndexingStatus.DONE,
            total_files=3,
            processed_files=3,
            file_mtimes={"/a/file.md": 1700000000.5, "/b/doc.txt": 1700000001.0},
            file_hashes={"/a/file.md": "abc123def456"},
            indexed_embedding_model="BAAI/bge-small-en-v1.5",
            indexed_chunk_size=512,
        )
        state = IndexingState(collections={"my_col": cp})
        store.write(state)
        loaded = store.read()
        loaded_cp = loaded.collections["my_col"]
        assert loaded_cp.file_mtimes == {"/a/file.md": 1700000000.5, "/b/doc.txt": 1700000001.0}
        assert loaded_cp.file_hashes == {"/a/file.md": "abc123def456"}
        assert loaded_cp.indexed_embedding_model == "BAAI/bge-small-en-v1.5"
        assert loaded_cp.indexed_chunk_size == 512

    def test_update_collection_roundtrip_with_phase4_fields(self, tmp_path: Path) -> None:
        """update_collection() preserves Phase 4 fields through actual disk I/O."""
        store = IndexingStateStore(tmp_path)
        cp = CollectionProgress(
            status=IndexingStatus.DONE,
            total_files=2,
            processed_files=2,
            file_mtimes={"/a/file.md": 1700000000.0},
            indexed_embedding_model="BAAI/bge-small-en-v1.5",
            indexed_chunk_size=256,
        )
        store.update_collection("col_a", cp)
        loaded = store.read()
        loaded_cp = loaded.collections["col_a"]
        assert loaded_cp.file_mtimes == {"/a/file.md": 1700000000.0}
        assert loaded_cp.indexed_embedding_model == "BAAI/bge-small-en-v1.5"
        assert loaded_cp.indexed_chunk_size == 256


class TestCollectionProgressNewFields:
    # --- Default values ---

    def test_collection_progress_file_mtimes_default(self) -> None:
        cp = CollectionProgress(status=IndexingStatus.PENDING)
        assert cp.file_mtimes == {}

    def test_collection_progress_file_hashes_default(self) -> None:
        cp = CollectionProgress(status=IndexingStatus.PENDING)
        assert cp.file_hashes == {}

    def test_collection_progress_indexed_embedding_model_default(self) -> None:
        cp = CollectionProgress(status=IndexingStatus.PENDING)
        assert cp.indexed_embedding_model == ""

    def test_collection_progress_indexed_chunk_size_default(self) -> None:
        cp = CollectionProgress(status=IndexingStatus.PENDING)
        assert cp.indexed_chunk_size == 0

    # --- to_dict includes new fields ---

    def test_to_dict_includes_file_mtimes(self) -> None:
        cp = CollectionProgress(
            status=IndexingStatus.DONE,
            file_mtimes={"/a/file.md": 1700000000.0},
        )
        state = IndexingState(collections={"col": cp})
        d = to_dict(state)
        assert d["collections"]["col"]["file_mtimes"] == {"/a/file.md": 1700000000.0}

    def test_to_dict_includes_file_hashes(self) -> None:
        cp = CollectionProgress(
            status=IndexingStatus.DONE,
            file_hashes={"/a/file.md": "abc123"},
        )
        state = IndexingState(collections={"col": cp})
        d = to_dict(state)
        assert d["collections"]["col"]["file_hashes"] == {"/a/file.md": "abc123"}

    def test_to_dict_includes_indexed_embedding_model(self) -> None:
        cp = CollectionProgress(
            status=IndexingStatus.DONE,
            indexed_embedding_model="BAAI/bge-small-en-v1.5",
        )
        state = IndexingState(collections={"col": cp})
        d = to_dict(state)
        assert d["collections"]["col"]["indexed_embedding_model"] == "BAAI/bge-small-en-v1.5"

    def test_to_dict_includes_indexed_chunk_size(self) -> None:
        cp = CollectionProgress(
            status=IndexingStatus.DONE,
            indexed_chunk_size=512,
        )
        state = IndexingState(collections={"col": cp})
        d = to_dict(state)
        assert d["collections"]["col"]["indexed_chunk_size"] == 512

    # --- from_dict round-trips ---

    def test_from_dict_parses_file_mtimes(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "file_mtimes": {"/a/file.md": 1.0}}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].file_mtimes == {"/a/file.md": 1.0}

    def test_from_dict_parses_file_hashes(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "file_hashes": {"/a/file.md": "abc123"}}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].file_hashes == {"/a/file.md": "abc123"}

    def test_from_dict_parses_indexed_embedding_model(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "indexed_embedding_model": "BAAI/bge-small-en-v1.5"}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].indexed_embedding_model == "BAAI/bge-small-en-v1.5"

    def test_from_dict_parses_indexed_chunk_size(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "indexed_chunk_size": 512}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].indexed_chunk_size == 512

    # --- Invalid type fallbacks ---

    def test_from_dict_invalid_file_mtimes_type(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "file_mtimes": "not_a_dict"}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].file_mtimes == {}

    def test_from_dict_invalid_file_hashes_type(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "file_hashes": 42}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].file_hashes == {}

    def test_from_dict_invalid_indexed_embedding_model_type(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "indexed_embedding_model": 42}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].indexed_embedding_model == ""

    def test_from_dict_invalid_indexed_chunk_size_type(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "indexed_chunk_size": "abc"}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].indexed_chunk_size == 0

    def test_from_dict_file_mtimes_with_non_float_values(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "file_mtimes": {"/a/file.md": "bad"}}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].file_mtimes == {}

    def test_from_dict_file_mtimes_with_int_values(self) -> None:
        # int mtime values must be accepted and converted to float
        d = {
            "collections": {
                "col": {"status": "done", "file_mtimes": {"/a/file.md": 1}}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].file_mtimes == {"/a/file.md": 1.0}
        assert isinstance(state.collections["col"].file_mtimes["/a/file.md"], float)

    def test_file_mtimes_default_not_shared_between_instances(self) -> None:
        cp1 = CollectionProgress(status=IndexingStatus.PENDING)
        cp2 = CollectionProgress(status=IndexingStatus.PENDING)
        cp1.file_mtimes["/a/file.md"] = 1.0
        assert cp2.file_mtimes == {}

    def test_file_hashes_default_not_shared_between_instances(self) -> None:
        cp1 = CollectionProgress(status=IndexingStatus.PENDING)
        cp2 = CollectionProgress(status=IndexingStatus.PENDING)
        cp1.file_hashes["/a/file.md"] = "abc"
        assert cp2.file_hashes == {}

    def test_from_dict_file_hashes_with_non_string_values(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "file_hashes": {"/a/file.md": 123}}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].file_hashes == {}

    def test_from_dict_file_mtimes_with_bool_values(self) -> None:
        d = {
            "collections": {
                "col": {"status": "done", "file_mtimes": {"/a/file.md": True}}
            }
        }
        state = from_dict(d)
        assert state.collections["col"].file_mtimes == {}


class TestTriggerField:
    def test_trigger_field_default(self) -> None:
        state = IndexingState()
        assert state.trigger is None

    def test_to_dict_includes_trigger_none(self) -> None:
        state = IndexingState()
        d = to_dict(state)
        assert "trigger" in d
        assert d["trigger"] is None

    def test_to_dict_includes_trigger_set(self) -> None:
        state = IndexingState()
        state.trigger = "install"
        d = to_dict(state)
        assert d["trigger"] == "install"

    def test_from_dict_reads_trigger(self) -> None:
        d = {"trigger": "install", "collections": {}}
        state = from_dict(d)
        assert state.trigger == "install"

    def test_from_dict_invalid_trigger_type_int(self) -> None:
        d = {"trigger": 42, "collections": {}}
        state = from_dict(d)
        assert state.trigger is None

    def test_from_dict_invalid_trigger_type_bool(self) -> None:
        # bool is subclass of int — isinstance(True, str) is False, so must map to None
        d = {"trigger": True, "collections": {}}
        state = from_dict(d)
        assert state.trigger is None

    def test_from_dict_invalid_trigger_type_list(self) -> None:
        d = {"trigger": [], "collections": {}}
        state = from_dict(d)
        assert state.trigger is None

    def test_from_dict_trigger_missing(self) -> None:
        d = {"collections": {}}
        state = from_dict(d)
        assert state.trigger is None

    def test_from_dict_trigger_empty_string(self) -> None:
        # Empty string is a valid str — must NOT be coerced to None
        d = {"trigger": "", "collections": {}}
        state = from_dict(d)
        assert state.trigger == ""


class TestComputeEtaSeconds:
    """Tests for compute_eta_seconds() pure function."""

    def _make_cp(self, **kwargs) -> "CollectionProgress":
        from archon.search.progress import CollectionProgress, IndexingStatus
        defaults = dict(
            status=IndexingStatus.IN_PROGRESS,
            total_files=100,
            processed_files=50,
            started_at=None,
        )
        defaults.update(kwargs)
        return CollectionProgress(**defaults)

    @pytest.mark.parametrize("status", [
        "done", "pending", "failed",
    ])
    def test_compute_eta_returns_none_when_not_in_progress(self, status: str) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        started = (now - timedelta(seconds=50)).isoformat()
        cp = self._make_cp(
            status=IndexingStatus(status),
            processed_files=50,
            started_at=started,
        )
        assert compute_eta_seconds(cp, now=now) is None

    def test_compute_eta_returns_none_when_too_few_files(self) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        started = (now - timedelta(seconds=50)).isoformat()
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=9,
            total_files=100,
            started_at=started,
        )
        assert compute_eta_seconds(cp, now=now) is None

    def test_compute_eta_returns_none_when_started_at_missing(self) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=50,
            total_files=100,
            started_at=None,
        )
        assert compute_eta_seconds(cp, now=now) is None

    def test_compute_eta_returns_none_when_elapsed_zero(self) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=50,
            total_files=100,
            started_at=now.isoformat(),
        )
        assert compute_eta_seconds(cp, now=now) is None

    def test_compute_eta_returns_none_when_nothing_remaining(self) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        started = (now - timedelta(seconds=50)).isoformat()
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=100,
            total_files=100,
            started_at=started,
        )
        assert compute_eta_seconds(cp, now=now) is None

    def test_compute_eta_basic_calculation(self) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        started = (now - timedelta(seconds=50)).isoformat()
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=50,
            total_files=100,
            started_at=started,
        )
        # fps = 50/50 = 1.0, remaining = 50, eta = int(50/1.0) = 50
        result = compute_eta_seconds(cp, now=now)
        assert result == 50
        assert isinstance(result, int)  # must be int, not float

    def test_compute_eta_accepts_custom_now(self) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        started = (now - timedelta(seconds=100)).isoformat()
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=20,
            total_files=100,
            started_at=started,
        )
        # fps = 20/100 = 0.2, remaining = 80, eta = int(80/0.2) = 400
        assert compute_eta_seconds(cp, now=now) == 400

    def test_compute_eta_returns_none_for_invalid_started_at(self) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=50,
            total_files=100,
            started_at="not-a-date",
        )
        assert compute_eta_seconds(cp, now=now) is None

    def test_compute_eta_returns_value_at_exact_threshold(self) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        started = (now - timedelta(seconds=10)).isoformat()
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=10,
            total_files=100,
            started_at=started,
        )
        # fps = 10/10 = 1.0, remaining = 90, eta = int(90/1.0) = 90; threshold is < 10, not <= 10
        assert compute_eta_seconds(cp, now=now) == 90

    def test_compute_eta_naive_started_at_treated_as_utc(self) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        # Naive ISO string — no timezone suffix
        started_naive = "2026-04-04T09:58:20"  # 100 seconds before now
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=20,
            total_files=100,
            started_at=started_naive,
        )
        # fps = 20/100 = 0.2, remaining = 80, eta = int(80/0.2) = 400
        assert compute_eta_seconds(cp, now=now) == 400

    def test_compute_eta_returns_none_when_elapsed_negative(self) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        # now is 5 seconds BEFORE started_at (clock skew)
        started = (now + timedelta(seconds=5)).isoformat()
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=50,
            total_files=100,
            started_at=started,
        )
        assert compute_eta_seconds(cp, now=now) is None

    def test_compute_eta_returns_none_when_total_files_zero(self) -> None:
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc)
        started = (now - timedelta(seconds=50)).isoformat()
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=10,
            total_files=0,
            started_at=started,
        )
        # processed (10) >= total (0) → nothing remaining
        assert compute_eta_seconds(cp, now=now) is None

    def test_compute_eta_naive_now_treated_as_utc(self) -> None:
        """Naive `now` kwarg should be treated as UTC (spec-mandated behavior)."""
        from archon.search.progress import IndexingStatus, compute_eta_seconds
        from datetime import datetime, timedelta, timezone
        # started_at is UTC-aware
        started_utc = datetime(2026, 4, 4, 9, 58, 20, tzinfo=timezone.utc)
        # now is naive (no tzinfo) — 100 seconds after started
        now_naive = datetime(2026, 4, 4, 10, 0, 0)  # no tzinfo
        cp = self._make_cp(
            status=IndexingStatus.IN_PROGRESS,
            processed_files=20,
            total_files=100,
            started_at=started_utc.isoformat(),
        )
        # fps = 20/100 = 0.2, remaining = 80, eta = int(80/0.2) = 400
        assert compute_eta_seconds(cp, now=now_naive) == 400


class TestSetTrigger:
    def test_set_trigger_creates_state(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        store.set_trigger("install")
        result = store.read()
        assert result is not None
        assert result.trigger == "install"
        assert result.collections == {}
        # Verify raw JSON serialization
        raw = json.loads(store._state_file.read_text())
        assert raw["trigger"] == "install"

    def test_set_trigger_updates_existing(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        cp = CollectionProgress(status=IndexingStatus.DONE, total_files=5)
        old_ts = "2020-01-01T00:00:00+00:00"
        state = IndexingState(collections={"col": cp}, last_updated=old_ts)
        store.write(state)
        store.set_trigger("manual")
        result = store.read()
        assert result is not None
        assert result.trigger == "manual"
        assert "col" in result.collections
        assert result.collections["col"].total_files == 5
        # set_trigger must update last_updated (like update_collection / remove_collection)
        assert result.last_updated != old_ts

    def test_set_trigger_clears_trigger(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        store.set_trigger("install")
        store.set_trigger(None)
        result = store.read()
        assert result is not None
        assert result.trigger is None
        # Verify JSON has null
        raw = json.loads(store._state_file.read_text())
        assert raw["trigger"] is None
