"""Tests for _needs_install_trigger() and MCP tools in archon_search.server.mcp."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Own xdist group ("mcp_stub"): this module installs a module-scoped global
# FastMCP stub via sys.modules mutation. Isolating it in a separate worker keeps
# that stub from leaking into real-FastMCP HTTP tests in the "mcp" group.
pytestmark = pytest.mark.xdist_group("mcp_stub")


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
# Use the real ``fastmcp`` package (3.4.x) whose FastMCP exposes http_app() — the
# low-level mcp.server.fastmcp class lacks it and would break create_mcp_http_app()
# for any sibling test that builds a real HTTP app after this module restores.
try:
    import fastmcp as _real_fastmcp_pkg  # type: ignore[import]
    _real_fastmcp_class = _real_fastmcp_pkg.FastMCP
    _real_fastmcp_context = getattr(_real_fastmcp_pkg, "Context", None)
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

from archon_search.constants import DEFAULT_CONTEXT_WINDOW_MAX
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


def test_get_collection_meta_excludes_description_embedding() -> None:
    """get_collection_meta uses CollectionDetailSchema which does not include description_embedding."""
    import asyncio

    meta = _make_meta_with_embeddings()
    tool_fn = _get_tool_fn("get_collection_meta", meta)
    result = asyncio.run(tool_fn(name="col1"))

    assert isinstance(result, dict)
    # CollectionDetailSchema does not expose description_embedding (use get_collections_meta for that)
    assert "description_embedding" not in result


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
    """delete_document MCP handler uses the authenticated namespace.

    BE-5: namespace now comes from _get_request_namespace() (authenticated namespace).
    The explicit namespace parameter is validated against the authenticated one.
    When they match, deletion proceeds with the authenticated namespace.
    """
    import asyncio
    from unittest.mock import patch

    pipeline = _make_pipeline_mock_with_delete(delete_return=1)
    tool_fn = _get_delete_tool_fn(pipeline)

    # Patch authenticated namespace to match the caller-supplied value
    with patch("archon_search.server.mcp._get_request_namespace", return_value="tenant1"):
        asyncio.run(tool_fn(doc_id="b" * 64, collection="col1", namespace="tenant1"))

    pipeline.delete_document.assert_awaited_once()
    _args, _kwargs = pipeline.delete_document.call_args
    assert _kwargs.get("namespace") == "tenant1" or (len(_args) >= 3 and _args[2] == "tenant1")


# ---------------------------------------------------------------------------
# C6-1 — MCP tools must map EmbedderNotReadyError to code='embedder_not_ready'
# instead of letting it fall into the generic except Exception -> 'internal_error'
# ---------------------------------------------------------------------------


def test_mcp_search_tool_maps_embedder_not_ready_to_embedder_not_ready_code() -> None:
    """The ``search`` MCP tool returns ``code='embedder_not_ready'``, not raising.

    A mounted FastMCP sub-app does not inherit FastAPI's exception handlers, and
    every MCP tool already wraps its body in a broad ``except Exception`` that
    returns an ``McpErrorResponse`` dict — without an explicit
    ``except EmbedderNotReadyError`` clause ahead of it, a wedged/slow model load
    would be misclassified as ``code='internal_error'`` instead of the retryable
    ``'embedder_not_ready'``.
    """
    import asyncio
    import importlib

    from archon_search.collection_meta import CollectionMeta
    from archon_search.embedder_cache import EmbedderNotReadyError
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    # active_embedding_model must be non-empty: resolve_active_model() falls
    # back to pipeline._global_embedder (never calling embedder_cache) when the
    # resolved model name is empty.
    meta = CollectionMeta(name="col1", namespace="default", active_embedding_model="some-model")
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=meta)

    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(
        side_effect=EmbedderNotReadyError("model still loading")
    )

    app = mcp_mod.create_app(pipeline, "col1", embedder_cache=embedder_cache)
    tool_fn = app._tools["search"]

    result = asyncio.run(tool_fn(query="test", collection="col1"))

    assert isinstance(result, dict)
    assert result.get("code") == "embedder_not_ready", (
        f"expected code='embedder_not_ready' from a wedged model load, got {result!r}"
    )


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
    pipeline.context_window_max = DEFAULT_CONTEXT_WINDOW_MAX
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
    # The public property and the private attribute are one object on a real pipeline (C2-T-9).
    pipeline.global_embedder = pipeline._global_embedder
    pipeline.search = AsyncMock(return_value=search_result)
    pipeline.search_many = AsyncMock(return_value=search_many_result)
    pipeline.warmup_models = AsyncMock(return_value=True)
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
    config.max_fanout = 8
    config.top_k_max = 100
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


_LEAKY_INSTALL_HINT = "printf '%s\\n' secret-exclude | uv tool install /opt/private/wheelhouse"


@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        ("search", {"collection": "col1"}),
        ("search", {"collections": ["col1", "col2"]}),
        ("search_with_context", {"collection": "col1"}),
        ("explain", {"collection": "col1"}),
        ("explain", {"collections": ["col1", "col2"]}),
    ],
)
def test_mcp_hyde_package_not_installed_returns_the_fixed_dependency_error(tool_name: str, kwargs: dict) -> None:
    """A missing HyDE package answers the fixed install message, never the exception's install command."""
    import asyncio

    import archon_search.hyde as hyde_mod

    pipeline = _make_hyde_pipeline_mock()
    gen = _make_hyde_generator_mock()
    tool_fn = _get_hyde_tool_fn(tool_name, pipeline, config=_make_config_with_hyde(enabled=True), hyde_generator=gen)
    # Looked up after the reload in _get_hyde_tool_fn, so it is the class mcp.py catches.
    gen.generate = AsyncMock(
        side_effect=hyde_mod.HyDEDependencyError(f"{hyde_mod.HYDE_DEPENDENCY_DETAIL} ({_LEAKY_INSTALL_HINT})")
    )

    result = asyncio.run(tool_fn(query="test query", hyde=True, **kwargs))

    assert result == {"error": "Install archon-search[hyde] to use HyDE", "code": "validation_error"}


