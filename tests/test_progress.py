"""packages/archon-search/tests/test_progress.py — CollectionProgress and IndexingState dataclasses."""
from __future__ import annotations

import json
import os
import threading
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


# ---------------------------------------------------------------------------
# Task 1.1 — Concurrency / RLock tests
# ---------------------------------------------------------------------------

class TestIndexingStateStoreLocking:
    """Tests that IndexingStateStore is thread-safe via an internal RLock."""

    def test_rlock_attribute_exists(self, tmp_path: Path) -> None:
        """IndexingStateStore must expose _lock as a threading.RLock."""
        store = IndexingStateStore(tmp_path)
        assert hasattr(store, "_lock"), "IndexingStateStore must have a _lock attribute"
        # RLock instances are a subtype of _RLock; check by attempting acquire/release
        store._lock.acquire()
        store._lock.release()

    def test_concurrent_update_collection_no_lost_writes(self, tmp_path: Path) -> None:
        """N concurrent update_collection calls each writing a distinct key must all survive."""
        N = 8
        store = IndexingStateStore(tmp_path)
        start_barrier = threading.Barrier(N)
        errors: list[Exception] = []

        def write_one(i: int) -> None:
            try:
                start_barrier.wait(timeout=10)  # all threads start simultaneously
                store.update_collection(
                    f"col-{i}",
                    CollectionProgress(status=IndexingStatus.PENDING, total_files=i),
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write_one, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Threads raised: {errors}"
        final_state = store.read()
        assert final_state is not None
        missing = [f"col-{i}" for i in range(N) if f"col-{i}" not in final_state.collections]
        assert not missing, f"Lost writes for collections: {missing}"

    def test_concurrent_writers_same_key_last_write_wins(self, tmp_path: Path) -> None:
        """Two threads both updating the same key — result must be valid JSON with one entry."""
        store = IndexingStateStore(tmp_path)
        start_barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def write_one(total: int) -> None:
            try:
                start_barrier.wait(timeout=5)
                store.update_collection(
                    "shared-col",
                    CollectionProgress(status=IndexingStatus.PENDING, total_files=total),
                )
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=write_one, args=(1,))
        t2 = threading.Thread(target=write_one, args=(2,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors
        final_state = store.read()
        assert final_state is not None
        assert "shared-col" in final_state.collections
        assert final_state.collections["shared-col"].total_files in (1, 2)

    def test_remove_collection_early_return_is_locked(self, tmp_path: Path) -> None:
        """Concurrent remove_collection and update_collection must not corrupt state."""
        store = IndexingStateStore(tmp_path)
        store.update_collection("k", CollectionProgress(status=IndexingStatus.PENDING))
        start_barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def do_remove() -> None:
            try:
                start_barrier.wait(timeout=5)
                store.remove_collection("k")
            except Exception as exc:
                errors.append(exc)

        def do_update() -> None:
            try:
                start_barrier.wait(timeout=5)
                store.update_collection("k", CollectionProgress(status=IndexingStatus.DONE))
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=do_remove)
        t2 = threading.Thread(target=do_update)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors
        # State must be valid JSON — either 0 or 1 entries for "k" (both valid)
        final_state = store.read()
        assert final_state is not None

    def test_exception_under_lock_releases_lock(self, tmp_path: Path) -> None:
        """If write() raises inside update_collection, the lock must be released."""
        store = IndexingStateStore(tmp_path)
        call_count = [0]
        original_write = IndexingStateStore.write

        def raising_write(self, state: IndexingState) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("simulated disk full")
            return original_write(self, state)

        with patch.object(IndexingStateStore, "write", raising_write):
            with pytest.raises(OSError, match="simulated disk full"):
                store.update_collection("col", CollectionProgress(status=IndexingStatus.PENDING))

        # The lock must have been released — set_trigger should complete within 1 second
        result_holder: list = []

        def do_set_trigger() -> None:
            store.set_trigger("x")
            result_holder.append(True)

        t = threading.Thread(target=do_set_trigger)
        t.start()
        t.join(timeout=1)
        assert not t.is_alive(), "Lock was not released after exception"
        assert result_holder == [True]

    def test_rlock_reentry_does_not_deadlock(self, tmp_path: Path) -> None:
        """update_collection calls write() internally — must not deadlock with RLock."""
        if hasattr(IndexingStateStore, "_write_unlocked"):
            pytest.skip("_write_unlocked helper factored — re-entry test not applicable")

        store = IndexingStateStore(tmp_path)
        result_holder: list = []

        def run() -> None:
            store.update_collection("col", CollectionProgress(status=IndexingStatus.PENDING))
            result_holder.append(True)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=2)
        assert not t.is_alive(), "Thread deadlocked — RLock re-entry failed"
        assert result_holder == [True]

    def test_read_does_not_acquire_lock(self, tmp_path: Path) -> None:
        """Two concurrent read() calls must not block each other."""
        store = IndexingStateStore(tmp_path)
        state = IndexingState(collections={"col": CollectionProgress(status=IndexingStatus.DONE)})
        store.write(state)

        barrier = threading.Barrier(2)
        results: list = []
        errors: list[Exception] = []

        def do_read() -> None:
            try:
                barrier.wait(timeout=5)
                r = store.read()
                results.append(r)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=do_read)
        t2 = threading.Thread(target=do_read)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors
        assert len(results) == 2
        assert all(r is not None for r in results)

    def test_set_trigger_under_concurrent_update_collection(self, tmp_path: Path) -> None:
        """Concurrent set_trigger and update_collection must not lose either write."""
        store = IndexingStateStore(tmp_path)
        start_barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def do_set_trigger() -> None:
            try:
                start_barrier.wait(timeout=5)
                store.set_trigger("t")
            except Exception as exc:
                errors.append(exc)

        def do_update() -> None:
            try:
                start_barrier.wait(timeout=5)
                store.update_collection("col", CollectionProgress(status=IndexingStatus.PENDING))
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=do_set_trigger)
        t2 = threading.Thread(target=do_update)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors
        final = store.read()
        assert final is not None
        # Both writes must have survived
        assert final.trigger == "t", "set_trigger write was lost"
        assert "col" in final.collections, "update_collection write was lost"

    def test_write_is_locked_independently(self, tmp_path: Path) -> None:
        """write() itself must be locked — two concurrent write() calls both complete."""
        store = IndexingStateStore(tmp_path)
        start_barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def write_one(name: str) -> None:
            try:
                state = IndexingState(
                    collections={name: CollectionProgress(status=IndexingStatus.PENDING)}
                )
                start_barrier.wait(timeout=5)
                store.write(state)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=write_one, args=("col-a",))
        t2 = threading.Thread(target=write_one, args=("col-b",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors
        # Final state must be valid JSON — one of the two writes wins
        final = store.read()
        assert final is not None


# ---------------------------------------------------------------------------
# Task 1.2 — reset_in_progress() tests
# ---------------------------------------------------------------------------

class TestResetInProgress:
    """Tests for IndexingStateStore.reset_in_progress(predicate)."""

    def test_reset_in_progress_resets_matching_entries(self, tmp_path: Path) -> None:
        """IN_PROGRESS entry becomes PENDING; DONE entry is unchanged."""
        store = IndexingStateStore(tmp_path)
        cp_in_progress = CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=10,
            processed_files=5,
            processed_paths=["/a/file.md"],
            file_mtimes={"/a/file.md": 1.0},
            file_hashes={"/a/file.md": "abc"},
            indexed_embedding_model="model-x",
            indexed_chunk_size=256,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at=None,
            error="some error",
            error_count=3,
        )
        cp_done = CollectionProgress(status=IndexingStatus.DONE, total_files=5, processed_files=5)
        state = IndexingState(collections={"in-prog": cp_in_progress, "done-col": cp_done})
        store.write(state)

        store.reset_in_progress(lambda cp: cp.status == IndexingStatus.IN_PROGRESS)

        final = store.read()
        assert final is not None
        assert final.collections["in-prog"].status == IndexingStatus.PENDING
        assert final.collections["done-col"].status == IndexingStatus.DONE

    def test_reset_in_progress_preserves_non_status_fields(self, tmp_path: Path) -> None:
        """Non-status fields survive the reset."""
        store = IndexingStateStore(tmp_path)
        cp = CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=10,
            processed_files=5,
            processed_paths=["/a/file.md"],
            file_mtimes={"/a/file.md": 1.0},
            file_hashes={"/a/file.md": "abc"},
            indexed_embedding_model="model-x",
            indexed_chunk_size=256,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:01:00+00:00",
            error="oops",
            error_count=2,
        )
        state = IndexingState(collections={"col": cp})
        store.write(state)

        store.reset_in_progress(lambda c: c.status == IndexingStatus.IN_PROGRESS)

        final = store.read()
        assert final is not None
        result_cp = final.collections["col"]
        assert result_cp.status == IndexingStatus.PENDING
        # Non-status fields preserved
        assert result_cp.total_files == 10
        assert result_cp.processed_files == 5
        assert result_cp.processed_paths == ["/a/file.md"]
        assert result_cp.file_mtimes == {"/a/file.md": 1.0}
        assert result_cp.file_hashes == {"/a/file.md": "abc"}
        assert result_cp.indexed_embedding_model == "model-x"
        assert result_cp.indexed_chunk_size == 256
        # Status-related fields cleared
        assert result_cp.started_at is None
        assert result_cp.completed_at is None
        assert result_cp.error is None
        assert result_cp.error_count == 0

    def test_reset_in_progress_short_circuits_when_no_match(self, tmp_path: Path) -> None:
        """No write() when predicate matches zero entries."""
        store = IndexingStateStore(tmp_path)
        cp = CollectionProgress(status=IndexingStatus.DONE, total_files=5)
        state = IndexingState(collections={"done-col": cp})
        store.write(state)

        write_calls: list = []
        original_write = IndexingStateStore.write

        def tracking_write(self, s: IndexingState) -> None:
            write_calls.append(s)
            return original_write(self, s)

        with patch.object(IndexingStateStore, "write", tracking_write):
            store.reset_in_progress(lambda c: False)

        assert write_calls == [], "write() should not be called when predicate matches nothing"

    def test_reset_in_progress_short_circuits_on_none_state(self, tmp_path: Path) -> None:
        """No exception and no write when state file is absent."""
        store = IndexingStateStore(tmp_path)
        write_calls: list = []
        original_write = IndexingStateStore.write

        def tracking_write(self, s: IndexingState) -> None:
            write_calls.append(s)
            return original_write(self, s)

        with patch.object(IndexingStateStore, "write", tracking_write):
            store.reset_in_progress(lambda c: True)

        assert write_calls == [], "write() should not be called when state is None"

    def test_reset_in_progress_skips_failed_and_done(self, tmp_path: Path) -> None:
        """Only IN_PROGRESS entries are affected by the standard predicate."""
        store = IndexingStateStore(tmp_path)
        state = IndexingState(collections={
            "in-prog": CollectionProgress(status=IndexingStatus.IN_PROGRESS, total_files=3),
            "failed": CollectionProgress(status=IndexingStatus.FAILED, total_files=2),
            "done": CollectionProgress(status=IndexingStatus.DONE, total_files=1),
        })
        store.write(state)

        store.reset_in_progress(lambda cp: cp.status == IndexingStatus.IN_PROGRESS)

        final = store.read()
        assert final is not None
        assert final.collections["in-prog"].status == IndexingStatus.PENDING
        assert final.collections["failed"].status == IndexingStatus.FAILED
        assert final.collections["done"].status == IndexingStatus.DONE

    def test_reset_in_progress_all_entries_match(self, tmp_path: Path) -> None:
        """All IN_PROGRESS entries become PENDING with preserved non-status fields."""
        store = IndexingStateStore(tmp_path)
        state = IndexingState(collections={
            f"col-{i}": CollectionProgress(
                status=IndexingStatus.IN_PROGRESS,
                total_files=i + 1,
                processed_files=i,
            )
            for i in range(3)
        })
        store.write(state)

        store.reset_in_progress(lambda cp: cp.status == IndexingStatus.IN_PROGRESS)

        final = store.read()
        assert final is not None
        for i in range(3):
            cp = final.collections[f"col-{i}"]
            assert cp.status == IndexingStatus.PENDING
            assert cp.total_files == i + 1
            assert cp.processed_files == i

    def test_reset_in_progress_concurrent_with_update_collection(
        self, tmp_path: Path
    ) -> None:
        """Concurrent reset_in_progress and update_collection must produce coherent state."""
        store = IndexingStateStore(tmp_path)
        state = IndexingState(collections={
            "existing": CollectionProgress(status=IndexingStatus.IN_PROGRESS, total_files=5),
        })
        store.write(state)

        start_barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def do_reset() -> None:
            try:
                start_barrier.wait(timeout=5)
                store.reset_in_progress(lambda cp: cp.status == IndexingStatus.IN_PROGRESS)
            except Exception as exc:
                errors.append(exc)

        def do_update() -> None:
            try:
                start_barrier.wait(timeout=5)
                store.update_collection(
                    "new-col", CollectionProgress(status=IndexingStatus.PENDING, total_files=1)
                )
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=do_reset)
        t2 = threading.Thread(target=do_update)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors
        final = store.read()
        assert final is not None
        assert "new-col" in final.collections, "update_collection write was lost"
        assert final.collections.get("existing") is not None
