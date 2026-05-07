"""Tests for _needs_install_trigger() in archon_search.server.mcp (M12.13–M12.16)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# fastmcp is not installed in the test venv; stub it so mcp.py can be imported.
if "fastmcp" not in sys.modules:
    _fastmcp = types.ModuleType("fastmcp")
    _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fastmcp

from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus, IndexingStateStore
from archon_search.server.mcp import _needs_install_trigger


# ---------------------------------------------------------------------------
# M12.13 — no state file → fresh install, must trigger
# ---------------------------------------------------------------------------

def test_M12_13_no_state_file_returns_true(tmp_path: Path) -> None:
    """M12.13: When no state file exists, _needs_install_trigger returns True."""
    store = IndexingStateStore(tmp_path)
    state = store.read()  # None — file doesn't exist yet

    assert _needs_install_trigger(state, {"docs": "/path/to/docs"}) is True


# ---------------------------------------------------------------------------
# M12.14 — state exists but a desired collection is absent → must trigger
# ---------------------------------------------------------------------------

def test_M12_14_new_collection_absent_returns_true(tmp_path: Path) -> None:
    """M12.14: State exists but a desired collection is not tracked — returns True."""
    store = IndexingStateStore(tmp_path)
    existing = IndexingState(
        collections={
            "old-col": CollectionProgress(status=IndexingStatus.DONE),
        }
    )
    store.write(existing)

    state = store.read()
    assert _needs_install_trigger(state, {"new-col": "/path/to/new"}) is True


# ---------------------------------------------------------------------------
# M12.15 — all desired collections are DONE → no trigger needed
# ---------------------------------------------------------------------------

def test_M12_15_all_done_returns_false(tmp_path: Path) -> None:
    """M12.15: All desired collections are DONE — returns False."""
    store = IndexingStateStore(tmp_path)
    existing = IndexingState(
        collections={
            "docs": CollectionProgress(status=IndexingStatus.DONE),
            "notes": CollectionProgress(status=IndexingStatus.DONE),
        }
    )
    store.write(existing)

    state = store.read()
    assert _needs_install_trigger(state, {"docs": "/d", "notes": "/n"}) is False


# ---------------------------------------------------------------------------
# M12.16 — a collection is IN_PROGRESS → implies prior crash, must trigger
# ---------------------------------------------------------------------------

def test_M12_16_in_progress_returns_true(tmp_path: Path) -> None:
    """M12.16: A desired collection is IN_PROGRESS (crash-recovery) — returns True."""
    store = IndexingStateStore(tmp_path)
    existing = IndexingState(
        collections={
            "docs": CollectionProgress(status=IndexingStatus.DONE),
            "notes": CollectionProgress(status=IndexingStatus.IN_PROGRESS),
        }
    )
    store.write(existing)

    state = store.read()
    assert _needs_install_trigger(state, {"docs": "/d", "notes": "/n"}) is True
