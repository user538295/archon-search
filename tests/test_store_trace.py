"""Tests for _hybrid_search_with_trace ACL population and SearchStore.hybrid_search_with_trace delegate."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from archon_search._diagnostics import ScoredSearchCandidate
from archon_search.observability import bind_stage_recorder
from archon_search.store import SearchStore, _hybrid_search_with_trace

# ---------------------------------------------------------------------------
# Local helpers (mirrors test_store.py)
# ---------------------------------------------------------------------------

_DIM = 4  # tiny embedding dim for tests


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _trace_store_with_row(tmp_path: Path, name: str, row: dict) -> SearchStore:
    """Build a SearchStore whose vector + FTS legs both return ``row`` once."""
    store = SearchStore(tmp_path / name)
    mock_db = MagicMock()
    mock_table = MagicMock()
    mock_table.vector_search = MagicMock(
        return_value=MagicMock(
            limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[row])))
        )
    )
    fts_result = MagicMock()
    fts_result.limit = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[row]))
    )
    mock_table.search = AsyncMock(return_value=fts_result)
    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db
    return store


def _row(doc_suffix: str, **extra: object) -> dict:
    doc_id = _doc_id()
    base = {
        "doc_id": doc_id,
        "chunk_id": f"{doc_id}-000000",
        "text": f"row {doc_suffix}",
        "source_path": f"/tmp/{doc_suffix}.md",
        "_distance": 0.25,
        "_score": 1.5,
    }
    base.update(extra)
    return base


def _run_trace(store: SearchStore) -> list[ScoredSearchCandidate]:
    return asyncio.run(_hybrid_search_with_trace(store, "my-col", [0.0] * _DIM, "hello", 20))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hybrid_search_with_trace_populates_acl_on_candidates(tmp_path: Path) -> None:
    """Candidates copy the ACL list from the stored row when present (case 1)."""
    src_acl = ["team-a"]
    store = _trace_store_with_row(tmp_path, "db", _row("foo", acl=src_acl))

    candidates = _run_trace(store)

    assert len(candidates) == 1
    assert isinstance(candidates[0], ScoredSearchCandidate)
    assert candidates[0].acl == ["team-a"]
    # Defensive copy: candidate.acl must not alias the source row's list.
    assert candidates[0].acl is not src_acl


def test_hybrid_search_with_trace_acl_none_when_absent(tmp_path: Path) -> None:
    """Candidate.acl is None when the row has no acl key (case 2)."""
    store = _trace_store_with_row(tmp_path, "db2", _row("bar"))

    candidates = _run_trace(store)

    assert len(candidates) == 1
    assert candidates[0].acl is None


def test_hybrid_search_with_trace_acl_none_for_non_list_value(tmp_path: Path) -> None:
    """A truthy non-list acl value (e.g. a string) normalizes to None (case 3)."""
    store = _trace_store_with_row(tmp_path, "db3", _row("baz", acl="team-a"))

    candidates = _run_trace(store)

    assert len(candidates) == 1
    assert candidates[0].acl is None


def test_hybrid_search_with_trace_populates_file_type(tmp_path: Path) -> None:
    """file_type is copied verbatim from the stored row."""
    store = _trace_store_with_row(tmp_path, "db4", _row("ft", file_type="py"))
    candidates = _run_trace(store)
    assert len(candidates) == 1
    assert candidates[0].file_type == "py"


def test_hybrid_search_with_trace_populates_indexed_at(tmp_path: Path) -> None:
    """indexed_at is copied verbatim from the stored row."""
    ts = "2026-01-02T03:04:05Z"
    store = _trace_store_with_row(tmp_path, "db5", _row("ia", indexed_at=ts))
    candidates = _run_trace(store)
    assert len(candidates) == 1
    assert candidates[0].indexed_at == ts


def test_hybrid_search_with_trace_updated_at_falls_back_to_indexed_at(tmp_path: Path) -> None:
    """updated_at falls back to indexed_at when absent or empty."""
    ts = "2026-01-02T03:04:05Z"
    # No updated_at key present
    store = _trace_store_with_row(tmp_path, "db6a", _row("ua_none", indexed_at=ts))
    candidates = _run_trace(store)
    assert candidates[0].updated_at == ts

    # updated_at is explicitly empty string
    store2 = _trace_store_with_row(tmp_path, "db6b", _row("ua_empty", indexed_at=ts, updated_at=""))
    candidates2 = _run_trace(store2)
    assert candidates2[0].updated_at == ts

    # updated_at present and distinct from indexed_at → used verbatim (no fallback)
    other = "2026-02-09T00:00:00Z"
    store3 = _trace_store_with_row(tmp_path, "db6c", _row("ua_real", indexed_at=ts, updated_at=other))
    candidates3 = _run_trace(store3)
    assert candidates3[0].updated_at == other


def test_hybrid_search_with_trace_populates_ingested_by(tmp_path: Path) -> None:
    """ingested_by is copied from the stored row; defaults to 'cli' when absent."""
    store = _trace_store_with_row(tmp_path, "db7", _row("ib", ingested_by="watcher"))
    candidates = _run_trace(store)
    assert len(candidates) == 1
    assert candidates[0].ingested_by == "watcher"

    # Absent ingested_by → _normalize_ingested_by(None) → "cli"
    store2 = _trace_store_with_row(tmp_path, "db7b", _row("ib_absent"))
    candidates2 = _run_trace(store2)
    assert candidates2[0].ingested_by == "cli"


def test_hybrid_search_with_trace_normalizes_legacy_ingested_by(tmp_path: Path) -> None:
    """Legacy 'archon-search-cli' value is normalized to 'cli'."""
    store = _trace_store_with_row(tmp_path, "db8", _row("legacy", ingested_by="archon-search-cli"))
    candidates = _run_trace(store)
    assert len(candidates) == 1
    assert candidates[0].ingested_by == "cli"


def test_hybrid_search_with_trace_populates_language(tmp_path: Path) -> None:
    """language is copied when present; normalizes empty/absent to None."""
    store = _trace_store_with_row(tmp_path, "db9a", _row("lang_en", language="en"))
    candidates = _run_trace(store)
    assert candidates[0].language == "en"

    # Absent key → None
    store2 = _trace_store_with_row(tmp_path, "db9b", _row("lang_absent"))
    candidates2 = _run_trace(store2)
    assert candidates2[0].language is None

    # Empty string → None
    store3 = _trace_store_with_row(tmp_path, "db9c", _row("lang_empty", language=""))
    candidates3 = _run_trace(store3)
    assert candidates3[0].language is None


def test_hybrid_search_with_trace_metadata_is_parsed_dict(tmp_path: Path) -> None:
    """metadata JSON string is parsed into a dict."""
    store = _trace_store_with_row(tmp_path, "db10", _row("meta", metadata='{"k": "v"}'))
    candidates = _run_trace(store)
    assert len(candidates) == 1
    assert candidates[0].metadata == {"k": "v"}

    # Absent metadata key → parsed empty dict (not None)
    store2 = _trace_store_with_row(tmp_path, "db10b", _row("meta_absent"))
    candidates2 = _run_trace(store2)
    assert candidates2[0].metadata == {}


def test_lance_store_hybrid_search_with_trace_delegates_to_module_function(
    tmp_path: Path,
) -> None:
    """SearchStore.hybrid_search_with_trace delegates to module-level _hybrid_search_with_trace."""
    fake_candidates = [object()]

    with patch(
        "archon_search.store._hybrid_search_with_trace",
        new_callable=AsyncMock,
        return_value=fake_candidates,
    ) as mock_fn:
        store = SearchStore(tmp_path / "db")
        result = asyncio.run(
            store.hybrid_search_with_trace("col", [0.1], "q", 7)
        )

    mock_fn.assert_awaited_once_with(store, "col", [0.1], "q", 7)
    assert result is fake_candidates


# ---------------------------------------------------------------------------
# Stage instrumentation tests (Task 3.3)
# ---------------------------------------------------------------------------


def _store_with_fts(tmp_path: Path, name: str, row: dict) -> SearchStore:
    """Store where both vector and FTS legs succeed."""
    return _trace_store_with_row(tmp_path, name, row)


def _store_without_fts(tmp_path: Path, name: str, row: dict) -> SearchStore:
    """Store where FTS raises an 'index not available' error (degraded path)."""
    store = SearchStore(tmp_path / name)
    mock_db = MagicMock()
    mock_table = MagicMock()
    mock_table.vector_search = MagicMock(
        return_value=MagicMock(
            limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[row])))
        )
    )
    mock_table.search = AsyncMock(side_effect=RuntimeError("fts index not available"))
    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db
    return store


def _run_hybrid_search(store: SearchStore) -> list:
    return asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5))


def test_hybrid_search_records_vector_fuse_stages(tmp_path: Path) -> None:
    """hybrid_search records 'vector' and 'fuse' stages when a recorder is bound."""
    store = _store_with_fts(tmp_path, "hs1", _row("s1"))
    with bind_stage_recorder() as recorder:
        _run_hybrid_search(store)
    assert {"vector", "fuse"} <= recorder.stage_timings_ms.keys()


def test_hybrid_search_records_fts_when_index_exists(tmp_path: Path) -> None:
    """hybrid_search records 'fts' stage when FTS search succeeds."""
    store = _store_with_fts(tmp_path, "hs2", _row("s2"))
    with bind_stage_recorder() as recorder:
        _run_hybrid_search(store)
    assert "fts" in recorder.stage_timings_ms


def test_hybrid_search_omits_fts_when_no_index(tmp_path: Path) -> None:
    """hybrid_search omits 'fts' key when FTS raises (degraded path)."""
    store = _store_without_fts(tmp_path, "hs3", _row("s3"))
    with bind_stage_recorder() as recorder:
        _run_hybrid_search(store)
    timings = recorder.stage_timings_ms
    assert "fts" not in timings
    assert "vector" in timings
    assert "fuse" in timings


def test_hybrid_search_trace_records_same_stages(tmp_path: Path) -> None:
    """_hybrid_search_with_trace records 'vector', 'fts', and 'fuse' stages."""
    store = _store_with_fts(tmp_path, "hs4", _row("s4"))
    with bind_stage_recorder() as recorder:
        asyncio.run(_hybrid_search_with_trace(store, "my-col", [0.0] * _DIM, "hello", 20))
    timings = recorder.stage_timings_ms
    assert {"vector", "fts", "fuse"} <= timings.keys()


def test_hybrid_search_with_trace_omits_fts_when_no_index(tmp_path: Path) -> None:
    """_hybrid_search_with_trace omits 'fts' key on the degraded (no-index) path."""
    store = _store_without_fts(tmp_path, "hs5", _row("s5"))
    with bind_stage_recorder() as recorder:
        asyncio.run(_hybrid_search_with_trace(store, "my-col", [0.0] * _DIM, "hello", 20))
    timings = recorder.stage_timings_ms
    assert "fts" not in timings
    assert "vector" in timings
    assert "fuse" in timings