_LEAKY_EMBED_ERROR = "ONNXRuntimeError: /Users/ops/.archon-search/models/onnx/model.onnx failed to load"


@pytest.mark.parametrize(
    ("tool_name", "kwargs", "expected"),
    [
        ("search", {"collection": "col1"}, {"error": "search failed", "code": "internal_error"}),
        ("search", {"collections": ["col1", "col2"]}, {"error": "search failed", "code": "internal_error"}),
        ("search_with_context", {"collection": "col1"}, {"error": "search failed", "code": "internal_error"}),
        ("explain", {"collection": "col1"}, {"error": "explain failed", "code": "internal_error"}),
        ("explain", {"collections": ["col1", "col2"]}, {"error": "explain failed", "code": "internal_error"}),
    ],
)
def test_mcp_hyde_embed_failure_does_not_leak_its_text(tool_name: str, kwargs: dict, expected: dict) -> None:
    """Any other RuntimeError during HyDE (the embedder's embed_one) is sanitized like a pipeline failure.

    Only ``HyDEDependencyError`` is a client-facing HyDE error; the fan-out paths used to
    resolve HyDE outside the catch-all, echoing the raw text as a ``validation_error``.
    """
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    gen = _make_hyde_generator_mock()
    gen.generate = AsyncMock(side_effect=RuntimeError(_LEAKY_EMBED_ERROR))
    tool_fn = _get_hyde_tool_fn(tool_name, pipeline, config=_make_config_with_hyde(enabled=True), hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", hyde=True, **kwargs))

    assert result == expected
    gen.generate.assert_awaited_once()


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


def test_mcp_explain_reranker_score_populated_when_rerank_true() -> None:
    """S391: MCP explain with rerank=true returns non-null reranker_score in breakdown."""
    import asyncio
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.pipeline import ExplainPipelineResult

    breakdown = SearchScoreBreakdown(
        vector_rank=0, vector_score=0.9, vector_score_kind="distance",
        fts_rank=0, fts_score=1.5, fts_score_kind="bm25",
        rrf_score=0.03, reranker_score=2.85,
    )
    candidate = ScoredSearchCandidate(
        doc_id="d1", chunk_id="d1-000000", text="test text",
        source_path="/tmp/test.md", score_breakdown=breakdown,
        collection="col1", file_type="md", indexed_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z", ingested_by="http",
    )
    explain_result = ExplainPipelineResult(
        top_results=[candidate], near_misses=[], acl_filtered=False,
        excluded_collections=[],
    )
    pipeline = _make_hyde_pipeline_mock(explain_result=explain_result)
    config = _make_config_with_hyde(enabled=False)
    tool_fn = _get_hyde_tool_fn("explain", pipeline, config=config)

    result = asyncio.run(tool_fn(query="test", collection="col1", rerank=True))

    assert isinstance(result, dict)
    results = result.get("results", [])
    assert len(results) == 1
    assert results[0]["breakdown"]["reranker_score"] == pytest.approx(2.85)


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


def test_mcp_explain_hyde_applies_with_the_cached_embedder_of_the_global_model() -> None:
    """explain must apply HyDE when the embedder cache serves the collection.

    The cache holds its own Embedder instance even for ``config.embedding_model``, so
    an identity check against ``pipeline._global_embedder`` used to discard the HyDE
    vector on every cached request. The hypothesis must instead be embedded with the
    embedder that serves the collection, and that vector must reach the pipeline.
    """
    import asyncio
    import importlib

    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    pipeline = _make_hyde_pipeline_mock()
    cached_embedder = MagicMock(name="cached-embedder")
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=cached_embedder)
    hyde_vector = [0.5] * 5
    gen = _make_hyde_generator_mock(vector=hyde_vector)
    app = mcp_mod.create_app(
        pipeline, "col1", config=_make_config_with_hyde(enabled=True),
        embedder_cache=embedder_cache, hyde_generator=gen,
    )

    result = asyncio.run(app._tools["explain"](query="test query", collection="col1", hyde=True))

    assert result.get("hyde_applied") is True, result
    assert gen.generate.await_args.kwargs["embedder"] is cached_embedder
    explain_kwargs = pipeline.explain.await_args.kwargs
    assert explain_kwargs["query_vector"] == hyde_vector
    assert explain_kwargs["embedder"] is cached_embedder


