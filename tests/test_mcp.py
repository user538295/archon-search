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
    """get_collections_meta must exclude centroid and have description_embedding=None by default."""
    import asyncio

    meta = _make_meta_with_embeddings()
    tool_fn = _get_tool_fn("get_collections_meta", meta)
    result = asyncio.run(tool_fn())

    assert isinstance(result, list)
    item = result[0]
    # centroid is an internal field, excluded after Task 2.5 Pydantic migration
    assert "centroid" not in item
    # description_embedding is present but null when include_description_embedding=False
    assert "description_embedding" in item
    assert item["description_embedding"] is None


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


# ---------------------------------------------------------------------------
# Task 5.1 — MCP RAG Fusion wiring tests
# ---------------------------------------------------------------------------


def _make_rag_fusion_pipeline_mock(
    search_result=None,
    search_many_result=None,
    swc_result=None,
    explain_result=None,
):
    """Return a pipeline mock configured for RAG Fusion wiring tests."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.pipeline import (
        ExplainPipelineResult,
        RagFusionSubQueryInfo,
        SearchPipelineResult,
        SearchWithContextResult,
    )

    if search_result is None:
        search_result = SearchPipelineResult(
            results=[], acl_filtered=False, excluded_collections=[],
            rag_fusion_applied=True, rag_fusion_queries_used=2, rag_fusion_attempted=True,
        )
    if search_many_result is None:
        search_many_result = SearchPipelineResult(
            results=[], acl_filtered=False, excluded_collections=[],
            rag_fusion_applied=True, rag_fusion_queries_used=2, rag_fusion_attempted=True,
        )
    if swc_result is None:
        pipeline_result = SearchPipelineResult(
            results=[], acl_filtered=False,
            rag_fusion_applied=True, rag_fusion_queries_used=2, rag_fusion_attempted=True,
        )
        swc_result = SearchWithContextResult(results=[], pipeline_result=pipeline_result)
    if explain_result is None:
        explain_result = ExplainPipelineResult(
            top_results=[], near_misses=[], acl_filtered=False,
            excluded_collections=[],
            rag_fusion_applied=True, rag_fusion_queries_used=2, rag_fusion_attempted=True,
            rag_fusion_sub_query_results=[
                RagFusionSubQueryInfo(variant_index=0, result_count=1, top_doc_ids=["doc-a"]),
                RagFusionSubQueryInfo(variant_index=1, result_count=0, top_doc_ids=[]),
                RagFusionSubQueryInfo(variant_index=2, result_count=1, top_doc_ids=["doc-b"]),
            ],
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


def _make_config_with_rag_fusion(enabled: bool = True):
    """Return a SearchConfig-like MagicMock with rag_fusion.enabled set."""
    config = MagicMock()
    config.hyde.enabled = False
    config.rag_fusion.enabled = enabled
    config.embedding_model = "test-model"
    config.observability.stage_timings_enabled = False
    config.routing_shortlist_size = 5
    config.routing_confidence_threshold = 0.5
    return config


def _make_rag_fusion_generator_mock(variants=None):
    """Return a mock RAGFusionGenerator whose generate_variants() returns variants."""
    gen = MagicMock()
    gen.generate_variants = AsyncMock(
        return_value=variants if variants is not None else ["variant one", "variant two"]
    )
    return gen


def _get_rag_fusion_tool_fn(
    tool_name: str,
    pipeline,
    config=None,
    hyde_generator=None,
    rag_fusion_generator=None,
):
    """Build a stub-backed MCP app and return the named tool function."""
    import importlib
    import archon_search.server.mcp as mcp_mod
    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(
        pipeline,
        "col1",
        config=config,
        hyde_generator=hyde_generator,
        rag_fusion_generator=rag_fusion_generator,
    )
    return app._tools[tool_name]


def test_mcp_search_tool_rag_fusion_parameter_accepted() -> None:
    """search tool accepts rag_fusion=True without error when generator is mocked."""
    import asyncio

    pipeline = _make_rag_fusion_pipeline_mock()
    config = _make_config_with_rag_fusion(enabled=True)
    gen = _make_rag_fusion_generator_mock()
    tool_fn = _get_rag_fusion_tool_fn("search", pipeline, config=config, rag_fusion_generator=gen)

    result = asyncio.run(tool_fn(query="what is archon?", collection="col1", rag_fusion=True))

    assert isinstance(result, dict)
    assert "error" not in result or result.get("code") != "internal_error"


def test_mcp_search_tool_rag_fusion_applied_in_result() -> None:
    """search tool accepts rag_fusion=True without error; response has McpSearchResponse shape."""
    import asyncio

    pipeline = _make_rag_fusion_pipeline_mock()
    config = _make_config_with_rag_fusion(enabled=True)
    gen = _make_rag_fusion_generator_mock()
    tool_fn = _get_rag_fusion_tool_fn("search", pipeline, config=config, rag_fusion_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collection="col1", rag_fusion=True))

    assert isinstance(result, dict)
    # After migration to McpSearchResponse, rag_fusion_* fields are not in the search response.
    # The search response is narrowed to: results, acl_filtered, excluded_collections, hyde_applied.
    assert "results" in result
    assert "acl_filtered" in result


def test_mcp_search_tool_rag_fusion_true_skips_hyde() -> None:
    """search tool with rag_fusion=True skips HyDE: resolve_hyde_vector NOT called."""
    import asyncio
    from unittest.mock import patch

    pipeline = _make_rag_fusion_pipeline_mock()
    config = _make_config_with_rag_fusion(enabled=True)
    rf_gen = _make_rag_fusion_generator_mock()
    hyde_gen = MagicMock()
    hyde_gen.generate = AsyncMock(return_value=[0.5] * 5)

    import importlib
    import archon_search.server.mcp as mcp_mod
    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(
        pipeline, "col1", config=config,
        hyde_generator=hyde_gen, rag_fusion_generator=rf_gen,
    )
    tool_fn = app._tools["search"]

    with patch("archon_search.server.mcp.resolve_hyde_vector", new=AsyncMock(return_value=([0.5] * 5, True))) as mock_resolve:
        result = asyncio.run(tool_fn(query="test query", collection="col1", rag_fusion=True, hyde=True))

    # resolve_hyde_vector must NOT have been called when rag_fusion=True
    mock_resolve.assert_not_called()
    assert isinstance(result, dict)
    assert result.get("hyde_applied") is False


def test_mcp_search_with_context_rag_fusion() -> None:
    """search_with_context returns SearchWithContextResponse shape (results + hyde_applied only).
    rag_fusion_applied / rag_fusion_queries_used / rag_fusion_attempted are intentionally
    excluded from the MCP response after the Task 2.2 Pydantic migration."""
    import asyncio

    pipeline = _make_rag_fusion_pipeline_mock()
    config = _make_config_with_rag_fusion(enabled=True)
    gen = _make_rag_fusion_generator_mock()
    tool_fn = _get_rag_fusion_tool_fn(
        "search_with_context", pipeline, config=config, rag_fusion_generator=gen
    )

    result = asyncio.run(tool_fn(query="test query", collection="col1", rag_fusion=True))

    assert isinstance(result, dict)
    # SearchWithContextResponse only exposes results and hyde_applied
    assert set(result.keys()) == {"results", "hyde_applied"}
    assert "rag_fusion_applied" not in result
    assert "rag_fusion_queries_used" not in result
    assert "rag_fusion_attempted" not in result


def test_mcp_search_with_context_telemetry_includes_feature_flags() -> None:
    """search_with_context telemetry now includes rag_fusion fields (pre-existing gap fixed)."""
    import asyncio

    from archon_search.telemetry.writer import TelemetryWriter

    pipeline = _make_rag_fusion_pipeline_mock()
    config = _make_config_with_rag_fusion(enabled=False)

    writer = MagicMock(spec=TelemetryWriter)

    import importlib
    import archon_search.server.mcp as mcp_mod
    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(pipeline, "col1", config=config, writer=writer)
    tool_fn = app._tools["search_with_context"]

    asyncio.run(tool_fn(query="test query", collection="col1", hyde=False, rag_fusion=False))

    writer.enqueue.assert_called_once()
    entry = writer.enqueue.call_args[0][0]
    # Verify rag_fusion fields are present (may be None/False when disabled)
    assert hasattr(entry, "rag_fusion_applied")
    assert hasattr(entry, "rag_fusion_queries_used")


def test_mcp_explain_rag_fusion() -> None:
    """explain tool includes all five rag_fusion fields in result dict."""
    import asyncio

    pipeline = _make_rag_fusion_pipeline_mock()
    config = _make_config_with_rag_fusion(enabled=True)
    gen = _make_rag_fusion_generator_mock()
    tool_fn = _get_rag_fusion_tool_fn(
        "explain", pipeline, config=config, rag_fusion_generator=gen
    )

    result = asyncio.run(tool_fn(query="test query", collection="col1", rag_fusion=True))

    assert isinstance(result, dict)
    assert result.get("rag_fusion_applied") is True
    assert result.get("rag_fusion_queries_used") == 2
    assert result.get("rag_fusion_attempted") is True
    assert "rag_fusion_sub_queries" in result



def test_mcp_search_with_context_rag_fusion_true_skips_hyde() -> None:
    """search_with_context with rag_fusion=True skips HyDE: resolve_hyde_vector NOT called."""
    import asyncio
    from unittest.mock import patch

    pipeline = _make_rag_fusion_pipeline_mock()
    config = _make_config_with_rag_fusion(enabled=True)
    rf_gen = _make_rag_fusion_generator_mock()
    hyde_gen = MagicMock()
    hyde_gen.generate = AsyncMock(return_value=[0.5] * 5)

    import importlib
    import archon_search.server.mcp as mcp_mod
    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(
        pipeline, "col1", config=config,
        hyde_generator=hyde_gen, rag_fusion_generator=rf_gen,
    )
    tool_fn = app._tools["search_with_context"]

    with patch("archon_search.server.mcp.resolve_hyde_vector", new=AsyncMock(return_value=([0.5] * 5, True))) as mock_resolve:
        result = asyncio.run(tool_fn(query="test query", collection="col1", rag_fusion=True, hyde=True))

    mock_resolve.assert_not_called()
    assert isinstance(result, dict)
    assert result.get("hyde_applied") is False
    # rag_fusion_applied is excluded from the MCP response (Task 2.2 narrowing)
    assert "rag_fusion_applied" not in result


# ---------------------------------------------------------------------------
# Task 2.1 — _ERR_SCHEMA constant + migrate search tool
# ---------------------------------------------------------------------------


def _make_search_result():
    """Return a minimal SearchResult for search tool tests."""
    from archon_search._types import SearchResult

    return SearchResult(
        doc_id="doc1",
        chunk_id="doc1-000000",
        text="hello world",
        score=0.9,
        source_path="/path/file.md",
        file_type="md",
        language="en",
        indexed_at="2024-01-01T00:00:00.000000Z",
        updated_at="2024-01-01T00:00:00.000000Z",
        ingested_by="cli",
        metadata={"k": "v"},
        acl=None,
        collection="col1",
    )


def _make_search_pipeline_with_result(result=None, acl_filtered=False, excluded=None):
    """Return a pipeline mock whose search() returns a SearchPipelineResult."""
    from archon_search.pipeline import SearchPipelineResult

    if result is None:
        result = _make_search_result()
    if excluded is None:
        excluded = []
    pipeline = MagicMock()
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
    pipeline.get_collection_meta = AsyncMock(return_value=None)
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=[result],
            acl_filtered=acl_filtered,
            excluded_collections=excluded,
        )
    )
    return pipeline


def _get_search_tool_fn(pipeline, config=None):
    """Build a stub-backed MCP app and return the search tool function."""
    import importlib
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(pipeline, "col1", config=config)
    return app._tools["search"]


def test_search_returns_mcp_search_response_shape() -> None:
    """search tool returns a dict with exactly McpSearchResponse keys (no embedding_model)."""
    import asyncio

    pipeline = _make_search_pipeline_with_result()
    tool_fn = _get_search_tool_fn(pipeline)
    result = asyncio.run(tool_fn(query="hello", collection="col1"))

    assert isinstance(result, dict)
    assert set(result.keys()) == {"results", "acl_filtered", "excluded_collections", "hyde_applied"}


def test_search_include_metadata_false_clears_metadata() -> None:
    """search tool with include_metadata=False returns empty metadata dicts."""
    import asyncio

    pipeline = _make_search_pipeline_with_result()
    tool_fn = _get_search_tool_fn(pipeline)
    result = asyncio.run(tool_fn(query="hello", collection="col1", include_metadata=False))

    assert isinstance(result, dict)
    for item in result["results"]:
        assert item["metadata"] == {}


def test_search_multi_collection_returns_mcp_search_response_shape() -> None:
    """Multi-collection search path also returns McpSearchResponse shape."""
    import asyncio
    from archon_search.pipeline import SearchPipelineResult

    search_result = _make_search_result()
    pipeline = MagicMock()
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
    pipeline.get_collection_meta = AsyncMock(return_value=None)
    pipeline.search_many = AsyncMock(
        return_value=SearchPipelineResult(
            results=[search_result],
            acl_filtered=False,
            excluded_collections=[],
        )
    )

    import importlib
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(pipeline, "col1")
    tool_fn = app._tools["search"]

    result = asyncio.run(tool_fn(query="hello", collections=["col1", "col2"]))

    assert isinstance(result, dict)
    assert set(result.keys()) == {"results", "acl_filtered", "excluded_collections", "hyde_applied"}


def test_search_acl_filtered_with_excluded_collections() -> None:
    """search returns acl_filtered=True and excluded_collections with name/reason."""
    import asyncio
    from archon_search.pipeline import ExcludedCollection

    excluded = [ExcludedCollection(name="restricted-col", reason="acl")]
    pipeline = _make_search_pipeline_with_result(acl_filtered=True, excluded=excluded)
    tool_fn = _get_search_tool_fn(pipeline)
    result = asyncio.run(tool_fn(query="hello", collection="col1"))

    assert result["acl_filtered"] is True
    assert len(result["excluded_collections"]) == 1
    assert result["excluded_collections"][0]["name"] == "restricted-col"
    assert result["excluded_collections"][0]["reason"] == "acl"


def test_search_schema_drift_returns_schema_validation_error() -> None:
    """search returns schema_validation_error code when schema construction raises ValidationError."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError
    from archon_search.server.mcp_schemas import McpSearchResultSchema
    from archon_search.server.mcp import _ERR_SCHEMA

    # Build a real ValidationError using the helper pattern from the plan
    try:
        McpSearchResultSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    pipeline = _make_search_pipeline_with_result()
    tool_fn = _get_search_tool_fn(pipeline)

    with patch(
        "archon_search.server.mcp.McpSearchResultSchema.from_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn(query="hello", collection="col1"))

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA


# ---------------------------------------------------------------------------
# Task 2.2 — Migrate search_with_context tool
# ---------------------------------------------------------------------------


def _make_swc_pipeline_with_result(result=None, hyde_applied=False, with_context=True):
    """Return a pipeline mock whose search_with_context() returns a SearchWithContextResult."""
    from archon_search._types import ChunkRecord, SearchResult
    from archon_search.pipeline import SearchPipelineResult, SearchWithContextResult

    if result is None:
        result = SearchResult(
            doc_id="doc1",
            chunk_id="doc1-000000",
            text="hello world",
            score=0.9,
            source_path="/path/file.md",
            file_type="md",
            language="en",
            indexed_at="2024-01-01T00:00:00.000000Z",
            updated_at="2024-01-01T00:00:00.000000Z",
            ingested_by="cli",
            metadata={"k": "v"},
            acl=None,
            collection="col1",
        )

    def _make_chunk(offset_start=0, offset_end=5):
        return ChunkRecord(
            doc_id="doc1",
            chunk_id=f"doc1-{offset_start:06d}",
            text=f"context chunk {offset_start}",
            vector=[0.1, 0.2],
            source_path="/path/file.md",
            indexed_at="2024-01-01T00:00:00.000000Z",
            file_type="md",
            language="en",
            metadata={"ck": "cv"},
            custom_score=0.8,
            ingested_by="cli",
            updated_at="2024-01-01T00:00:00.000000Z",
            acl=None,
            start_offset=offset_start,
            end_offset=offset_end,
        )

    pipeline_result = SearchPipelineResult(
        results=[result],
        acl_filtered=False,
        excluded_collections=[],
        rag_fusion_applied=False,
        rag_fusion_queries_used=0,
        rag_fusion_attempted=False,
    )
    context_chunks = [_make_chunk(0, 5)] if with_context else []
    swc_result = SearchWithContextResult(
        results=[{"result": result, "context_before": context_chunks, "context_after": context_chunks}],
        pipeline_result=pipeline_result,
    )

    pipeline = MagicMock()
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
    pipeline.get_collection_meta = AsyncMock(return_value=None)
    pipeline.search_with_context = AsyncMock(return_value=swc_result)
    return pipeline


def _get_swc_tool_fn(pipeline, config=None):
    """Build a stub-backed MCP app and return the search_with_context tool function."""
    import importlib
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(pipeline, "col1", config=config)
    return app._tools["search_with_context"]


def test_search_with_context_result_shape() -> None:
    """search_with_context returns dict with results list and hyde_applied bool;
    context chunks must not contain start_offset, end_offset, custom_score, vector."""
    import asyncio

    pipeline = _make_swc_pipeline_with_result()
    tool_fn = _get_swc_tool_fn(pipeline)
    result = asyncio.run(tool_fn(query="hello", collection="col1"))

    assert isinstance(result, dict)
    assert "results" in result
    assert "hyde_applied" in result
    assert isinstance(result["hyde_applied"], bool)

    # Verify context chunks exclude transient fields
    for item in result["results"]:
        for chunk in item.get("context_before", []) + item.get("context_after", []):
            assert "vector" not in chunk
            assert "start_offset" not in chunk
            assert "end_offset" not in chunk
            assert "custom_score" not in chunk


def test_search_with_context_hyde_applied_propagated() -> None:
    """search_with_context propagates hyde_applied=True when HyDE is active."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=True)
    gen = _make_hyde_generator_mock(vector=[0.5] * 5)
    tool_fn = _get_hyde_tool_fn("search_with_context", pipeline, config=config, hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collection="col1", hyde=True))

    assert isinstance(result, dict)
    assert result.get("hyde_applied") is True


def test_search_with_context_hyde_applied_false() -> None:
    """search_with_context returns hyde_applied=False when HyDE is not used."""
    import asyncio

    pipeline = _make_swc_pipeline_with_result()
    tool_fn = _get_swc_tool_fn(pipeline)
    result = asyncio.run(tool_fn(query="hello", collection="col1", hyde=False))

    assert isinstance(result, dict)
    assert result.get("hyde_applied") is False


def test_search_with_context_include_metadata_false_clears_metadata() -> None:
    """search_with_context with include_metadata=False clears metadata in results and context chunks."""
    import asyncio

    pipeline = _make_swc_pipeline_with_result()
    tool_fn = _get_swc_tool_fn(pipeline)
    result = asyncio.run(tool_fn(query="hello", collection="col1", include_metadata=False))

    assert isinstance(result, dict)
    for item in result["results"]:
        assert item["result"]["metadata"] == {}
        for chunk in item.get("context_before", []) + item.get("context_after", []):
            assert chunk["metadata"] == {}


def test_chunk_to_context_dict_removed() -> None:
    """_chunk_to_context_dict helper must not exist in mcp module after migration."""
    import importlib
    import archon_search.server.mcp as mcp_module

    importlib.reload(mcp_module)
    assert not hasattr(mcp_module, "_chunk_to_context_dict")


def test_search_with_context_schema_drift_returns_schema_validation_error() -> None:
    """search_with_context returns schema_validation_error when schema construction raises ValidationError."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError
    from archon_search.server.mcp_schemas import McpSearchResultSchema
    from archon_search.server.mcp import _ERR_SCHEMA

    # Build a real ValidationError using the test helper pattern
    try:
        McpSearchResultSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    pipeline = _make_swc_pipeline_with_result()
    tool_fn = _get_swc_tool_fn(pipeline)

    with patch(
        "archon_search.server.mcp.McpSearchResultSchema.from_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn(query="hello", collection="col1"))

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA


# ---------------------------------------------------------------------------
# Task 2.3 — Migrate explain tool (ValidationError catch)
# ---------------------------------------------------------------------------


def test_explain_schema_drift_returns_schema_validation_error() -> None:
    """explain (single-collection path) returns schema_validation_error when ExplainResponse.from_pipeline_result raises ValidationError."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError

    from archon_search.server.mcp import _ERR_SCHEMA
    from archon_search.server.mcp_schemas import McpSearchResultSchema

    # Build a real ValidationError using the test helper pattern
    try:
        McpSearchResultSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=False)
    tool_fn = _get_hyde_tool_fn("explain", pipeline, config=config)

    with patch(
        "archon_search.server.mcp.ExplainResponse.from_pipeline_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn(query="test query", collection="col1"))

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA


def test_explain_multi_collection_schema_drift_returns_schema_validation_error() -> None:
    """explain (multi-collection path) returns schema_validation_error when ExplainResponse.from_pipeline_result raises ValidationError."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError

    from archon_search.server.mcp import _ERR_SCHEMA
    from archon_search.server.mcp_schemas import McpSearchResultSchema

    # Build a real ValidationError using the test helper pattern
    try:
        McpSearchResultSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=False)
    tool_fn = _get_hyde_tool_fn("explain", pipeline, config=config)

    with patch(
        "archon_search.server.mcp.ExplainResponse.from_pipeline_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn(query="test query", collections=["col1"]))

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA


# ---------------------------------------------------------------------------
# Task 2.4 — Migrate ingest_file and ingest_directory tools
# ---------------------------------------------------------------------------


def _make_ingest_pipeline_with_result(needs_recompute=True):
    """Return a pipeline mock whose ingest_file returns an IngestResult with needs_recompute set."""
    from archon_search._types import IngestResult

    result = IngestResult(
        doc_id="doc1",
        chunks_created=5,
        status="ok",
        error=None,
        needs_recompute=needs_recompute,
    )
    pipeline = MagicMock()
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
    pipeline.get_collection_meta = AsyncMock(return_value=None)
    pipeline.ingest_file = AsyncMock(return_value=result)
    pipeline.ingest_directory = AsyncMock(return_value=[result])
    return pipeline


def _get_ingest_tool_fn(tool_name: str, pipeline, config=None):
    """Build a stub-backed MCP app and return the ingest tool function."""
    import importlib
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(pipeline, "col1", config=config)
    return app._tools[tool_name]


def test_ingest_file_result_excludes_needs_recompute() -> None:
    """ingest_file must not include needs_recompute in the return dict."""
    import asyncio

    pipeline = _make_ingest_pipeline_with_result(needs_recompute=True)
    tool_fn = _get_ingest_tool_fn("ingest_file", pipeline)
    result = asyncio.run(tool_fn(path="/tmp/test.md", collection="col1"))

    assert isinstance(result, dict)
    assert "needs_recompute" not in result
    assert result.get("doc_id") == "doc1"
    assert result.get("chunks_created") == 5
    assert result.get("status") == "ok"


def test_ingest_directory_result_excludes_needs_recompute() -> None:
    """ingest_directory must not include needs_recompute in the return list items."""
    import asyncio

    pipeline = _make_ingest_pipeline_with_result(needs_recompute=True)
    tool_fn = _get_ingest_tool_fn("ingest_directory", pipeline)
    result = asyncio.run(tool_fn(path="/tmp/", collection="col1"))

    assert isinstance(result, list)
    assert len(result) == 1
    item = result[0]
    assert "needs_recompute" not in item
    assert item.get("doc_id") == "doc1"
    assert item.get("chunks_created") == 5


def test_ingest_file_schema_drift_returns_schema_validation_error() -> None:
    """ingest_file returns schema_validation_error code when schema construction raises ValidationError."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError

    from archon_search.server.mcp import _ERR_SCHEMA
    from archon_search.server.mcp_schemas import IngestResultSchema

    # Build a real ValidationError using the test helper pattern
    try:
        IngestResultSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    pipeline = _make_ingest_pipeline_with_result()
    tool_fn = _get_ingest_tool_fn("ingest_file", pipeline)

    with patch(
        "archon_search.server.mcp.IngestResultSchema.from_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn(path="/tmp/test.md", collection="col1"))

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA


def test_ingest_directory_schema_drift_returns_schema_validation_error() -> None:
    """ingest_directory returns schema_validation_error code when schema construction raises ValidationError."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError

    from archon_search.server.mcp import _ERR_SCHEMA
    from archon_search.server.mcp_schemas import IngestResultSchema

    # Build a real ValidationError using the test helper pattern
    try:
        IngestResultSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    pipeline = _make_ingest_pipeline_with_result()
    tool_fn = _get_ingest_tool_fn("ingest_directory", pipeline)

    with patch(
        "archon_search.server.mcp.IngestResultSchema.from_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn(path="/tmp/", collection="col1"))

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA


# ---------------------------------------------------------------------------
# Task 2.5 — Migrate list_collections and get_collections_meta tools
# ---------------------------------------------------------------------------


def _make_full_collection_meta():
    """Return a CollectionMeta with all internal fields populated."""
    from archon_search.collection_meta import CollectionMeta
    from datetime import datetime

    return CollectionMeta(
        name="col1",
        description="test collection",
        centroid=[0.1, 0.2, 0.3],
        centroid_sum=[0.3, 0.6, 0.9],
        mutations_since_recompute=5,
        needs_recompute=True,
        doc_count=10,
        chunk_count=50,
        active_embedding_model="text-embed-v2",
        pending_embedding_model=None,
        needs_reindex=False,
        reindex_job_id=None,
        last_indexed=datetime(2024, 1, 1, 12, 0, 0),
        last_described=datetime(2024, 1, 2, 12, 0, 0),
        described_at_doc_count=10,
        namespace="default",
        description_embedding=[0.4, 0.5, 0.6],
    )


def _get_collections_tool_fn(tool_name: str, meta, config=None):
    """Build a stub-backed MCP app and return the named tool function."""
    import importlib
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(return_value=[meta])
    app = mcp_mod.create_app(pipeline, "col1", config=config)
    return app._tools[tool_name]


_INTERNAL_FIELDS = {
    "centroid",
    "centroid_sum",
    "namespace",
    "needs_recompute",
    "needs_reindex",
    "reindex_job_id",
    "mutations_since_recompute",
    "described_at_doc_count",
}


def test_list_collections_excludes_internal_fields() -> None:
    """list_collections must exclude all internal CollectionMeta fields."""
    import asyncio

    meta = _make_full_collection_meta()
    tool_fn = _get_collections_tool_fn("list_collections", meta)
    result = asyncio.run(tool_fn())

    assert isinstance(result, list)
    assert len(result) == 1
    item = result[0]
    for field in _INTERNAL_FIELDS:
        assert field not in item, f"internal field {field!r} must not appear in list_collections output"


def test_list_collections_renames_active_embedding_model() -> None:
    """list_collections must return embedding_model (not active_embedding_model)."""
    import asyncio

    meta = _make_full_collection_meta()
    tool_fn = _get_collections_tool_fn("list_collections", meta)
    result = asyncio.run(tool_fn())

    assert isinstance(result, list)
    item = result[0]
    assert "embedding_model" in item
    assert item["embedding_model"] == "text-embed-v2"
    assert "active_embedding_model" not in item


def test_get_collections_meta_without_description_embedding() -> None:
    """get_collections_meta with include_description_embedding=False returns description_embedding as None."""
    import asyncio

    meta = _make_full_collection_meta()
    tool_fn = _get_collections_tool_fn("get_collections_meta", meta)
    result = asyncio.run(tool_fn(include_description_embedding=False))

    assert isinstance(result, list)
    item = result[0]
    assert "description_embedding" in item
    assert item["description_embedding"] is None


def test_get_collections_meta_with_description_embedding() -> None:
    """get_collections_meta with include_description_embedding=True includes the vector."""
    import asyncio

    meta = _make_full_collection_meta()
    tool_fn = _get_collections_tool_fn("get_collections_meta", meta)
    result = asyncio.run(tool_fn(include_description_embedding=True))

    assert isinstance(result, list)
    item = result[0]
    assert item["description_embedding"] == [0.4, 0.5, 0.6]


def test_list_collections_schema_drift_returns_schema_validation_error() -> None:
    """list_collections returns schema_validation_error when from_result raises ValidationError."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError

    from archon_search.server.mcp import _ERR_SCHEMA
    from archon_search.server.mcp_schemas import CollectionListItemSchema

    try:
        CollectionListItemSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    meta = _make_full_collection_meta()
    tool_fn = _get_collections_tool_fn("list_collections", meta)

    with patch(
        "archon_search.server.mcp.CollectionListItemSchema.from_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn())

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA


def test_get_collections_meta_schema_drift_returns_schema_validation_error() -> None:
    """get_collections_meta returns schema_validation_error when from_result raises ValidationError."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError

    from archon_search.server.mcp import _ERR_SCHEMA
    from archon_search.server.mcp_schemas import CollectionMetaMcpSchema

    try:
        CollectionMetaMcpSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    meta = _make_full_collection_meta()
    tool_fn = _get_collections_tool_fn("get_collections_meta", meta)

    with patch(
        "archon_search.server.mcp.CollectionMetaMcpSchema.from_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn())

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA
