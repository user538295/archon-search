"""Tests for archon.rag.progress — CollectionProgress and IndexingState dataclasses."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from archon.rag.progress import (
    CollectionProgress,
    IndexingState,
    IndexingStateStore,
    IndexingStatus,
    from_dict,
    to_dict,
)


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