_SHORT_SEARCH_BUDGET_SECONDS = 0.05
_SILENT_PROVIDER_SECONDS = 30.0
_BOUNDED_CALL_CEILING_SECONDS = 5.0


@pytest.mark.parametrize("tool_name", ["search_with_context", "explain"])
def test_mcp_hyde_is_bounded_by_the_search_budget_on_swc_and_explain(
    tool_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S582 on the remaining HyDE surfaces: a silent LLM must not park the tool call.

    ``search`` already wrapped HyDE in ``resolve_hyde_within_budget``; ``search_with_context``
    and ``explain`` awaited the provider for the whole unclamped ``[hyde].timeout_seconds``.
    """
    import asyncio
    import time

    generation_started = asyncio.Event()

    async def _never_answers(*_args: object, **_kwargs: object) -> list[float]:
        generation_started.set()
        await asyncio.sleep(_SILENT_PROVIDER_SECONDS)
        raise AssertionError("the HyDE call was not cancelled by the search budget")

    pipeline = _make_hyde_pipeline_mock()
    gen = _make_hyde_generator_mock()
    gen.generate = _never_answers
    tool_fn = _get_hyde_tool_fn(tool_name, pipeline, config=_make_config_with_hyde(enabled=True), hyde_generator=gen)
    monkeypatch.setattr(sys.modules["archon_search.server.mcp"], "_SEARCH_TIMEOUT_SECONDS", _SHORT_SEARCH_BUDGET_SECONDS)

    started = time.monotonic()
    result = asyncio.run(tool_fn(query="test query", collection="col1", hyde=True))
    elapsed = time.monotonic() - started

    # Presence anchor: the budget cut a generation that started, not a skipped one.
    assert generation_started.is_set(), "HyDE generation never started"
    assert elapsed < _BOUNDED_CALL_CEILING_SECONDS, f"{tool_name} waited {elapsed:.1f}s on the HyDE call"
    assert result["hyde_applied"] is False, result


@pytest.mark.parametrize("tool_name", ["search_with_context", "explain"])
def test_mcp_hyde_on_swc_and_explain_still_runs_while_the_embedder_is_cold(tool_name: str) -> None:
    """C2-I-1: these tools have no FTS-only degrade — retrieval embeds on the cold model anyway.

    Skipping HyDE there saved no wait and only dropped the expansion, so HyDE runs (bounded
    by the search budget) and no extra bounded warm-up is spent on its behalf.
    """
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    pipeline._global_embedder.is_warm = False
    gen = _make_hyde_generator_mock(vector=[0.5] * 5)
    tool_fn = _get_hyde_tool_fn(tool_name, pipeline, config=_make_config_with_hyde(enabled=True), hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collection="col1", hyde=True))

    gen.generate.assert_awaited_once()
    assert gen.generate.await_args.kwargs["embedder"] is pipeline._global_embedder
    assert result["hyde_applied"] is True, result
    pipeline.warmup_models.assert_not_awaited()


@pytest.mark.parametrize("kwargs", [{"collection": "col1"}, {"collections": ["col1", "col2"]}])
@pytest.mark.parametrize("graph_mode", ["naive", "local", "global", "ppr"])
def test_mcp_search_graph_mode_suppresses_hyde(graph_mode: str, kwargs: dict) -> None:
    """C2-A-1: graph_mode wins over HyDE (most graph paths drop the vector), so the LLM is never called.

    It used to be: the query went off-host, ``hyde_applied`` read ``true``, and the vector
    was dropped by the graph path.
    """
    import asyncio

    if graph_mode == "ppr" and "collections" in kwargs:
        pytest.skip("ppr + collections is rejected before HyDE")
    pipeline = _make_hyde_pipeline_mock()
    gen = _make_hyde_generator_mock(vector=[0.5] * 5)
    tool_fn = _get_hyde_tool_fn("search", pipeline, config=_make_config_with_hyde(enabled=True), hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", hyde=True, graph_mode=graph_mode, **kwargs))

    assert "error" not in result, result
    gen.generate.assert_not_awaited()
    assert result["hyde_applied"] is False
    assert result["expansion_warning"] is None
    called = pipeline.search_many if "collections" in kwargs else pipeline.search
    assert called.await_args.kwargs["query_vector"] is None


def test_mcp_search_fan_out_hyde_embeds_with_the_global_embedder() -> None:
    """C2-T-9: search_many embeds with the global embedder, so HyDE embeds with that object too."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    gen = _make_hyde_generator_mock(vector=[0.5] * 5)
    tool_fn = _get_hyde_tool_fn("search", pipeline, config=_make_config_with_hyde(enabled=True), hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collections=["col1", "col2"], hyde=True))

    assert result["hyde_applied"] is True, result
    assert gen.generate.await_args.kwargs["embedder"] is pipeline.global_embedder
    assert pipeline.search_many.await_args.kwargs["query_vector"] == [0.5] * 5


