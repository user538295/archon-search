"""Tests for _hybrid_search_with_trace ACL population and SearchStore.hybrid_search_with_trace delegate."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from archon_search._diagnostics import ScoredSearchCandidate
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
