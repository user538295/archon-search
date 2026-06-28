"""tests/server/test_mcp_search_be4_multi_collection_filters.py

BE-4 unit tests: Remove MCP language restriction; build SearchFilters for
multi-collection path; pass to search_many().

Plan task: BE-4 — Remove MCP language restriction; build `SearchFilters` for
multi-collection path; pass to `search_many()` #backend-role

Tests:
- test_mcp_search_multi_collection_language_filter_no_longer_rejected
- test_mcp_search_multi_collection_file_type_filter_passed_to_pipeline
- test_mcp_search_multi_collection_no_filters_passes_none
- test_mcp_search_multi_collection_all_filter_params_forwarded
"""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# Resolve fastmcp the same way sibling tests do.
if "fastmcp" not in sys.modules:
    try:
        import fastmcp as _real_fastmcp  # type: ignore[import-not-found]
        sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]
    except ImportError:
        _fastmcp = types.ModuleType("fastmcp")
        _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _fastmcp

from archon_search.pipeline import SearchPipelineResult


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


def _make_pipeline(*, result: SearchPipelineResult | None = None) -> Any:
    """Return a pipeline mock with search_many returning the given result."""
    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))
    pipeline.search_many = AsyncMock(
        return_value=result or SearchPipelineResult(results=[], acl_filtered=False)
    )
    return pipeline


# ---------------------------------------------------------------------------
# test_mcp_search_multi_collection_language_filter_no_longer_rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_multi_collection_language_filter_no_longer_rejected() -> None:
    """MCP search tool with collections + language MUST NOT return validation_error.

    Previously (pre-BE-4) the tool returned:
        {"code": "validation_error", "error": "language filter is not supported ..."}
    After BE-4 this restriction is lifted — the tool must call search_many() and
    return a non-error response.
    """
    import archon_search.server.mcp as mcp_module

    pipeline = _make_pipeline()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        fn = app.tools["search"]
        result = await fn(query="hello", collections=["col1", "col2"], language="fr")

    # Must NOT be a validation_error
    assert isinstance(result, dict)
    assert result.get("code") != "validation_error", (
        f"Expected language filter to be accepted in multi-collection search, "
        f"but got validation_error: {result!r}"
    )
    # search_many must have been called (not rejected before fanout)
    pipeline.search_many.assert_awaited_once()


# ---------------------------------------------------------------------------
# test_mcp_search_multi_collection_file_type_filter_passed_to_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_multi_collection_file_type_filter_passed_to_pipeline() -> None:
    """Mock pipeline confirms search_many() is called with filters.file_type set."""
    from archon_search.filters import SearchFilters
    import archon_search.server.mcp as mcp_module

    pipeline = _make_pipeline()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        fn = app.tools["search"]
        await fn(query="hello", collections=["col1", "col2"], file_type=".py")

    pipeline.search_many.assert_awaited_once()
    kwargs = pipeline.search_many.await_args.kwargs
    assert "filters" in kwargs, f"search_many not called with filters kwarg; kwargs={kwargs}"
    filters = kwargs["filters"]
    assert isinstance(filters, SearchFilters), (
        f"Expected SearchFilters, got {type(filters)}"
    )
    assert filters.file_type == "py", (  # dot-normalised
        f"Expected file_type='py' (normalised from '.py'), got {filters.file_type!r}"
    )


# ---------------------------------------------------------------------------
# test_mcp_search_multi_collection_no_filters_passes_none
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_multi_collection_no_filters_passes_none() -> None:
    """When no filter args are passed, search_many() must be called with filters=None.

    This ensures applied_filters is null when no filters were submitted.
    """
    import archon_search.server.mcp as mcp_module

    pipeline = _make_pipeline()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        fn = app.tools["search"]
        await fn(query="hello", collections=["col1", "col2"])

    pipeline.search_many.assert_awaited_once()
    kwargs = pipeline.search_many.await_args.kwargs
    assert "filters" in kwargs, f"search_many not called with filters kwarg; kwargs={kwargs}"
    assert kwargs["filters"] is None, (
        f"Expected filters=None when no filter args supplied, got {kwargs['filters']!r}"
    )