def test_mcp_search_fan_out_skips_hyde_on_a_cold_global_embedder_with_one_warmup() -> None:
    """C2-T-4: the fan-out's own bounded warm-up answers HyDE's warmth — no second wait."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    pipeline._global_embedder.is_warm = False
    pipeline.embedder_is_warm = False
    gen = _make_hyde_generator_mock(vector=[0.5] * 5)
    tool_fn = _get_hyde_tool_fn("search", pipeline, config=_make_config_with_hyde(enabled=True), hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collections=["col1", "col2"], hyde=True))

    gen.generate.assert_not_awaited()
    pipeline.warmup_models.assert_awaited_once()
    assert result["hyde_applied"] is False, result
    assert result["expansion_warning"] == "HyDE expansion failed"
    assert pipeline.search_many.await_args.kwargs["fts_only"] is True


_LEAKY_RAG_FUSION_HINT = "uv tool install /opt/private/wheelhouse 'archon-search[rag_fusion]'"


@pytest.mark.parametrize(
    ("tool_name", "pipeline_method", "kwargs"),
    [
        ("search", "search", {"collection": "col1"}),
        ("search", "search_many", {"collections": ["col1", "col2"]}),
        ("search_with_context", "search_with_context", {"collection": "col1"}),
        ("explain", "explain", {"collection": "col1"}),
        ("explain", "explain", {"collections": ["col1", "col2"]}),
    ],
)
def test_mcp_rag_fusion_package_not_installed_returns_the_fixed_dependency_error(
    tool_name: str, pipeline_method: str, kwargs: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """G2: a missing RAG Fusion package answers fixed text; the install command stays in the log."""
    import asyncio
    import logging

    pipeline = _make_hyde_pipeline_mock()
    tool_fn = _get_hyde_tool_fn(tool_name, pipeline, config=_make_config_with_hyde(enabled=False))
    # Looked up after the reload in _get_hyde_tool_fn: create_app imports the class lazily.
    rag_fusion_mod = sys.modules["archon_search.rag_fusion"]
    setattr(pipeline, pipeline_method, AsyncMock(side_effect=rag_fusion_mod.RAGFusionDependencyError(
        f"{rag_fusion_mod.RAG_FUSION_DEPENDENCY_DETAIL} ({_LEAKY_RAG_FUSION_HINT})"
    )))

    with caplog.at_level(logging.WARNING, logger="archon_search.server.mcp"):
        result = asyncio.run(tool_fn(query="test query", rag_fusion=True, **kwargs))

    assert result == {"error": "Install archon-search[rag_fusion] to use RAG Fusion", "code": "validation_error"}
    assert _LEAKY_RAG_FUSION_HINT in caplog.text


def _schema_drift(*_args: object, **_kwargs: object) -> None:
    """Raise a real pydantic ValidationError whose text embeds a would-be leaked value."""
    from pydantic import TypeAdapter

    TypeAdapter(int).validate_python("LEAKED-INPUT-VALUE")


@pytest.mark.parametrize("kwargs", [{"collection": "col1"}, {"collections": ["col1", "col2"]}])
def test_mcp_explain_response_schema_drift_does_not_echo_validation_text(kwargs: dict) -> None:
    """G3: a response-schema ValidationError answers fixed text, like search / search_with_context."""
    import asyncio
    from unittest.mock import patch

    pipeline = _make_hyde_pipeline_mock()
    tool_fn = _get_hyde_tool_fn("explain", pipeline, config=_make_config_with_hyde(enabled=False))
    explain_response = sys.modules["archon_search.server.mcp"].ExplainResponse

    with patch.object(explain_response, "from_pipeline_result", side_effect=_schema_drift):
        result = asyncio.run(tool_fn(query="test query", **kwargs))

    assert result == {"error": "explain response failed schema validation", "code": "schema_validation_error"}


_SLOW_HYDE_SECONDS = 0.3
_SLOW_HYDE_MS = _SLOW_HYDE_SECONDS * 1000.0


def test_mcp_explain_stage_total_excludes_hyde_time() -> None:
    """C2-T-6: ``stage_timings_ms.total`` covers retrieval, not HyDE's LLM call."""
    import asyncio

    async def _slow_generate(*_args: object, **_kwargs: object) -> list[float]:
        await asyncio.sleep(_SLOW_HYDE_SECONDS)
        return [0.5] * 5

    pipeline = _make_hyde_pipeline_mock()
    config = _make_config_with_hyde(enabled=True)
    config.observability.stage_timings_enabled = True
    gen = _make_hyde_generator_mock()
    gen.generate = _slow_generate
    tool_fn = _get_hyde_tool_fn("explain", pipeline, config=config, hyde_generator=gen)

    result = asyncio.run(tool_fn(query="test query", collection="col1", hyde=True))

    assert result["hyde_applied"] is True, result
    assert 0 <= result["stage_timings_ms"]["total"] < _SLOW_HYDE_MS


