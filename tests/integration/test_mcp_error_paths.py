"""Task 3.1 — MCP search/explain validation and typed-exception mapping.

Exercises MCP tool error paths with real pipelines:
- Both collection/collections set → validation_error
- Nonexistent collection → not_found
- FanoutTimeoutError from pipeline → timeout code
- explain rerank=False + multi-collection → validation_error
- HyDE dependency absent → error with dependency message
- RAG fusion dependency absent → error with dependency message

Run with:
    uv run pytest tests/integration/test_mcp_error_paths.py -v
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.constants import DEFAULT_CONTEXT_WINDOW_MAX

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# FastMCP stand-in, installed per app build by a scoped patch() in _make_mcp_app
# (same pattern as tests/server/test_mcp_error_responses.py).
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, name: str) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            return func

        return decorator

    def custom_route(self, path: str, methods: list[str] | None = None) -> Any:
        def decorator(func: Any) -> Any:
            return func

        return decorator


class _FakeFastMCP:
    def __new__(cls, name: str, **kwargs: Any) -> _FakeApp:  # type: ignore[misc]
        return _FakeApp(name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mcp_app(
    pipeline: Any,
    *,
    hyde_generator: Any = None,
    rag_fusion_generator: Any = None,
    config: Any = None,
) -> _FakeApp:
    """Create an MCP app with a real-ish pipeline and stub FastMCP."""
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        return mcp_module.create_app(  # type: ignore[call-arg]
            pipeline,
            "default",
            writer=None,
            config=config,
            hyde_generator=hyde_generator,
            rag_fusion_generator=rag_fusion_generator,
        )


def _make_pipeline_with_search_many_raising(exc: Exception) -> MagicMock:
    """Return a mock pipeline whose search_many raises exc."""
    pipeline = MagicMock()
    pipeline.search_many = AsyncMock(side_effect=exc)
    pipeline.warmup_models = AsyncMock(return_value=True)
    # _global_embedder is accessed internally; stub it out
    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])
    pipeline._global_embedder = embedder
    return pipeline


# ---------------------------------------------------------------------------
# Test 1 — both collection and collections → validation_error
# ---------------------------------------------------------------------------


async def test_mcp_search_both_collection_fields_returns_validation_error() -> None:
    """MCP search with both collection= and collections= returns validation_error.

    The tool enforces mutual exclusivity — supplying both is a client error.
    """
    pipeline = MagicMock()
    app = _make_mcp_app(pipeline)

    result = await app.tools["search"](
        query="test query",
        collection="col-a",
        collections=["col-a", "col-b"],
    )

    assert isinstance(result, dict), f"expected dict, got {type(result)}"
    assert result.get("code") == "validation_error", f"expected validation_error, got: {result}"
    assert "error" in result
    # The error should mention mutual exclusivity
    error_msg = result["error"].lower()
    assert any(
        kw in error_msg for kw in ("both", "either", "not both", "supply")
    ), f"expected mutual-exclusion message, got: {result['error']!r}"


# ---------------------------------------------------------------------------
# Test 2 — nonexistent collection → not_found
# ---------------------------------------------------------------------------


async def test_mcp_search_missing_collection_returns_not_found_code() -> None:
    """MCP search with a nonexistent collection returns is_error with not_found code.

    The multi-collection fan-out path explicitly maps CollectionNotFoundError to
    code='not_found'. The single-collection path falls through to the generic
    Exception handler (code='internal_error') — that is a separate, expected behavior
    difference and is not tested here. This test targets the explicit not_found mapping.
    """
    from archon_search.pipeline import CollectionNotFoundError

    pipeline = _make_pipeline_with_search_many_raising(CollectionNotFoundError("ghost"))

    app = _make_mcp_app(pipeline)

    result = await app.tools["search"](
        query="test query",
        collections=["ghost-collection"],
    )

    assert isinstance(result, dict), f"expected dict, got {type(result)}"
    assert result.get("code") == "not_found", (
        f"expected not_found for nonexistent collection, got: {result}"
    )
    error_text = result.get("error", "").lower()
    assert "not found" in error_text, (
        f"expected 'not found' in error message, got: {result.get('error')!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — FanoutTimeoutError → timeout code
# ---------------------------------------------------------------------------


async def test_mcp_search_fanout_timeout_returns_timeout_code() -> None:
    """FanoutTimeoutError from pipeline.search_many is mapped to code='timeout'.

    Monkeypatches pipeline.search_many to raise FanoutTimeoutError. Calls
    MCP search with collections=[...] to trigger the multi-collection fan-out
    path. Asserts the returned dict has code='timeout'.
    """
    from archon_search.pipeline import FanoutTimeoutError

    pipeline = _make_pipeline_with_search_many_raising(FanoutTimeoutError())

    app = _make_mcp_app(pipeline)

    result = await app.tools["search"](
        query="test fanout query",
        collections=["col-a", "col-b"],
    )

    assert isinstance(result, dict), f"expected dict, got {type(result)}"
    assert result.get("code") == "timeout", f"expected timeout code, got: {result}"
    assert "error" in result
    error_text = result["error"].lower()
    assert "timeout" in error_text or "timed out" in error_text, (
        f"expected timeout in error message, got: {result['error']!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — explain rerank=False with 2 collections → validation_error
# ---------------------------------------------------------------------------


async def test_mcp_explain_rerank_false_multi_collections_returns_error() -> None:
    """MCP explain with rerank=False and two collections returns validation_error.

    The MCP explain tool explicitly blocks rerank=False on multi-collection
    queries because cross-collection score normalization requires reranking.
    """
    pipeline = MagicMock()
    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])
    pipeline._global_embedder = embedder

    app = _make_mcp_app(pipeline)

    result = await app.tools["explain"](
        query="test explain query",
        collections=["col-a", "col-b"],
        rerank=False,
    )

    assert isinstance(result, dict), f"expected dict, got {type(result)}"
    assert result.get("code") == "validation_error", (
        f"expected validation_error for rerank=False+multi-collection, got: {result}"
    )
    assert "error" in result
    error_msg = result["error"].lower()
    assert any(
        kw in error_msg for kw in ("rerank", "reranking", "multi-collection", "disabled")
    ), f"expected rerank/multi-collection constraint message, got: {result['error']!r}"


# ---------------------------------------------------------------------------
# Test 5 — search_with_context + hyde=True, dependency absent → error
# ---------------------------------------------------------------------------


async def test_mcp_search_with_context_hyde_dependency_absent_returns_error() -> None:
    """MCP search_with_context with hyde=True and missing anthropic dep returns error.

    Monkeypatches a hyde_generator.generate() to raise HyDEDependencyError (mimicking
    the behavior of HyDEGenerator when the anthropic package is absent).
    The MCP tool catches it and returns the fixed message with code='validation_error'.
    """
    from archon_search.hyde import HYDE_DEPENDENCY_DETAIL

    pipeline = MagicMock()
    pipeline.context_window_max = DEFAULT_CONTEXT_WINDOW_MAX
    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])
    pipeline._global_embedder = embedder
    # HyDE resolves after the collection lookup: it embeds with the collection's embedder.
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.warmup_models = AsyncMock(return_value=True)

    # Mock a hyde_generator whose generate() raises HyDEDependencyError (dependency
    # absent) — the class mcp.py itself catches, whatever reloads ran before.
    from archon_search.server import mcp as mcp_module

    hyde_generator = MagicMock()
    hyde_generator.generate = AsyncMock(
        side_effect=mcp_module.HyDEDependencyError(
            "Install archon-search[hyde] to use HyDE (pip install 'archon-search[hyde]')"
        )
    )

    # hyde config must have enabled=True for RequestHyDE.resolve to call generate()
    from archon_search.config import HyDEConfig

    config = MagicMock()
    config.hyde = HyDEConfig(enabled=True)
    config.rag_fusion = MagicMock()
    config.rag_fusion.enabled = False
    config.observability = MagicMock()
    config.observability.stage_timings_enabled = False
    config.embedding_model = ""

    app = _make_mcp_app(pipeline, hyde_generator=hyde_generator, config=config)

    result = await app.tools["search_with_context"](
        query="test query",
        collection="col",
        hyde=True,
    )

    assert isinstance(result, dict), f"expected dict, got {type(result)}"
    assert result.get("code") == "validation_error", (
        f"expected validation_error for missing HyDE dependency, got: {result}"
    )
    assert result["error"] == HYDE_DEPENDENCY_DETAIL


# ---------------------------------------------------------------------------
# Test 6 — search + rag_fusion=True, dependency absent → error
# ---------------------------------------------------------------------------


async def test_mcp_search_rag_fusion_dependency_absent_returns_error() -> None:
    """MCP search with rag_fusion=True and missing anthropic dep returns error.

    Monkeypatches pipeline.search to raise RAGFusionDependencyError (the error
    raised when the anthropic package is absent for RAG fusion). Asserts the
    MCP tool catches it and returns code='validation_error'.
    """
    from archon_search.rag_fusion import RAG_FUSION_DEPENDENCY_DETAIL, RAGFusionDependencyError

    pipeline = MagicMock()
    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])
    pipeline._global_embedder = embedder
    # Namespace gate calls await pipeline.get_collection_meta(); must return a truthy value
    # so the gate passes and the test can exercise the RAGFusionDependencyError path.
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.search = AsyncMock(
        side_effect=RAGFusionDependencyError(
            "Install archon-search[rag-fusion] to use RAG Fusion"
        )
    )
    pipeline.warmup_models = AsyncMock(return_value=True)

    app = _make_mcp_app(pipeline)

    result = await app.tools["search"](
        query="test rag fusion query",
        collection="col",
        rag_fusion=True,
    )

    assert isinstance(result, dict), f"expected dict, got {type(result)}"
    assert result.get("code") == "validation_error", (
        f"expected validation_error for missing RAG Fusion dependency, got: {result}"
    )
    assert result["error"] == RAG_FUSION_DEPENDENCY_DETAIL
