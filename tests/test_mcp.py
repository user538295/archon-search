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


_MCP_MODULE = "archon_search.server.mcp"
_FASTMCP_MODULE = "fastmcp"

# Always load the real fastmcp classes directly from source, regardless of
# what's in sys.modules at collection time. This survives any collection order.
try:
    import mcp.server.fastmcp as _mcp_server_fastmcp  # type: ignore[import]
    _real_fastmcp_class = _mcp_server_fastmcp.FastMCP
    _real_fastmcp_context = getattr(_mcp_server_fastmcp, "Context", None)
except (ImportError, AttributeError):
    _real_fastmcp_class = None
    _real_fastmcp_context = None

# Install stub into sys.modules["fastmcp"] so archon_search.server.mcp can be imported.
if _FASTMCP_MODULE not in sys.modules:
    _fastmcp_mod = types.ModuleType(_FASTMCP_MODULE)
    _fastmcp_mod.FastMCP = _StubFastMCP  # type: ignore[attr-defined]
    _fastmcp_mod.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules[_FASTMCP_MODULE] = _fastmcp_mod
else:
    sys.modules[_FASTMCP_MODULE].FastMCP = _StubFastMCP  # type: ignore[attr-defined]

from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus, IndexingStateStore
from archon_search.server.mcp import _needs_install_trigger

# Restore real FastMCP immediately after the module-level import so that other
# test modules (test_mcp_auth.py, test_mcp_search.py) see the real class whether
# they are collected before or after this module.
if _real_fastmcp_class is not None:
    sys.modules[_FASTMCP_MODULE].FastMCP = _real_fastmcp_class  # type: ignore[attr-defined]
    if _real_fastmcp_context is not None:
        sys.modules[_FASTMCP_MODULE].Context = _real_fastmcp_context  # type: ignore[attr-defined]
sys.modules.pop(_MCP_MODULE, None)


@pytest.fixture(autouse=True, scope="module")
def _stub_fastmcp_for_module():
    """Reinstall _StubFastMCP only for this module's test execution, then restore."""
    sys.modules[_FASTMCP_MODULE].FastMCP = _StubFastMCP  # type: ignore[attr-defined]
    sys.modules.pop(_MCP_MODULE, None)  # force reload with stub in _get_tool_fn
    yield
    if _real_fastmcp_class is not None:
        sys.modules[_FASTMCP_MODULE].FastMCP = _real_fastmcp_class  # type: ignore[attr-defined]
        if _real_fastmcp_context is not None:
            sys.modules[_FASTMCP_MODULE].Context = _real_fastmcp_context  # type: ignore[attr-defined]
    sys.modules.pop(_MCP_MODULE, None)


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


# ---------------------------------------------------------------------------
# B5 Task 4.2 — MCP delete_document: StoreBusyError mapping and namespace forwarding
# ---------------------------------------------------------------------------


def _make_pipeline_mock_with_delete(delete_side_effect=None, delete_return=0):
    """Return a pipeline mock for delete_document tests."""
    pipeline = MagicMock()
    pipeline.delete_document = AsyncMock(
        side_effect=delete_side_effect,
        return_value=delete_return if delete_side_effect is None else None,
    )
    return pipeline


def _get_delete_tool_fn(pipeline):
    """Build a stub-backed app with the given pipeline and return delete_document tool fn."""
    import importlib
    import archon_search.server.mcp as mcp_mod
    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(pipeline, "col1")
    return app._tools["delete_document"]


def test_mcp_delete_document_maps_store_busy_to_store_busy_code() -> None:
    """delete_document MCP handler returns code='store_busy' when store is busy."""
    import asyncio
    from archon_search.store import StoreBusyError

    pipeline = _make_pipeline_mock_with_delete(delete_side_effect=StoreBusyError(timeout_s=0.1))
    tool_fn = _get_delete_tool_fn(pipeline)

    result = asyncio.run(tool_fn(doc_id="a" * 64, collection="col1"))

    assert isinstance(result, dict)
    assert result.get("code") == "store_busy"


def test_mcp_delete_document_forwards_namespace() -> None:
    """delete_document MCP handler forwards namespace parameter to pipeline.delete_document."""
    import asyncio

    pipeline = _make_pipeline_mock_with_delete(delete_return=1)
    tool_fn = _get_delete_tool_fn(pipeline)

    asyncio.run(tool_fn(doc_id="b" * 64, collection="col1", namespace="tenant1"))

    pipeline.delete_document.assert_awaited_once()
    _args, _kwargs = pipeline.delete_document.call_args
    assert _kwargs.get("namespace") == "tenant1" or (len(_args) >= 3 and _args[2] == "tenant1")


# ---------------------------------------------------------------------------
# Task 5.1 — MCP HyDE wiring: search, search_with_context, explain tools
# ---------------------------------------------------------------------------