_ROUTING_VECTOR = [0.1, 0.2]
_OTHER_MODEL = "other/collection-model"


def _routed_explain(*, chosen_model: str, hyde: bool, hyde_vector: list[float] | None = None, graph_mode: str | None = None):
    """Run a collectionless (auto-routed) MCP explain over one collection on *chosen_model*."""
    import asyncio

    from archon_search.collection_meta import CollectionMeta

    pipeline = _make_hyde_pipeline_mock()
    pipeline.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name="col1", active_embedding_model=chosen_model)]
    )
    embedders: dict[str, MagicMock] = {}

    async def _get_or_load(model: str) -> MagicMock:
        return embedders.setdefault(model, MagicMock(name=f"embedder-{model}"))

    embedder_cache = MagicMock(get_or_load=_get_or_load)
    gen = _make_hyde_generator_mock()
    gen.generate = AsyncMock(return_value=hyde_vector)  # None: HyDE requested but failed
    import importlib

    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(
        pipeline, "col1", config=_make_config_with_hyde(enabled=True),
        embedder_cache=embedder_cache, hyde_generator=gen,
    )
    result = asyncio.run(app._tools["explain"](query="test query", hyde=hyde, graph_mode=graph_mode))
    return pipeline, gen, embedders, result


def test_mcp_explain_routes_on_the_plain_query_and_reuses_that_vector_without_hyde() -> None:
    """Routing embeds the plain query once; on the global model that vector also drives retrieval."""
    pipeline, gen, _embedders, result = _routed_explain(chosen_model="test-model", hyde=False)

    assert result["routing"]["chosen_collection"] == "col1", result
    pipeline._global_embedder.embed_one.assert_awaited_once_with("test query")
    gen.generate.assert_not_awaited()
    assert pipeline.explain.await_args.kwargs["query_vector"] == _ROUTING_VECTOR


def test_mcp_explain_does_not_reuse_the_routing_vector_for_a_collection_on_another_model() -> None:
    """The routing vector is in the global model's space — useless to a collection on another model."""
    pipeline, _gen, embedders, _result = _routed_explain(chosen_model=_OTHER_MODEL, hyde=False)

    explain_kwargs = pipeline.explain.await_args.kwargs
    assert explain_kwargs["query_vector"] is None
    assert explain_kwargs["embedder"] is embedders[_OTHER_MODEL]


def test_mcp_explain_routed_hyde_embeds_with_the_chosen_collections_embedder() -> None:
    """Routing still runs on the plain query; HyDE then embeds with the routed collection's embedder."""
    pipeline, gen, embedders, result = _routed_explain(chosen_model=_OTHER_MODEL, hyde=True, hyde_vector=[0.9] * 5)

    pipeline._global_embedder.embed_one.assert_awaited_once_with("test query")
    assert gen.generate.await_args.kwargs["embedder"] is embedders[_OTHER_MODEL]
    assert pipeline.explain.await_args.kwargs["query_vector"] == [0.9] * 5
    assert result["hyde_applied"] is True


def test_mcp_explain_reuses_the_routing_vector_when_hyde_was_requested_but_failed() -> None:
    """C2-T-5: a failed HyDE on the global model falls back to the routing vector, not a re-embed."""
    pipeline, gen, _embedders, result = _routed_explain(chosen_model="test-model", hyde=True)

    gen.generate.assert_awaited_once()
    assert result["hyde_applied"] is False, result
    pipeline._global_embedder.embed_one.assert_awaited_once_with("test query")
    assert pipeline.explain.await_args.kwargs["query_vector"] == _ROUTING_VECTOR


def test_mcp_explain_does_not_reuse_the_routing_vector_with_graph_mode() -> None:
    """C2-T-5: graph-mode retrieval takes no query vector, so the routing vector is not handed over."""
    pipeline, gen, _embedders, result = _routed_explain(chosen_model="test-model", hyde=False, graph_mode="naive")

    assert "error" not in result, result
    assert pipeline.explain.await_args.kwargs["graph_mode"] == "naive"
    assert pipeline.explain.await_args.kwargs["query_vector"] is None
    gen.generate.assert_not_awaited()


_LEAKY_SEARCH_ERROR = "Invalid input, No vector column found to match with the query vector dimension: 384"


