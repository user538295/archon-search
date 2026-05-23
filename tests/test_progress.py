"""packages/archon-search/tests/test_progress.py — CollectionProgress and IndexingState dataclasses."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from archon_search.progress import (
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


class TestIndexingStateStoreInit:
    def test_indexing_state_store_init_expands_tilde(self) -> None:
        store = IndexingStateStore(Path("~/.archon/search"))
        assert store._state_dir == Path.home() / ".archon/search"

    def test_indexing_state_store_init_absolute_path_unchanged(self) -> None:
        store = IndexingStateStore(Path("/tmp/state"))
        assert store._state_dir == Path("/tmp/state")


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
        from archon_search.progress import CollectionProgress, IndexingStatus
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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
        from archon_search.progress import IndexingStatus, compute_eta_seconds
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


class TestIndexingStateStoreEdgeCases:
    """Edge-case tests for IndexingStateStore and compute_eta_seconds."""

    # PermissionError reading state file → returns None, logs warning
    def test_read_permission_error_returns_none_and_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        store = IndexingStateStore(tmp_path)
        # read() uses Path.read_text — patch it to raise PermissionError (subclass of OSError)
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with caplog.at_level(logging.WARNING, logger="archon"):
                result = store.read()
        assert result is None
        assert any(r.levelno == logging.WARNING and r.name == "archon" for r in caplog.records)

    # state path is a directory → returns None, no crash
    def test_read_state_path_is_directory_returns_none(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        # Make the state file path be a directory instead of a file
        store._state_file.mkdir(parents=True, exist_ok=True)
        result = store.read()
        assert result is None

    # os.replace raises → .tmp unlinked; original exception re-raised
    def test_write_os_replace_raises_unlinks_tmp_and_reraises(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        state = IndexingState()
        tmp_file = store._state_file.with_suffix(".json.tmp")
        error = OSError("disk full")
        with patch("os.replace", side_effect=error):
            with pytest.raises(OSError, match="disk full"):
                store.write(state)
        # .tmp file must have been cleaned up
        assert not tmp_file.exists()

    # state absent → remove_collection doesn't crash, doesn't write
    def test_remove_collection_state_absent_does_not_write(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        # No state file exists
        assert not store._state_file.exists()
        store.remove_collection("nonexistent_col")
        # Still no state file — nothing was written
        assert not store._state_file.exists()

    # update collection A → collection B unchanged
    def test_update_collection_a_leaves_collection_b_unchanged(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        cp_a = CollectionProgress(status=IndexingStatus.PENDING, total_files=3)
        cp_b = CollectionProgress(status=IndexingStatus.DONE, total_files=7, processed_files=7)
        initial = IndexingState(collections={"col_a": cp_a, "col_b": cp_b})
        store.write(initial)

        updated_a = CollectionProgress(status=IndexingStatus.IN_PROGRESS, total_files=3, processed_files=1)
        store.update_collection("col_a", updated_a)

        result = store.read()
        assert result is not None
        assert result.collections["col_a"].status == IndexingStatus.IN_PROGRESS
        assert result.collections["col_b"].status == IndexingStatus.DONE
        assert result.collections["col_b"].total_files == 7
        assert result.collections["col_b"].processed_files == 7

    # processed_files > 0, elapsed=0 → returns None, no ZeroDivisionError
    def test_compute_eta_elapsed_zero_returns_none(self) -> None:
        from archon_search.progress import compute_eta_seconds
        from datetime import datetime, timezone
        now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
        # started_at == now → elapsed == 0
        cp = CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=100,
            processed_files=50,
            started_at=now.isoformat(),
        )
        result = compute_eta_seconds(cp, now=now)
        assert result is None

    # started_at with UTC+05:00 → ETA computed without crash
    def test_compute_eta_utc_plus_offset_no_crash(self) -> None:
        from archon_search.progress import compute_eta_seconds
        from datetime import datetime, timezone, timedelta
        tz_plus5 = timezone(timedelta(hours=5))
        now = datetime(2026, 5, 7, 17, 1, 40, tzinfo=tz_plus5)  # 12:01:40 UTC
        started = datetime(2026, 5, 7, 17, 0, 0, tzinfo=tz_plus5)  # 100s before now
        cp = CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=100,
            processed_files=20,
            started_at=started.isoformat(),
        )
        # elapsed=100s, fps=20/100=0.2, remaining=80, eta=int(80/0.2)=400
        result = compute_eta_seconds(cp, now=now)
        assert result == 400

    # file_mtimes: {"file.md": true} → boolean fails isinstance check; file_mtimes == {}
    def test_read_file_mtimes_boolean_value_falls_back_to_empty(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        raw_state = {
            "last_updated": "2026-05-07T00:00:00+00:00",
            "trigger": None,
            "collections": {
                "col": {
                    "status": "done",
                    "total_files": 1,
                    "processed_files": 1,
                    "file_mtimes": {"file.md": True},
                }
            },
        }
        store._state_dir.mkdir(parents=True, exist_ok=True)
        store._state_file.write_text(json.dumps(raw_state))
        result = store.read()
        assert result is not None
        assert result.collections["col"].file_mtimes == {}


class TestIndexingStateStoreThreadSafety:
    """Concurrency regression tests for the internal RLock (CON-3).

    The lost-update bug lives in the read-modify-write window of each composite
    method: reader A reads, reader B reads, A writes, B writes over A's update.
    The internal RLock closes that window by making the whole RMW atomic.

    To make the race manifest WITHOUT the lock (the "red" phase that proves these
    tests can detect a regression), we widen the no-lock RMW window by sleeping a
    few milliseconds immediately after each ``read()`` returns. Without the lock,
    that sleep guarantees every thread reads stale state before any thread writes,
    so the last writer clobbers the others (lost update) and the shared
    ``.json.tmp`` path can be torn apart.

    Note the injected sleep does NOT, by itself, "deterministically expose" the
    race for composite methods WITH the lock: under a correct lock the sleep runs
    while the lock is held, so it cannot interleave two RMW cycles — only one
    thread is ever inside the critical section. WITH the lock these tests instead
    assert correctness / serialization that must hold regardless of how threads
    happen to interleave. We do NOT place a ``Barrier`` inside the critical
    section: under a correct lock only one thread can be inside it, so a barrier
    there would deadlock by construction.
    """

    @staticmethod
    def _slow_read_patch(
        monkeypatch: pytest.MonkeyPatch, delay: float = 0.05
    ) -> None:
        """Widen every composite's read-modify-write window to force interleaving."""
        original_read = IndexingStateStore.read

        def slow_read(self: IndexingStateStore) -> IndexingState | None:
            result = original_read(self)
            time.sleep(delay)  # AFTER read returns, BEFORE the modify+write
            return result

        monkeypatch.setattr(IndexingStateStore, "read", slow_read)

    def test_concurrent_update_collection_no_lost_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = IndexingStateStore(tmp_path)
        n = 8
        self._slow_read_patch(monkeypatch)
        start = threading.Barrier(n)  # start all threads simultaneously

        def worker(idx: int) -> None:
            start.wait()
            store.update_collection(
                f"col_{idx}", CollectionProgress(status=IndexingStatus.DONE)
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()

        result = store.read()
        assert result is not None
        assert len(result.collections) == n
        for i in range(n):
            assert f"col_{i}" in result.collections

    def test_concurrent_writers_same_key_no_torn_or_mixed_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same-key writers: plain "last write wins" is the correct outcome with OR
        # without a lock, so asserting it is tautological and does NOT detect CON-3.
        # Instead each of N writers writes a fully self-consistent record whose
        # three int fields all carry the SAME writer id (v). The guarded invariant
        # is that the surviving record is never TORN (invalid JSON) nor MIXED (a
        # Frankenstein with fields from different writers). The lock makes each
        # whole read-modify-write atomic; without it, concurrent writes to the
        # shared ``.json.tmp`` path tear the record apart.
        n = 8
        store = IndexingStateStore(tmp_path)

        # Widen the no-lock write window deterministically. We split every tmp
        # write of the state JSON into two flushes and gather ALL writers at a
        # barrier in between, so they all resume and overwrite the SHARED tmp
        # mid-write together -> a torn/mixed record. The barrier can only be
        # satisfied when N writers are inside write() at once, which the lock
        # forbids: under the lock exactly one writer is ever in the critical
        # section, the barrier times out (BrokenBarrierError), and that writer
        # finishes a clean atomic write. We also widen read() so all writers read
        # stale state and reach the tmp write together.
        self._slow_read_patch(monkeypatch, delay=0.03)
        original_write_text = Path.write_text
        tear_barrier = threading.Barrier(n, timeout=0.5)

        def torn_write_text(self: Path, data: str, *args: object, **kwargs: object) -> int:
            if str(self).endswith(".json.tmp"):
                mid = len(data) // 2
                with open(self, "w", encoding="utf-8") as f:
                    f.write(data[:mid])
                    f.flush()
                    try:
                        tear_barrier.wait()  # all N writers gather (no-lock only)
                    except threading.BrokenBarrierError:
                        pass  # lock serialized us -> finish cleanly
                    f.write(data[mid:])
                return len(data)
            return original_write_text(self, data, *args, **kwargs)  # type: ignore[arg-type]

        # Tolerate the incidental shared-tmp collision so the TORN/MIXED record
        # (not an unrelated FileNotFoundError crash) is what the assertions catch.
        original_replace = os.replace

        def tolerant_replace(src: object, dst: object) -> None:
            try:
                original_replace(src, dst)  # type: ignore[arg-type]
            except FileNotFoundError:
                pass

        monkeypatch.setattr(Path, "write_text", torn_write_text)
        monkeypatch.setattr(os, "replace", tolerant_replace)
        start = threading.Barrier(n)

        def worker(v: int) -> None:
            start.wait()
            store.update_collection(
                "col",
                CollectionProgress(
                    status=IndexingStatus.DONE,
                    total_files=v,
                    processed_files=v,
                    error_count=v,
                ),
            )

        threads = [threading.Thread(target=worker, args=(v,)) for v in range(1, n + 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()

        # (a) file is valid JSON — not torn by interleaved writes
        raw = json.loads(store._state_file.read_text())
        result = store.read()
        assert result is not None
        assert list(result.collections.keys()) == ["col"]
        # (b) surviving record is internally consistent: all three fields equal the
        # SAME writer's id (1..n), never a mix like total=3 / error_count=7.
        col = result.collections["col"]
        fields = {col.total_files, col.processed_files, col.error_count}
        assert len(fields) == 1, f"mixed record: {fields}"
        assert col.total_files in range(1, n + 1)
        raw_col = raw["collections"]["col"]
        raw_fields = {raw_col["total_files"], raw_col["processed_files"], raw_col["error_count"]}
        assert len(raw_fields) == 1, f"mixed record on disk: {raw_fields}"

    def test_remove_collection_early_return_is_locked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guarded invariant is the lost-update window of remove_collection's
        # read-modify-write -- NOT merely the incidental shared-.json.tmp crash.
        # We race remove("k") against an update of a DIFFERENT key "m" (plus a
        # pre-seeded "survivor"). Under the lock the two RMW cycles serialize, so
        # BOTH effects land: "k" is gone AND "m" is present. Without the lock one
        # writer reads the other's stale snapshot and clobbers it on write, so
        # EITHER "k" is revived (the remove is lost) OR "m" is dropped (the update
        # is lost) -- either way the invariant below fails, and it fails on the
        # lost update itself even if the tmp path were ever made per-call-unique
        # (no crash). We assert no ordering: both effects must hold regardless of
        # which writer serialized first.
        store = IndexingStateStore(tmp_path)
        store.write(
            IndexingState(
                collections={
                    "k": CollectionProgress(status=IndexingStatus.DONE),
                    "survivor": CollectionProgress(status=IndexingStatus.DONE),
                }
            )
        )
        self._slow_read_patch(monkeypatch)
        start = threading.Barrier(2)

        def remover() -> None:
            start.wait()
            store.remove_collection("k")

        def updater() -> None:
            start.wait()
            store.update_collection("m", CollectionProgress(status=IndexingStatus.PENDING))

        threads = [threading.Thread(target=remover), threading.Thread(target=updater)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()

        raw = json.loads(store._state_file.read_text())  # valid JSON, no corruption
        result = store.read()
        assert result is not None
        # No update is lost: the remove ("k" absent) AND the unrelated update
        # ("m" present) both survive, and the pre-seeded "survivor" is untouched.
        assert "k" not in result.collections
        assert "m" in result.collections
        assert "survivor" in result.collections
        assert "k" not in raw["collections"]
        assert "m" in raw["collections"]
        assert "survivor" in raw["collections"]

    def test_exception_under_lock_releases_lock(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)

        def boom(_state: IndexingState) -> None:
            raise OSError("disk full")

        with patch.object(store, "write", side_effect=boom):
            with pytest.raises(OSError, match="disk full"):
                store.update_collection("col", CollectionProgress(status=IndexingStatus.DONE))

        # Lock must have been released by the `with` context manager.
        done = threading.Event()

        def call_set_trigger() -> None:
            store.set_trigger("x")
            done.set()

        t = threading.Thread(target=call_set_trigger)
        t.start()
        t.join(timeout=1)
        assert not t.is_alive()
        assert done.is_set()
        result = store.read()
        assert result is not None
        assert result.trigger == "x"

    def test_write_is_locked_independently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # write() must hold the lock for its whole body, so two concurrent
        # write() calls serialize: the second cannot start os.replace until the
        # first returns. To make the serialized ordering a genuine consequence of
        # the lock (and not scheduling luck), we coordinate with Events so writer 1
        # does NOT finish its os.replace until writer 2 has definitely entered
        # write() and is queued on the lock. (No barrier INSIDE the critical
        # section — that would deadlock against a correct lock.)
        store = IndexingStateStore(tmp_path)
        order: list[str] = []
        order_lock = threading.Lock()
        original_replace = os.replace
        w1_in_replace = threading.Event()
        w2_entered_write = threading.Event()

        def wrapped_replace(src: object, dst: object) -> None:
            with order_lock:
                order.append("enter")
            # Only writer 1 reaches here first: it started first and holds the
            # lock, so writer 2 is still blocked acquiring it. Hold the replace
            # open until writer 2 has entered write() and queued on the lock,
            # proving the resulting order is forced by the lock, not by timing.
            if not w1_in_replace.is_set():
                w1_in_replace.set()
                w2_entered_write.wait(timeout=5)
                time.sleep(0.02)  # let writer 2 actually block on the lock
            original_replace(src, dst)
            with order_lock:
                order.append("exit")

        monkeypatch.setattr(os, "replace", wrapped_replace)

        def writer1() -> None:
            store.write(
                IndexingState(
                    collections={"col": CollectionProgress(status=IndexingStatus.DONE, total_files=1)}
                )
            )

        def writer2() -> None:
            w1_in_replace.wait(timeout=5)  # writer 1 is inside its critical section first
            w2_entered_write.set()
            store.write(
                IndexingState(
                    collections={"col": CollectionProgress(status=IndexingStatus.DONE, total_files=2)}
                )
            )

        threads = [threading.Thread(target=writer1), threading.Thread(target=writer2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()

        # Serialized: each enter is followed by its own exit before the next enter.
        assert order == ["enter", "exit", "enter", "exit"]
        raw = json.loads(store._state_file.read_text())
        assert raw["collections"]["col"]["total_files"] in (1, 2)

    def test_set_trigger_under_concurrent_update_collection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guarded invariant is the lost-update window between set_trigger and
        # update_collection -- NOT merely the incidental shared-.json.tmp crash.
        # set_trigger writes the `trigger` field; update_collection writes the
        # `collections` dict. Both read-modify-write the same state, so under the
        # lock both fields survive. Without the lock one writer reads the other's
        # stale snapshot and clobbers its field on write: the most common
        # observable loss is `trigger` reverting to None (the updater wrote a
        # snapshot read before set_trigger ran). We pre-seed a distinct "survivor"
        # key so the assertion also proves no unrelated state is lost. This fails
        # on the lost update itself, independent of any tmp-collision crash.
        store = IndexingStateStore(tmp_path)
        store.write(
            IndexingState(
                collections={"survivor": CollectionProgress(status=IndexingStatus.DONE)}
            )
        )
        self._slow_read_patch(monkeypatch)
        start = threading.Barrier(2)

        def trigger_worker() -> None:
            start.wait()
            store.set_trigger("t")

        def update_worker() -> None:
            start.wait()
            store.update_collection("col", CollectionProgress(status=IndexingStatus.DONE))

        threads = [threading.Thread(target=trigger_worker), threading.Thread(target=update_worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()

        result = store.read()
        assert result is not None
        # No update is lost: the trigger set by one writer AND the collection
        # added by the other BOTH survive, and the pre-seeded survivor remains.
        assert result.trigger == "t"
        assert "col" in result.collections
        assert "survivor" in result.collections

    def test_rlock_reentry_does_not_deadlock(self, tmp_path: Path) -> None:
        # update_collection acquires the lock and then calls write(), which
        # re-acquires it — RLock re-entry must not deadlock. This is only the
        # interesting scenario while write() itself re-acquires the lock; a valid
        # future refactor that splits out an unlocked `_write_unlocked` helper for
        # composites would make re-entry impossible by construction, so skip
        # rather than fail in that case.
        if hasattr(IndexingStateStore, "_write_unlocked"):
            pytest.skip("write() refactored to a lock-free helper; RLock re-entry no longer occurs")
        store = IndexingStateStore(tmp_path)

        def worker() -> None:
            store.update_collection("col", CollectionProgress(status=IndexingStatus.DONE))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=2)
        assert not t.is_alive()
        result = store.read()
        assert result is not None
        assert "col" in result.collections

    def test_read_does_not_acquire_lock(self, tmp_path: Path) -> None:
        # read() is lock-free: even while a writer holds the lock for a long time,
        # concurrent read() calls return without blocking.
        store = IndexingStateStore(tmp_path)
        store.write(
            IndexingState(collections={"col": CollectionProgress(status=IndexingStatus.DONE)})
        )
        writer_holding = threading.Event()
        release_writer = threading.Event()
        original_write = IndexingStateStore.write

        def slow_write(self: IndexingStateStore, state: IndexingState) -> None:
            writer_holding.set()
            release_writer.wait(timeout=5)
            original_write(self, state)

        writer = threading.Thread(
            target=lambda: store.update_collection(
                "blocker", CollectionProgress(status=IndexingStatus.PENDING)
            )
        )
        with patch.object(IndexingStateStore, "write", slow_write):
            writer.start()
            assert writer_holding.wait(timeout=5)  # writer now holds the lock

            barrier = threading.Barrier(2)
            results: list[IndexingState | None] = []
            results_lock = threading.Lock()

            def reader() -> None:
                barrier.wait()
                res = store.read()
                with results_lock:
                    results.append(res)

            readers = [threading.Thread(target=reader) for _ in range(2)]
            for t in readers:
                t.start()
            for t in readers:
                t.join(timeout=2)
                assert not t.is_alive()  # readers did NOT block on the held lock

            release_writer.set()

        writer.join(timeout=5)
        assert not writer.is_alive()

        assert len(results) == 2
        assert all(r is not None for r in results)