def _make_hyde_pipeline_mock(search_result=None, search_many_result=None, swc_result=None, explain_result=None):
    """Return a pipeline mock configured for HyDE wiring tests."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.pipeline import ExplainPipelineResult

    if search_result is None:
        from archon_search.pipeline import SearchPipelineResult
        search_result = SearchPipelineResult(results=[], acl_filtered=False, excluded_collections=[])
    if search_many_result is None:
        from archon_search.pipeline import SearchPipelineResult
        search_many_result = SearchPipelineResult(results=[], acl_filtered=False, excluded_collections=[])
    if swc_result is None:
        from archon_search.pipeline import SearchPipelineResult, SearchWithContextResult
        swc_result = SearchWithContextResult(
            results=[],
            pipeline_result=SearchPipelineResult(results=[], acl_filtered=False),
        )
    if explain_result is None:
        explain_result = ExplainPipelineResult(
            top_results=[], near_misses=[], acl_filtered=False,
            excluded_collections=[],
        )

    pipeline = MagicMock()
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
    pipeline.search = AsyncMock(return_value=search_result)
    pipeline.search_many = AsyncMock(return_value=search_many_result)
    pipeline.search_with_context = AsyncMock(return_value=swc_result)
    pipeline.explain = AsyncMock(return_value=explain_result)
    pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col1"))
    pipeline.get_all_collections_meta = AsyncMock(return_value=[CollectionMeta(name="col1")])
    return pipeline


def _make_config_with_hyde(enabled: bool = True):
    """Return a SearchConfig-like MagicMock with hyde.enabled set."""
    config = MagicMock()
    config.hyde.enabled = enabled
    config.embedding_model = "test-model"
    config.observability.stage_timings_enabled = False
    config.routing_shortlist_size = 5
    config.routing_confidence_threshold = 0.5
    return config


def _make_hyde_generator_mock(vector=None):
    """Return a mock HyDEGenerator whose generate() returns vector (or [0.5] * 5)."""
    gen = MagicMock()
    gen.generate = AsyncMock(return_value=vector if vector is not None else [0.5] * 5)
    return gen


def _get_hyde_tool_fn(tool_name: str, pipeline, config=None, hyde_generator=None):
    """Build a stub-backed MCP app and return the named tool function."""
    import importlib
    import archon_search.server.mcp as mcp_mod
    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(
        pipeline, "col1", config=config, hyde_generator=hyde_generator
    )
    return app._tools[tool_name]


def test_mcp_search_tool_hyde_parameter_accepted() -> None:
    """search tool accepts hyde=True without error when generator is mocked."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=True)
    gen = _make_hyde_generator_mock()
    tool_fn = _get_hyde_tool_fn("search", pipeline, config=config, hyde_generator=gen)

    result = asyncio.run(tool_fn(query="what is archon?", collection="col1", hyde=True))

    assert isinstance(result, dict)
    assert "error" not in result or result.get("code") != "internal_error"


def test_mcp_search_tool_hyde_applied_in_result() -> None:
    """search tool returns hyde_applied=True when generator returns a vector."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=True)
    gen = _make_hyde_generator_mock(vector=[0.5] * 5)
    tool_fn = _get_hyde_tool_fn("search", pipeline, config=config, hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collection="col1", hyde=True))

    assert isinstance(result, dict)
    assert result.get("hyde_applied") is True


def test_mcp_search_tool_hyde_applied_false_when_hyde_false() -> None:
    """search tool returns hyde_applied=False when hyde=False."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=True)
    gen = _make_hyde_generator_mock()
    tool_fn = _get_hyde_tool_fn("search", pipeline, config=config, hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collection="col1", hyde=False))

    assert isinstance(result, dict)
    assert result.get("hyde_applied") is False


def test_mcp_search_tool_hyde_package_not_installed_returns_error() -> None:
    """search tool returns error dict when HyDE package not installed (RuntimeError)."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=True)
    gen = _make_hyde_generator_mock()
    gen.generate = AsyncMock(side_effect=RuntimeError("Install archon-search[hyde]"))

    tool_fn = _get_hyde_tool_fn("search", pipeline, config=config, hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collection="col1", hyde=True))

    assert isinstance(result, dict)
    assert "error" in result


def test_mcp_search_with_context_hyde() -> None:
    """search_with_context tool accepts hyde=True and returns hyde_applied in result dict."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=True)
    gen = _make_hyde_generator_mock(vector=[0.5] * 5)
    tool_fn = _get_hyde_tool_fn("search_with_context", pipeline, config=config, hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collection="col1", hyde=True))

    # Task 5.1: search_with_context now returns {"results": [...], "hyde_applied": bool}
    assert isinstance(result, dict)
    assert "hyde_applied" in result
    assert result.get("hyde_applied") is True


def test_mcp_search_with_context_hyde_false_returns_hyde_applied_false() -> None:
    """search_with_context returns hyde_applied=False when hyde=False."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=True)
    gen = _make_hyde_generator_mock()
    tool_fn = _get_hyde_tool_fn("search_with_context", pipeline, config=config, hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collection="col1", hyde=False))

    assert isinstance(result, dict)
    assert "hyde_applied" in result
    assert result.get("hyde_applied") is False


def test_mcp_explain_hyde() -> None:
    """explain tool accepts hyde=True and returns hyde_applied=True in result dict."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=True)
    gen = _make_hyde_generator_mock(vector=[0.5] * 5)
    tool_fn = _get_hyde_tool_fn("explain", pipeline, config=config, hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collection="col1", hyde=True))

    assert isinstance(result, dict)
    assert result.get("hyde_applied") is True


def test_mcp_explain_hyde_false_returns_hyde_applied_false() -> None:
    """explain tool returns hyde_applied=False when hyde=False."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=True)
    gen = _make_hyde_generator_mock()
    tool_fn = _get_hyde_tool_fn("explain", pipeline, config=config, hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collection="col1", hyde=False))

    assert isinstance(result, dict)
    assert result.get("hyde_applied") is False