@pytest.mark.parametrize(
    ("tool_name", "pipeline_method", "kwargs"),
    [
        ("search", "search", {"collection": "col1"}),
        ("search", "search_many", {"collections": ["col1", "col2"]}),
        ("search_with_context", "search_with_context", {"collection": "col1"}),
    ],
)
def test_mcp_search_internal_error_does_not_leak_exception_text(
    tool_name: str, pipeline_method: str, kwargs: dict
) -> None:
    """An unexpected pipeline failure must answer a fixed message, never ``str(exc)``."""
    import asyncio

    pipeline = _make_hyde_pipeline_mock()
    setattr(pipeline, pipeline_method, AsyncMock(side_effect=ValueError(_LEAKY_SEARCH_ERROR)))
    tool_fn = _get_hyde_tool_fn(tool_name, pipeline, config=_make_config_with_hyde(enabled=False))

    result = asyncio.run(tool_fn(query="test query", **kwargs))

    assert result == {"error": "search failed", "code": "internal_error"}


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
    pipeline.context_window_max = DEFAULT_CONTEXT_WINDOW_MAX
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
    # The public property and the private attribute are one object on a real pipeline (C2-T-9).
    pipeline.global_embedder = pipeline._global_embedder
    pipeline.search = AsyncMock(return_value=search_result)
    pipeline.search_many = AsyncMock(return_value=search_many_result)
    pipeline.warmup_models = AsyncMock(return_value=True)
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
    config.max_fanout = 8
    config.top_k_max = 100
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
    """search tool with rag_fusion=True skips HyDE: generate NOT called."""
    import asyncio

    pipeline = _make_rag_fusion_pipeline_mock()
    config = _make_config_with_rag_fusion(enabled=True)
    config.hyde.enabled = True  # only the rag_fusion suppression may keep HyDE off
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

    result = asyncio.run(tool_fn(query="test query", collection="col1", rag_fusion=True, hyde=True))

    # HyDE must NOT have been generated when rag_fusion=True
    hyde_gen.generate.assert_not_awaited()
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
    # SearchWithContextResponse exposes results, hyde_applied, expansion_used, expansion_warning
    assert set(result.keys()) == {"results", "hyde_applied", "expansion_used", "expansion_warning"}
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
    """search_with_context with rag_fusion=True skips HyDE: generate NOT called."""
    import asyncio

    pipeline = _make_rag_fusion_pipeline_mock()
    config = _make_config_with_rag_fusion(enabled=True)
    config.hyde.enabled = True  # only the rag_fusion suppression may keep HyDE off
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

    result = asyncio.run(tool_fn(query="test query", collection="col1", rag_fusion=True, hyde=True))

    hyde_gen.generate.assert_not_awaited()
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
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=[result],
            acl_filtered=acl_filtered,
            excluded_collections=excluded,
        )
    )
    pipeline.warmup_models = AsyncMock(return_value=True)
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
    assert set(result.keys()) == {"results", "acl_filtered", "excluded_collections", "hyde_applied", "expansion_used", "expansion_warning", "graph_expansion_applied", "ppr_entities_matched"}


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
    pipeline.warmup_models = AsyncMock(return_value=True)

    import importlib
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    app = mcp_mod.create_app(pipeline, "col1")
    tool_fn = app._tools["search"]

    result = asyncio.run(tool_fn(query="hello", collections=["col1", "col2"]))

    assert isinstance(result, dict)
    assert set(result.keys()) == {"results", "acl_filtered", "excluded_collections", "hyde_applied", "expansion_used", "expansion_warning", "graph_expansion_applied", "ppr_entities_matched"}


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
    pipeline.search_many = pipeline.search
    tool_fn = _get_search_tool_fn(pipeline)

    with patch(
        "archon_search.server.mcp.McpSearchResultSchema.from_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn(query="hello", collection="col1"))
        fanout_result = asyncio.run(tool_fn(query="hello", collections=["col1", "col2"]))

    # Fixed text: a pydantic ValidationError embeds the offending values.
    assert result == {"error": "search response failed schema validation", "code": _ERR_SCHEMA}
    assert fanout_result == {"error": "search response failed schema validation", "code": _ERR_SCHEMA}


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
    pipeline.context_window_max = DEFAULT_CONTEXT_WINDOW_MAX
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
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

    assert result == {"error": "search response failed schema validation", "code": _ERR_SCHEMA}


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


def test_list_collections_pipeline_error_returns_internal_error() -> None:
    """list_collections outer exception handler returns internal_error on pipeline failure."""
    import asyncio
    import importlib
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(side_effect=RuntimeError("db down"))
    app = mcp_mod.create_app(pipeline, "col1")
    tool_fn = app._tools["list_collections"]

    result = asyncio.run(tool_fn())

    assert isinstance(result, dict)
    assert result.get("code") == "internal_error"


def test_get_collections_meta_pipeline_error_returns_internal_error() -> None:
    """get_collections_meta outer exception handler returns internal_error on pipeline failure."""
    import asyncio
    import importlib
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(side_effect=RuntimeError("db down"))
    app = mcp_mod.create_app(pipeline, "col1")
    tool_fn = app._tools["get_collections_meta"]

    result = asyncio.run(tool_fn())

    assert isinstance(result, dict)
    assert result.get("code") == "internal_error"


# ---------------------------------------------------------------------------
# Task 2.6 — Migrate get_collection_meta and update_collection tools
# ---------------------------------------------------------------------------


def _get_single_collection_tool_fn(tool_name: str, meta, pipeline_override=None, config=None):
    """Build a stub-backed MCP app for single-collection tools."""
    import importlib
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    if pipeline_override is not None:
        pipeline = pipeline_override
    else:
        pipeline = MagicMock()
        pipeline.get_collection_meta = AsyncMock(return_value=meta)
    app = mcp_mod.create_app(pipeline, "col1", config=config)
    return app._tools[tool_name]


def test_get_collection_meta_excludes_internal_fields() -> None:
    """get_collection_meta must exclude all internal CollectionMeta fields."""
    import asyncio

    meta = _make_full_collection_meta()
    tool_fn = _get_single_collection_tool_fn("get_collection_meta", meta)
    result = asyncio.run(tool_fn(name="col1"))

    assert isinstance(result, dict)
    for field in _INTERNAL_FIELDS:
        assert field not in result, f"internal field {field!r} must not appear in get_collection_meta output"
    assert "description_embedding" not in result


def test_get_collection_meta_renames_active_embedding_model() -> None:
    """get_collection_meta must return embedding_model (not active_embedding_model)."""
    import asyncio

    meta = _make_full_collection_meta()
    tool_fn = _get_single_collection_tool_fn("get_collection_meta", meta)
    result = asyncio.run(tool_fn(name="col1"))

    assert isinstance(result, dict)
    assert "embedding_model" in result
    assert result["embedding_model"] == "text-embed-v2"
    assert "active_embedding_model" not in result


def test_get_collection_meta_schema_drift_returns_schema_validation_error() -> None:
    """get_collection_meta returns schema_validation_error when from_result raises ValidationError."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError

    from archon_search.server.mcp import _ERR_SCHEMA
    from archon_search.server.mcp_schemas import CollectionDetailSchema

    try:
        CollectionDetailSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    meta = _make_full_collection_meta()
    tool_fn = _get_single_collection_tool_fn("get_collection_meta", meta)

    with patch(
        "archon_search.server.mcp.CollectionDetailSchema.from_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn(name="col1"))

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA


def test_update_collection_excludes_internal_fields() -> None:
    """update_collection must exclude all internal CollectionMeta fields on success path."""
    import asyncio
    from unittest.mock import patch

    meta = _make_full_collection_meta()

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)
    store.count_chunks = AsyncMock(return_value=0)
    store.update_collection_meta = AsyncMock()

    pipeline = MagicMock()
    pipeline.store = store

    tool_fn = _get_single_collection_tool_fn("update_collection", meta, pipeline_override=pipeline)

    ctx = MagicMock()
    ctx.meta = {"namespace": "default"}

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ):
        result = asyncio.run(tool_fn(collection_name="col1", embedding_model="new-model", ctx=ctx))

    assert isinstance(result, dict)
    for field in _INTERNAL_FIELDS:
        assert field not in result, f"internal field {field!r} must not appear in update_collection output"


def test_update_collection_no_op_same_model() -> None:
    """update_collection no-op path returns CollectionDetailSchema shape (no internal fields)."""
    import asyncio
    from unittest.mock import patch

    meta = _make_full_collection_meta()  # active_embedding_model="text-embed-v2"

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()

    pipeline = MagicMock()
    pipeline.store = store

    tool_fn = _get_single_collection_tool_fn("update_collection", meta, pipeline_override=pipeline)

    ctx = MagicMock()
    ctx.meta = {"namespace": "default"}

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ):
        # Request the same model that is already active -> no-op
        result = asyncio.run(tool_fn(collection_name="col1", embedding_model="text-embed-v2", ctx=ctx))

    assert isinstance(result, dict)
    assert "error" not in result
    # Must contain public fields
    assert "name" in result
    assert "embedding_model" in result
    # Must not contain internal fields
    for field in _INTERNAL_FIELDS:
        assert field not in result


def test_update_collection_pending_model_path() -> None:
    """update_collection pending path returns CollectionDetailSchema shape with pending_embedding_model."""
    import asyncio
    from unittest.mock import patch

    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(
        name="col1",
        active_embedding_model="old-model",
        pending_embedding_model=None,
        needs_reindex=False,
        reindex_job_id=None,
        centroid=[0.1, 0.2],
        centroid_sum=[0.3, 0.4],
        mutations_since_recompute=2,
        needs_recompute=False,
        namespace="default",
    )

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)
    store.count_chunks = AsyncMock(return_value=10)  # has chunks -> pending path
    store.update_collection_meta = AsyncMock()

    pipeline = MagicMock()
    pipeline.store = store

    tool_fn = _get_single_collection_tool_fn("update_collection", meta, pipeline_override=pipeline)

    ctx = MagicMock()
    ctx.meta = {"namespace": "default"}

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ):
        result = asyncio.run(tool_fn(collection_name="col1", embedding_model="new-model", ctx=ctx))

    assert isinstance(result, dict)
    assert "error" not in result
    # Schema fields present
    assert result.get("pending_embedding_model") == "new-model"
    assert "embedding_model" in result
    # No internal fields
    for field in _INTERNAL_FIELDS:
        assert field not in result


def test_update_collection_schema_drift_returns_schema_validation_error() -> None:
    """update_collection returns schema_validation_error when CollectionDetailSchema.from_result raises."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError

    from archon_search.server.mcp import _ERR_SCHEMA
    from archon_search.server.mcp_schemas import CollectionDetailSchema

    try:
        CollectionDetailSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    meta = _make_full_collection_meta()

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)
    store.count_chunks = AsyncMock(return_value=0)
    store.update_collection_meta = AsyncMock()

    pipeline = MagicMock()
    pipeline.store = store

    tool_fn = _get_single_collection_tool_fn("update_collection", meta, pipeline_override=pipeline)

    ctx = MagicMock()
    ctx.meta = {"namespace": "default"}

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ), patch(
        "archon_search.server.mcp.CollectionDetailSchema.from_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn(collection_name="col1", embedding_model="new-model", ctx=ctx))

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA


# ---------------------------------------------------------------------------
# Task 2.7 — Migrate list_documents and delete_document tools
# ---------------------------------------------------------------------------


def _make_document_info():
    """Return a minimal DocumentInfo for list_documents tests."""
    from archon_search._types import DocumentInfo

    return DocumentInfo(
        doc_id="doc1",
        source_path="/path/file.md",
        chunk_count=5,
        indexed_at="2024-01-01T00:00:00.000000Z",
    )


def _get_list_documents_tool_fn(doc_infos):
    """Build a stub-backed MCP app and return the list_documents tool function."""
    import importlib
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    pipeline = MagicMock()
    pipeline.list_documents = AsyncMock(return_value=(doc_infos, None, len(doc_infos)))
    app = mcp_mod.create_app(pipeline, "col1")
    return app._tools["list_documents"]


def _get_delete_document_tool_fn_v2(delete_return=1, delete_side_effect=None):
    """Build a stub-backed MCP app and return the delete_document tool function."""
    import importlib
    import archon_search.server.mcp as mcp_mod

    importlib.reload(mcp_mod)
    pipeline = MagicMock()
    pipeline.delete_document = AsyncMock(
        return_value=delete_return if delete_side_effect is None else None,
        side_effect=delete_side_effect,
    )
    app = mcp_mod.create_app(pipeline, "col1")
    return app._tools["delete_document"]


def test_list_documents_returns_document_info_schema_shape() -> None:
    """list_documents returns list of dicts with DocumentInfoSchema fields."""
    import asyncio

    doc = _make_document_info()
    tool_fn = _get_list_documents_tool_fn([doc])

    result = asyncio.run(tool_fn(collection="col1"))

    assert isinstance(result, list)
    assert len(result) == 1
    item = result[0]
    assert item["doc_id"] == "doc1"
    assert item["source_path"] == "/path/file.md"
    assert item["chunk_count"] == 5
    assert item["indexed_at"] == "2024-01-01T00:00:00.000000Z"


def test_delete_document_returns_delete_schema_shape() -> None:
    """delete_document returns a dict with a 'deleted' key."""
    import asyncio

    tool_fn = _get_delete_document_tool_fn_v2(delete_return=3)

    result = asyncio.run(tool_fn(doc_id="doc1", collection="col1"))

    assert isinstance(result, dict)
    assert "deleted" in result
    assert result["deleted"] == 3


def test_list_documents_schema_drift_returns_schema_validation_error() -> None:
    """list_documents returns schema_validation_error when from_result raises ValidationError."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError

    from archon_search.server.mcp import _ERR_SCHEMA
    from archon_search.server.mcp_schemas import DocumentInfoSchema

    try:
        DocumentInfoSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    doc = _make_document_info()
    tool_fn = _get_list_documents_tool_fn([doc])

    with patch(
        "archon_search.server.mcp.DocumentInfoSchema.from_result",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn(collection="col1"))

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA


def test_delete_document_schema_drift_returns_schema_validation_error() -> None:
    """delete_document returns schema_validation_error when DeleteDocumentSchema raises ValidationError."""
    import asyncio
    from unittest.mock import patch

    from pydantic import ValidationError

    from archon_search.server.mcp import _ERR_SCHEMA
    from archon_search.server.mcp_schemas import DeleteDocumentSchema

    try:
        DeleteDocumentSchema.model_validate({"bad": 1})
    except ValidationError as e:
        _fake_err = e

    tool_fn = _get_delete_document_tool_fn_v2(delete_return=1)

    with patch(
        "archon_search.server.mcp.DeleteDocumentSchema",
        side_effect=_fake_err,
    ):
        result = asyncio.run(tool_fn(doc_id="doc1", collection="col1"))

    assert isinstance(result, dict)
    assert result.get("code") == _ERR_SCHEMA
