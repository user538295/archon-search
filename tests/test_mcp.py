"""Tests for _needs_install_trigger() and MCP tools in archon_search.server.mcp."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# fastmcp stub — must support tool() decorator and custom_route() so
# create_app() can register tools and we can retrieve the inner functions.
# ---------------------------------------------------------------------------
class _StubFastMCP:
    def __init__(self, *args, **kwargs):
        self._tools: dict = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def custom_route(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


if "fastmcp" not in sys.modules:
    _fastmcp = types.ModuleType("fastmcp")
    _fastmcp.FastMCP = _StubFastMCP  # type: ignore[attr-defined]
    _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fastmcp
else:
    # Already imported — patch the class so create_app picks up our stub.
    sys.modules["fastmcp"].FastMCP = _StubFastMCP  # type: ignore[attr-defined]

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


# ---------------------------------------------------------------------------
# MCP tool: list_collections strips description_embedding and centroid
# ---------------------------------------------------------------------------

def _make_meta_with_embeddings():
    """Return a CollectionMeta with both centroid and description_embedding set."""
    from archon_search.collection_meta import CollectionMeta
    return CollectionMeta(
        name="col1",
        description="test",
        centroid=[0.2, 0.3],
        description_embedding=[0.1, 0.4],
    )


def _make_pipeline_mock(meta):
    """Return a minimal pipeline mock whose get_all_collections_meta returns [meta]."""
    from unittest.mock import AsyncMock, MagicMock
    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(return_value=[meta])
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    return pipeline


def _get_tool_fn(tool_name: str, meta):
    """Build a _StubFastMCP-backed app and return the named tool's inner function."""
    import importlib
    import archon_search.server.mcp as mcp_mod
    # Force re-import so create_app picks up the stub FastMCP class.
    importlib.reload(mcp_mod)
    pipeline = _make_pipeline_mock(meta)
    app = mcp_mod.create_app(pipeline, "col1")
    return app._tools[tool_name]


def test_list_collections_strips_description_embedding() -> None:
    """list_collections must omit both centroid and description_embedding."""
    import asyncio

    meta = _make_meta_with_embeddings()
    tool_fn = _get_tool_fn("list_collections", meta)
    result = asyncio.run(tool_fn())

    assert isinstance(result, list)
    assert len(result) == 1
    item = result[0]
    assert "centroid" not in item
    assert "description_embedding" not in item


def test_get_collections_meta_strips_description_embedding_by_default() -> None:
    """get_collections_meta must strip description_embedding but keep centroid by default."""
    import asyncio

    meta = _make_meta_with_embeddings()
    tool_fn = _get_tool_fn("get_collections_meta", meta)
    result = asyncio.run(tool_fn())

    assert isinstance(result, list)
    item = result[0]
    assert "centroid" in item
    assert "description_embedding" not in item


def test_get_collections_meta_includes_description_embedding_when_opted_in() -> None:
    """get_collections_meta must include description_embedding when include_description_embedding=True."""
    import asyncio

    meta = _make_meta_with_embeddings()
    tool_fn = _get_tool_fn("get_collections_meta", meta)
    result = asyncio.run(tool_fn(include_description_embedding=True))

    assert isinstance(result, list)
    item = result[0]
    assert "description_embedding" in item
    assert item["description_embedding"] == [0.1, 0.4]


def test_get_collection_meta_includes_description_embedding() -> None:
    """get_collection_meta returns description_embedding by default (bounded payload)."""
    import asyncio

    meta = _make_meta_with_embeddings()
    tool_fn = _get_tool_fn("get_collection_meta", meta)
    result = asyncio.run(tool_fn(name="col1"))

    assert isinstance(result, dict)
    assert "description_embedding" in result
    assert result["description_embedding"] == [0.1, 0.4]