# ---------------------------------------------------------------------------
# test_mcp_search_multi_collection_all_filter_params_forwarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_multi_collection_all_filter_params_forwarded() -> None:
    """All 6 filter params (file_type, source_path_prefix, source_path_glob,
    indexed_after, indexed_before, language) must be forwarded to search_many().

    This catches the pre-BE-4 silent data loss bug where all 6 filter types
    were dropped for multi-collection MCP search.
    """
    from archon_search.filters import SearchFilters
    import archon_search.server.mcp as mcp_module

    pipeline = _make_pipeline()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        fn = app.tools["search"]
        await fn(
            query="hello",
            collections=["col1", "col2"],
            file_type=".md",
            source_path_prefix="/docs/",
            source_path_glob="*/docs/*.md",
            indexed_after="2024-01-01",
            indexed_before="2025-12-31",
            language="en",
        )

    pipeline.search_many.assert_awaited_once()
    kwargs = pipeline.search_many.await_args.kwargs
    assert "filters" in kwargs, f"search_many not called with filters kwarg; kwargs={kwargs}"
    filters = kwargs["filters"]
    assert isinstance(filters, SearchFilters), (
        f"Expected SearchFilters, got {type(filters)}"
    )
    # Verify all 6 fields are populated (dot-normalised for file_type)
    assert filters.file_type == "md", f"file_type mismatch: {filters.file_type!r}"
    assert filters.source_path_prefix == "/docs/", (
        f"source_path_prefix mismatch: {filters.source_path_prefix!r}"
    )
    assert filters.source_path_glob == "*/docs/*.md", (
        f"source_path_glob mismatch: {filters.source_path_glob!r}"
    )
    assert filters.indexed_after is not None, "indexed_after must be set"
    assert filters.indexed_before is not None, "indexed_before must be set"
    assert filters.language == "en", f"language mismatch: {filters.language!r}"


# ---------------------------------------------------------------------------
# test_mcp_search_multi_collection_invalid_filter_returns_validation_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_multi_collection_invalid_filter_returns_validation_error() -> None:
    """An invalid filter value with collections must return code='validation_error'.

    The multi-collection path has its own SearchFilters construction + ValidationError
    catch block. This test verifies it is not bypassed (i.e., the exception is caught
    and returned as a structured error, not propagated as an unhandled exception).
    """
    import archon_search.server.mcp as mcp_module

    pipeline = _make_pipeline()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        fn = app.tools["search"]
        # file_type="" is rejected by SearchFilters validator
        result = await fn(query="hello", collections=["col1", "col2"], file_type="")

    assert isinstance(result, dict)
    assert result.get("code") == "validation_error", (
        f"Expected validation_error for empty file_type in multi-collection search, "
        f"got: {result!r}"
    )
    # search_many must NOT have been called — rejected at filter construction
    pipeline.search_many.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_mcp_search_multi_collection_explicit_none_filters_passes_none
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_multi_collection_explicit_none_filters_passes_none() -> None:
    """When all 6 filter params are explicitly passed as None, search_many() must
    receive filters=None (same as omitting them entirely).

    This covers the _any_filter check path where each arg is explicitly None
    (vs just using function parameter defaults).
    """
    import archon_search.server.mcp as mcp_module

    pipeline = _make_pipeline()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        fn = app.tools["search"]
        await fn(
            query="hello",
            collections=["col1", "col2"],
            file_type=None,
            source_path_prefix=None,
            source_path_glob=None,
            indexed_after=None,
            indexed_before=None,
            language=None,
        )

    pipeline.search_many.assert_awaited_once()
    kwargs = pipeline.search_many.await_args.kwargs
    assert "filters" in kwargs, f"search_many not called with filters kwarg; kwargs={kwargs}"
    assert kwargs["filters"] is None, (
        f"Expected filters=None when all filter args explicitly None, got {kwargs['filters']!r}"
    )


# ---------------------------------------------------------------------------
# test_mcp_search_multi_collection_include_metadata_not_in_filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_multi_collection_include_metadata_not_in_filters() -> None:
    """include_metadata is intentionally excluded from the SearchFilters forwarded to
    search_many() in the multi-collection path. It is a presentation-layer projection
    flag handled at response serialization, not a retrieval filter for search_many().

    This test pins the intentional asymmetry so it cannot be accidentally 'fixed'
    by adding include_metadata to the _multi_filters constructor.
    """
    from archon_search.filters import SearchFilters
    import archon_search.server.mcp as mcp_module

    pipeline = _make_pipeline()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        fn = app.tools["search"]
        # Pass include_metadata=True alongside a real filter to trigger _multi_filters construction
        await fn(
            query="hello",
            collections=["col1", "col2"],
            file_type=".md",
            include_metadata=True,
        )

    pipeline.search_many.assert_awaited_once()
    kwargs = pipeline.search_many.await_args.kwargs
    assert "filters" in kwargs, f"search_many not called with filters kwarg; kwargs={kwargs}"
    filters = kwargs["filters"]
    assert isinstance(filters, SearchFilters), (
        f"Expected SearchFilters, got {type(filters)}"
    )
    # include_metadata must NOT be set on the filters forwarded to search_many().
    # It is a presentation-layer flag, handled at response serialization (mcp.py line ~377).
    assert filters.include_metadata is False, (
        f"include_metadata must not be forwarded to search_many() (it is a presentation flag); "
        f"got filters.include_metadata={filters.include_metadata!r}"
    )
