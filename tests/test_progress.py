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


class _ThreadSafetyHarness:
    """Shared concurrency test scaffolding (helpers + tuning constants).

    Deliberately NOT prefixed with ``Test`` so pytest does not collect it as a
    test class. Both ``TestIndexingStateStoreThreadSafety`` and
    ``TestResetInProgressThreadSafety`` inherit from this mixin to reuse the
    ``_slow_read_patch`` / ``_atomic_write_patch`` helpers and the load-bearing
    timing/sizing constants — without either class re-running the other's tests.
    """

    # --- Load-bearing timing/sizing constants (named so intent is explicit) ---
    # Delay injected after each read() to widen the no-lock RMW window enough that
    # all racing threads read stale state before any writes back.
    _RMW_DELAY = 0.05
    # Small safety margin to let a peer thread settle onto the lock queue once it
    # has signalled it is about to acquire (ordering itself is forced by Events).
    _LOCK_SETTLE = 0.02
    # Number of concurrent writers in the multi-writer races.
    _N_WRITERS = 8
    # Thread.join safety net so a hung worker fails fast instead of blocking the
    # suite forever. Generous relative to the sub-second work each worker does.
    _JOIN_TIMEOUT = 10.0

    @classmethod
    def _slow_read_patch(cls, monkeypatch: pytest.MonkeyPatch) -> None:
        """Widen every composite's read-modify-write window to force interleaving."""
        original_read = IndexingStateStore.read

        def slow_read(self: IndexingStateStore) -> IndexingState | None:
            result = original_read(self)
            time.sleep(cls._RMW_DELAY)  # AFTER read returns, BEFORE the modify+write
            return result

        monkeypatch.setattr(IndexingStateStore, "read", slow_read)

    @classmethod
    def _atomic_write_patch(cls, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serialize only the PHYSICAL write (tmp write + os.replace) so the final
        on-disk file is always valid JSON — never a torn shared ``.json.tmp``.

        This isolates the LOGICAL lost-update (stale RMW snapshot clobbered by the
        last writer) from the incidental tmp tear, so the lost-update tests go red
        on their intended lost-update assertion rather than on a torn file that
        makes ``read()`` return None. The composite RMW window stays unguarded —
        only the inner write() body is atomic via this test-local lock.
        """
        original_write = IndexingStateStore.write
        write_lock = threading.Lock()

        def atomic_write(self: IndexingStateStore, state: IndexingState) -> None:
            with write_lock:
                original_write(self, state)

        monkeypatch.setattr(IndexingStateStore, "write", atomic_write)


class TestIndexingStateStoreThreadSafety(_ThreadSafetyHarness):
    """Concurrency regression tests for the internal RLock (CON-3).

    The lost-update bug lives in the read-modify-write (RMW) window of each
    composite method: reader A reads, reader B reads, A writes, B writes over A's
    update. The internal RLock closes that window by making the whole RMW atomic.

    To make the race manifest WITHOUT the lock (the "red" phase that proves these
    tests can detect a regression), we widen the no-lock RMW window by sleeping a
    few milliseconds immediately after each ``read()`` returns. Without the lock,
    that sleep guarantees every thread reads stale state before any thread writes,
    so the last writer clobbers the others (lost update). WITH the lock the sleep
    runs while the lock is held, so it cannot interleave two RMW cycles — only one
    thread is ever inside the critical section.

    Shared rationale for the lost-update tests below
    (``remove_collection`` / ``set_trigger`` races): the guarded invariant is the
    lost-update window of the RMW, NOT the incidental shared-``.json.tmp`` tear.
    To keep the tear from pre-empting the intended assertion, these tests also
    make the inner ``write()`` body atomic with a test-local lock
    (``_atomic_write_patch``) — the final file is therefore always valid JSON, so
    they read back via ``store.read()`` and go red on the lost update itself, not
    on a torn file that would make ``read()`` return None. We do NOT place a
    ``Barrier`` inside the critical section: under a correct lock only one thread
    can be inside it, so a barrier there would deadlock by construction. Where
    ordering must be forced deterministically (without timing luck), we signal an
    Event BEFORE acquiring the lock so a peer can wait on it without deadlock.

    The shared helpers (``_slow_read_patch`` / ``_atomic_write_patch``) and tuning
    constants live on ``_ThreadSafetyHarness`` so they can be reused without
    re-running these tests under a subclass.
    """

    def test_concurrent_update_collection_no_lost_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = IndexingStateStore(tmp_path)
        n = self._N_WRITERS
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
            t.join(timeout=self._JOIN_TIMEOUT)
            assert not t.is_alive()

        result = store.read()
        assert result is not None
        assert len(result.collections) == n
        for i in range(n):
            assert f"col_{i}" in result.collections

    def test_concurrent_writers_same_key_serialize_to_consistent_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two writers update the SAME key with self-consistent records (all three
        # int fields carry the same writer id). The lock must serialize their RMW
        # cycles so the survivor is a clean, internally consistent record from ONE
        # writer — never a MIXED Frankenstein (total from one, error_count from
        # another) and never TORN (invalid JSON). We force the ordering
        # deterministically with the signal-BEFORE-lock pattern (no wall-clock
        # timeout, no deadlock risk): writer 2 sets ``w2_entered`` right before it
        # calls update_collection (before it can touch the lock), and writer 1 —
        # already inside its os.replace, holding the lock — waits on that signal.
        # WITH the lock the os.replace calls serialize (enter/exit/enter/exit) and
        # the survivor is consistent; WITHOUT the lock writer 2's RMW overlaps
        # writer 1's (enter, enter, ...) and the records interleave.
        store = IndexingStateStore(tmp_path)
        order: list[str] = []
        order_lock = threading.Lock()
        original_replace = os.replace
        w1_in_replace = threading.Event()
        w2_entered = threading.Event()

        def wrapped_replace(src: object, dst: object) -> None:
            with order_lock:
                order.append("enter")
            # Writer 1 reaches here first (it started + acquired the lock first).
            # Hold its replace open until writer 2 has entered update_collection
            # and queued on the lock, so the ordering is forced by the lock.
            if not w1_in_replace.is_set():
                w1_in_replace.set()
                w2_entered.wait(timeout=5)
                time.sleep(self._LOCK_SETTLE)
            original_replace(src, dst)  # type: ignore[arg-type]
            with order_lock:
                order.append("exit")

        monkeypatch.setattr(os, "replace", wrapped_replace)

        def writer1() -> None:
            store.update_collection(
                "col",
                CollectionProgress(
                    status=IndexingStatus.DONE,
                    total_files=1,
                    processed_files=1,
                    error_count=1,
                ),
            )

        def writer2() -> None:
            w1_in_replace.wait(timeout=5)  # writer 1 entered its replace first
            w2_entered.set()  # signal BEFORE touching the lock — no deadlock
            store.update_collection(
                "col",
                CollectionProgress(
                    status=IndexingStatus.DONE,
                    total_files=2,
                    processed_files=2,
                    error_count=2,
                ),
            )

        threads = [threading.Thread(target=writer1), threading.Thread(target=writer2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=self._JOIN_TIMEOUT)
            assert not t.is_alive()

        # Serialized: each enter is followed by its own exit before the next enter.
        assert order == ["enter", "exit", "enter", "exit"]
        # Valid JSON is the WITH-lock guarantee (no torn shared tmp write).
        raw = json.loads(store._state_file.read_text())
        result = store.read()
        assert result is not None
        assert list(result.collections.keys()) == ["col"]
        # Survivor is one writer's consistent record, never a mix.
        col = result.collections["col"]
        fields = {col.total_files, col.processed_files, col.error_count}
        assert len(fields) == 1, f"mixed record: {fields}"
        assert col.total_files in (1, 2)
        raw_col = raw["collections"]["col"]
        raw_fields = {raw_col["total_files"], raw_col["processed_files"], raw_col["error_count"]}
        assert len(raw_fields) == 1, f"mixed record on disk: {raw_fields}"

    def test_remove_collection_early_return_is_locked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Race remove("k") against an update of a DIFFERENT key "m" (plus a
        # pre-seeded "survivor"). Under the lock both RMW cycles serialize, so
        # BOTH effects land; without it, the last writer clobbers the other's
        # stale snapshot, dropping one effect. See the class docstring for the
        # shared lost-update rationale.
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
        self._atomic_write_patch(monkeypatch)  # final file always valid JSON
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
            t.join(timeout=self._JOIN_TIMEOUT)
            assert not t.is_alive()

        result = store.read()
        assert result is not None
        # No update is lost: the remove ("k" absent) AND the unrelated update
        # ("m" present) both survive, and the pre-seeded "survivor" is untouched.
        # This is the intended lost-update discriminator (the physical write is
        # made atomic above, so a torn file can never pre-empt these asserts).
        assert "k" not in result.collections
        assert "m" in result.collections
        assert "survivor" in result.collections

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
        # first returns. We force the ordering with Events (signal-before-lock):
        # writer 1 holds its os.replace open until writer 2 has entered write()
        # and queued on the lock, so the order is a consequence of the lock, not
        # scheduling luck. (No barrier INSIDE the critical section — that would
        # deadlock against a correct lock.)
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
                time.sleep(self._LOCK_SETTLE)  # small safety margin
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
            t.join(timeout=self._JOIN_TIMEOUT)
            assert not t.is_alive()

        # Serialized: each enter is followed by its own exit before the next enter.
        assert order == ["enter", "exit", "enter", "exit"]
        raw = json.loads(store._state_file.read_text())
        assert raw["collections"]["col"]["total_files"] in (1, 2)

    def test_set_trigger_under_concurrent_update_collection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Race set_trigger (writes the `trigger` field) against update_collection
        # (writes the `collections` dict). Under the lock both fields survive;
        # without it one writer clobbers the other (commonly `trigger` reverts to
        # None). A pre-seeded "survivor" key proves no unrelated state is lost.
        # See the class docstring for the shared lost-update rationale.
        store = IndexingStateStore(tmp_path)
        store.write(
            IndexingState(
                collections={"survivor": CollectionProgress(status=IndexingStatus.DONE)}
            )
        )
        self._slow_read_patch(monkeypatch)
        self._atomic_write_patch(monkeypatch)  # final file always valid JSON
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
            t.join(timeout=self._JOIN_TIMEOUT)
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


class TestResetInProgress:
    """Tests for IndexingStateStore.reset_in_progress(predicate) — locked RMW."""

    def test_reset_in_progress_resets_matching_entries(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        in_prog = CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=10,
            processed_files=4,
            started_at="2026-01-01T00:00:00+00:00",
            error="boom",
            error_count=3,
            processed_paths=["/a/file.md"],
            indexed_embedding_model="BAAI/bge-small-en-v1.5",
            indexed_chunk_size=512,
        )
        done = CollectionProgress(
            status=IndexingStatus.DONE,
            total_files=5,
            processed_files=5,
            completed_at="2026-01-01T00:05:00+00:00",
        )
        old_last_updated = "2020-01-01T00:00:00+00:00"
        store.write(
            IndexingState(
                collections={"working": in_prog, "finished": done},
                last_updated=old_last_updated,
            )
        )

        store.reset_in_progress(lambda c: c.status == IndexingStatus.IN_PROGRESS)

        result = store.read()
        assert result is not None
        # A real reset bumps the top-level last_updated timestamp.
        assert result.last_updated != old_last_updated
        reset = result.collections["working"]
        assert reset.status == IndexingStatus.PENDING
        # non-status fields preserved
        assert reset.total_files == 10
        assert reset.processed_files == 4
        assert reset.processed_paths == ["/a/file.md"]
        assert reset.indexed_embedding_model == "BAAI/bge-small-en-v1.5"
        assert reset.indexed_chunk_size == 512
        # status-related fields cleared
        assert reset.started_at is None
        assert reset.completed_at is None
        assert reset.error is None
        assert reset.error_count == 0
        # DONE entry untouched
        assert result.collections["finished"].status == IndexingStatus.DONE
        assert result.collections["finished"].completed_at == "2026-01-01T00:05:00+00:00"

    def test_reset_in_progress_short_circuits_when_no_match(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        store.write(
            IndexingState(collections={"col": CollectionProgress(status=IndexingStatus.DONE)})
        )
        calls: list[IndexingState] = []

        def spy_write(state: IndexingState) -> None:
            calls.append(state)

        with patch.object(store, "write", side_effect=spy_write):
            store.reset_in_progress(lambda c: False)
        assert calls == []

    def test_reset_in_progress_short_circuits_on_none_state(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        assert not store._state_file.exists()
        calls: list[IndexingState] = []

        def spy_write(state: IndexingState) -> None:
            calls.append(state)

        with patch.object(store, "write", side_effect=spy_write):
            # No state file → must not raise and must not write.
            store.reset_in_progress(lambda cp: True)
        assert calls == []
        assert not store._state_file.exists()

    def test_reset_in_progress_preserves_non_status_fields(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        cp = CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=20,
            processed_files=7,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:09:00+00:00",
            error="some error",
            error_count=2,
            processed_paths=["/a/file.md", "/b/doc.txt"],
            file_mtimes={"/a/file.md": 1700000000.5},
            file_hashes={"/a/file.md": "abc123"},
            indexed_embedding_model="BAAI/bge-small-en-v1.5",
            indexed_chunk_size=256,
        )
        store.write(IndexingState(collections={"col": cp}))

        store.reset_in_progress(lambda c: c.status == IndexingStatus.IN_PROGRESS)

        result = store.read()
        assert result is not None
        out = result.collections["col"]
        assert out.status == IndexingStatus.PENDING
        # preserved
        assert out.total_files == 20
        assert out.processed_files == 7
        assert out.processed_paths == ["/a/file.md", "/b/doc.txt"]
        assert out.file_mtimes == {"/a/file.md": 1700000000.5}
        assert out.file_hashes == {"/a/file.md": "abc123"}
        assert out.indexed_embedding_model == "BAAI/bge-small-en-v1.5"
        assert out.indexed_chunk_size == 256
        # cleared
        assert out.started_at is None
        assert out.completed_at is None
        assert out.error is None
        assert out.error_count == 0

    def test_reset_in_progress_skips_failed_and_done(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        store.write(
            IndexingState(
                collections={
                    "working": CollectionProgress(
                        status=IndexingStatus.IN_PROGRESS, total_files=3, processed_files=1
                    ),
                    "broken": CollectionProgress(
                        status=IndexingStatus.FAILED, error="bad", error_count=4
                    ),
                    "finished": CollectionProgress(
                        status=IndexingStatus.DONE, total_files=2, processed_files=2
                    ),
                }
            )
        )

        store.reset_in_progress(lambda cp: cp.status == IndexingStatus.IN_PROGRESS)

        result = store.read()
        assert result is not None
        assert result.collections["working"].status == IndexingStatus.PENDING
        assert result.collections["broken"].status == IndexingStatus.FAILED
        assert result.collections["broken"].error == "bad"
        assert result.collections["broken"].error_count == 4
        assert result.collections["finished"].status == IndexingStatus.DONE

    def test_reset_in_progress_all_entries_match(self, tmp_path: Path) -> None:
        store = IndexingStateStore(tmp_path)
        store.write(
            IndexingState(
                collections={
                    f"col_{i}": CollectionProgress(
                        status=IndexingStatus.IN_PROGRESS,
                        total_files=i + 1,
                        processed_files=i,
                        started_at="2026-01-01T00:00:00+00:00",
                        error_count=i,
                    )
                    for i in range(3)
                }
            )
        )

        store.reset_in_progress(lambda cp: cp.status == IndexingStatus.IN_PROGRESS)

        result = store.read()
        assert result is not None
        for i in range(3):
            out = result.collections[f"col_{i}"]
            assert out.status == IndexingStatus.PENDING
            assert out.total_files == i + 1
            assert out.processed_files == i
            assert out.started_at is None
            assert out.error_count == 0


class TestResetInProgressThreadSafety(_ThreadSafetyHarness):
    """Concurrency regression test for reset_in_progress (reuses the CON-3 harness)."""

    def test_reset_in_progress_concurrent_with_update_collection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Race reset_in_progress (resets IN_PROGRESS entries to PENDING) against an
        # update of a DIFFERENT key. Under the lock both RMW cycles serialize so
        # BOTH effects land; without it the last writer clobbers the other's stale
        # snapshot, dropping one effect. See the parent class docstring for the
        # shared lost-update rationale.
        store = IndexingStateStore(tmp_path)
        store.write(
            IndexingState(
                collections={
                    "working": CollectionProgress(
                        status=IndexingStatus.IN_PROGRESS, total_files=5, processed_files=2
                    ),
                }
            )
        )
        self._slow_read_patch(monkeypatch)
        self._atomic_write_patch(monkeypatch)  # final file always valid JSON
        start = threading.Barrier(2)

        def resetter() -> None:
            start.wait()
            store.reset_in_progress(lambda cp: cp.status == IndexingStatus.IN_PROGRESS)

        def updater() -> None:
            start.wait()
            store.update_collection("new-col", CollectionProgress(status=IndexingStatus.DONE))

        threads = [threading.Thread(target=resetter), threading.Thread(target=updater)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=self._JOIN_TIMEOUT)
            assert not t.is_alive()

        # (a) valid JSON on disk (atomic write guarantees this even without the lock)
        raw = json.loads(store._state_file.read_text())
        assert isinstance(raw, dict)
        result = store.read()
        assert result is not None
        # (b) the update survived
        assert "new-col" in result.collections
        # (c) the reset survived: the previously IN_PROGRESS entry is now PENDING
        assert result.collections["working"].status == IndexingStatus.PENDING
