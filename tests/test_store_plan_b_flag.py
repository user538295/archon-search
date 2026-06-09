"""Tests for the FTS_OPTIMIZE_REMOVES_DELETED module-level constant and
the ``supports_incremental_fts_delete`` property on ``SearchStore``.

Implements Task 1.2 of Documentation/Backlog/C6-incremental-fts-maintenance-plan.md.
"""
from __future__ import annotations

import pytest

import archon_search.store as store_module
from archon_search.store import FTS_OPTIMIZE_REMOVES_DELETED, SearchStore


def test_fts_optimize_removes_deleted_is_bool() -> None:
    """The module-level constant must be a bool (not just truthy)."""
    assert isinstance(FTS_OPTIMIZE_REMOVES_DELETED, bool)


def test_fts_optimize_removes_deleted_is_true() -> None:
    """Spike gate (c) passed — Plan A is active; constant must be True."""
    assert FTS_OPTIMIZE_REMOVES_DELETED is True


def test_supports_incremental_fts_delete_reflects_constant(tmp_path: pytest.TempPathFactory) -> None:
    """``store.supports_incremental_fts_delete`` must equal ``FTS_OPTIMIZE_REMOVES_DELETED``."""
    s = SearchStore(db_path=tmp_path)
    assert s.supports_incremental_fts_delete == store_module.FTS_OPTIMIZE_REMOVES_DELETED


def test_supports_incremental_fts_delete_is_bool(tmp_path: pytest.TempPathFactory) -> None:
    """Property must return a bool, not just a truthy value."""
    s = SearchStore(db_path=tmp_path)
    assert isinstance(s.supports_incremental_fts_delete, bool)


def test_supports_incremental_fts_delete_changes_with_constant(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the constant is patched to False (simulating Plan B), the property reflects it."""
    monkeypatch.setattr(store_module, "FTS_OPTIMIZE_REMOVES_DELETED", False)
    s = SearchStore(db_path=tmp_path)
    assert s.supports_incremental_fts_delete is False
