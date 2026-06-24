"""Tests for MCP ``search`` tool metadata suppression (Task 1.3) and
filter forwarding / parity contract (Task 4.2).

Verifies:
- metadata stripped from results when include_metadata=False (default)
- metadata present when include_metadata=True
- language field appears in MCP search output schema
- filter kwargs hydrate SearchFilters and are forwarded to pipeline.search
- invalid filter input surfaces as a structured tool error
- MCP tool input schema is a superset of SearchFilters fields (parity contract)
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# Resolve fastmcp the same way test_mcp_search_with_context.py does.
if "fastmcp" not in sys.modules:
    try:
        import fastmcp as _real_fastmcp  # type: ignore[import-not-found]
        sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]
    except ImportError:
        _fastmcp = types.ModuleType("fastmcp")
        _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _fastmcp

from archon_search._types import SearchResult
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


def _make_result(metadata: dict[str, str] | None = None, language: str = "") -> SearchResult:
    return SearchResult(
        doc_id="d" * 64,
        chunk_id="d" * 64 + "-000001",
        text="some matched text",
        score=0.85,
        source_path="/tmp/doc.md",
        file_type="md",
        language=language,
        metadata=metadata or {},
        ingested_by="cli",
    )


async def _call_mcp_search(pipeline_result: SearchPipelineResult, include_metadata: bool | None = None) -> dict[str, Any]:
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.search = AsyncMock(return_value=pipeline_result)

    with __import__("unittest.mock", fromlist=["patch"]).patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        fn = app.tools["search"]
        if include_metadata is None:
            return await fn(query="hello", collection=None)
        return await fn(query="hello", collection=None, include_metadata=include_metadata)


@pytest.mark.asyncio
async def test_mcp_search_strips_metadata_when_include_metadata_false() -> None:
    """MCP search tool must strip metadata from results when include_metadata is False (default)."""
    result = _make_result(metadata={"k": "v"})
    pipeline_result = SearchPipelineResult(results=[result], acl_filtered=False)

    payload = await _call_mcp_search(pipeline_result)

    assert "results" in payload
    assert len(payload["results"]) == 1
    # empty dict not key-absent, consistent with REST surface
    assert payload["results"][0]["metadata"] == {}
    # Single-collection responses carry the additive excluded_collections envelope (empty).
    assert payload["excluded_collections"] == []


@pytest.mark.asyncio
async def test_mcp_search_includes_metadata_when_include_metadata_true() -> None:
    """MCP search tool must include metadata when include_metadata=True."""
    result = _make_result(metadata={"k": "v"})
    pipeline_result = SearchPipelineResult(results=[result], acl_filtered=False)

    payload = await _call_mcp_search(pipeline_result, include_metadata=True)

    assert "results" in payload
    assert len(payload["results"]) == 1
    assert payload["results"][0]["metadata"] == {"k": "v"}


@pytest.mark.asyncio
async def test_mcp_search_tool_schema_advertises_language_field() -> None:
    """MCP search results must include language field."""
    result = _make_result(language="en")
    pipeline_result = SearchPipelineResult(results=[result], acl_filtered=False)

    # include_metadata=True so we get the full result dict
    payload = await _call_mcp_search(pipeline_result, include_metadata=True)

    assert "results" in payload
    assert len(payload["results"]) == 1
    assert "language" in payload["results"][0]
    assert payload["results"][0]["language"] == "en"


# ---------------------------------------------------------------------------
# Task 4.2 — filter forwarding, validation error surface, parity contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_forwards_filters_to_pipeline() -> None:
    """All filter kwargs hydrate a SearchFilters instance and reach pipeline.search."""
    from archon_search.filters import SearchFilters
    import archon_search.server.mcp as mcp_module

    result = _make_result(metadata={"k": "v"})
    pipeline_result = SearchPipelineResult(results=[result], acl_filtered=False)
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.search = AsyncMock(return_value=pipeline_result)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        fake_app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = fake_app.tools["search"]
        await fn(
            query="hello",
            collection=None,
            file_type="md",
            source_path_prefix="/docs/",
            source_path_glob="*.md",
            indexed_after="2025-01-01",
            indexed_before="2025-12-31",
            include_metadata=True,
        )

    pipeline.search.assert_called_once()
    _args, kwargs = pipeline.search.call_args
    assert "filters" in kwargs
    filters_obj = kwargs["filters"]
    assert isinstance(filters_obj, SearchFilters), f"Expected SearchFilters, got {type(filters_obj)}"
    assert filters_obj.file_type == "md"
    assert filters_obj.source_path_prefix == "/docs/"
    assert filters_obj.source_path_glob == "*.md"
    assert filters_obj.indexed_after is not None
    assert filters_obj.indexed_before is not None
    assert filters_obj.include_metadata is True


@pytest.mark.asyncio
async def test_mcp_search_with_context_forwards_filters_to_pipeline() -> None:
    """Filter kwargs reach pipeline.search_with_context as a SearchFilters instance."""
    from archon_search.filters import SearchFilters
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    from archon_search.pipeline import SearchPipelineResult, SearchWithContextResult
    pipeline.search_with_context = AsyncMock(return_value=SearchWithContextResult(results=[], pipeline_result=SearchPipelineResult(results=[], acl_filtered=False)))

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        fake_app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = fake_app.tools["search_with_context"]
        await fn(
            query="hello",
            collection=None,
            file_type="pdf",
            source_path_prefix="/reports/",
            indexed_after="2024-06-01",
        )

    pipeline.search_with_context.assert_called_once()
    _args, kwargs = pipeline.search_with_context.call_args
    assert "filters" in kwargs
    filters_obj = kwargs["filters"]
    assert isinstance(filters_obj, SearchFilters), f"Expected SearchFilters, got {type(filters_obj)}"
    assert filters_obj.file_type == "pdf"
    assert filters_obj.source_path_prefix == "/reports/"
    assert filters_obj.indexed_after is not None


@pytest.mark.asyncio
async def test_mcp_search_invalid_filter_surfaces_validator_error() -> None:
    """An invalid filter value must return a structured error dict, not raise."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        fake_app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = fake_app.tools["search"]
        # empty file_type is rejected by the validator
        result = await fn(query="hello", collection=None, file_type="")

    assert isinstance(result, dict)
    assert "code" in result
    assert result["code"] == "validation_error"
    assert "error" in result


@pytest.mark.asyncio
async def test_mcp_search_with_context_invalid_filter_surfaces_validator_error() -> None:
    """search_with_context also surfaces SearchFilters validation errors."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    from archon_search.pipeline import SearchPipelineResult, SearchWithContextResult
    pipeline.search_with_context = AsyncMock(return_value=SearchWithContextResult(results=[], pipeline_result=SearchPipelineResult(results=[], acl_filtered=False)))

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        fake_app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = fake_app.tools["search_with_context"]
        # indexed_after > indexed_before is rejected
        result = await fn(
            query="hello",
            collection=None,
            indexed_after="2025-12-31",
            indexed_before="2025-01-01",
        )

    assert isinstance(result, dict)
    assert "code" in result
    assert result["code"] == "validation_error"


@pytest.mark.asyncio
async def test_mcp_search_suppresses_metadata_when_include_metadata_false() -> None:
    """Fake pipeline returns non-empty metadata; MCP must return results without the metadata key value."""
    result = _make_result(metadata={"secret": "value"})
    pipeline_result = SearchPipelineResult(results=[result], acl_filtered=False)

    payload = await _call_mcp_search(pipeline_result, include_metadata=False)

    assert "results" in payload
    assert len(payload["results"]) == 1
    # Task spec says pop the key; current impl sets to {}; both satisfy "suppressed"
    # assert key absent OR value is empty dict
    r = payload["results"][0]
    assert r.get("metadata", None) in ({}, None), (
        f"metadata not suppressed: {r.get('metadata')!r}"
    )


@pytest.mark.asyncio
async def test_mcp_search_includes_metadata_when_include_metadata_true_task42() -> None:
    """When include_metadata=True the full metadata dict must appear in each result."""
    result = _make_result(metadata={"secret": "value"})
    pipeline_result = SearchPipelineResult(results=[result], acl_filtered=False)

    payload = await _call_mcp_search(pipeline_result, include_metadata=True)

    assert "results" in payload
    assert len(payload["results"]) == 1
    assert payload["results"][0]["metadata"] == {"secret": "value"}


@pytest.mark.asyncio
async def test_mcp_search_tool_input_schema_is_superset_of_search_filters() -> None:
    """Every field on SearchFilters must appear in the published MCP tool input schema
    for both ``search`` and ``search_with_context``.  This is the compile-time guard
    against REST↔MCP drift."""
    from archon_search.filters import SearchFilters
    from archon_search.pipeline import SearchPipelineResult

    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))
    from archon_search.pipeline import SearchPipelineResult, SearchWithContextResult
    pipeline.search_with_context = AsyncMock(return_value=SearchWithContextResult(results=[], pipeline_result=SearchPipelineResult(results=[], acl_filtered=False)))

    # Use the real FastMCP so list_tools() reflects actual type annotations
    import archon_search.server.mcp as mcp_module
    real_app = mcp_module.create_app(pipeline, "default", writer=None)

    tools_list = await real_app.list_tools()
    schema_by_name = {t.name: t.to_mcp_tool().inputSchema for t in tools_list}

    filter_fields = set(SearchFilters.model_fields.keys())

    for tool_name in ("search", "search_with_context"):
        assert tool_name in schema_by_name, f"Tool {tool_name!r} not found in MCP app"
        tool_props = set(schema_by_name[tool_name].get("properties", {}).keys())
        missing = filter_fields - tool_props
        assert not missing, (
            f"Tool {tool_name!r} input schema is missing SearchFilters fields: {missing}"
        )


# ---------------------------------------------------------------------------
# B3 Task 5.1 — multi-collection `collections` parameter on MCP search
# ---------------------------------------------------------------------------


async def _call_mcp_search_multi(
    *,
    collection: str | None = None,
    collections: list[str] | None = None,
    search_many_return: SearchPipelineResult | None = None,
    search_many_raises: Exception | None = None,
):
    """Invoke the MCP search tool with a pipeline whose search_many is mocked."""
    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))
    if search_many_raises is not None:
        pipeline.search_many = AsyncMock(side_effect=search_many_raises)
    else:
        pipeline.search_many = AsyncMock(
            return_value=search_many_return or SearchPipelineResult(results=[], acl_filtered=False)
        )

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["search"]
        result = await fn(query="hello", collection=collection, collections=collections)
    return pipeline, result


@pytest.mark.asyncio
async def test_mcp_search_both_collection_fields_is_error() -> None:
    pipeline, result = await _call_mcp_search_multi(collection="x", collections=["y"])
    assert result["code"] == "validation_error"
    pipeline.search_many.assert_not_awaited()
    pipeline.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_search_empty_collections_is_error() -> None:
    pipeline, result = await _call_mcp_search_multi(collections=[])
    assert result["code"] == "validation_error"
    pipeline.search_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_search_whitespace_collection_is_error() -> None:
    pipeline, result = await _call_mcp_search_multi(collections=["  "])
    assert result["code"] == "validation_error"
    pipeline.search_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_search_over_limit_collections_is_error() -> None:
    pipeline, result = await _call_mcp_search_multi(collections=[f"c{i}" for i in range(9)])
    assert result["code"] == "validation_error"
    pipeline.search_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_search_deduplicates_collections() -> None:
    pipeline, _result = await _call_mcp_search_multi(collections=["a", "a", "b"])
    pipeline.search_many.assert_awaited_once()
    passed = pipeline.search_many.await_args.args[1]
    assert passed == ["a", "b"]


@pytest.mark.asyncio
async def test_mcp_search_meta_lookup_failure_returns_internal_error() -> None:
    from archon_search.pipeline import MetadataLookupError

    _pipeline, result = await _call_mcp_search_multi(
        collections=["a"], search_many_raises=MetadataLookupError(RuntimeError("x"))
    )
    assert result["code"] == "internal_error"
    assert result["error"] == "service unavailable"


@pytest.mark.asyncio
async def test_mcp_search_collections_calls_search_many() -> None:
    pipeline, _result = await _call_mcp_search_multi(collections=["a", "b"])
    pipeline.search_many.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_search_result_includes_collection_key() -> None:
    r = _make_result()
    r.collection = "a"
    _pipeline, result = await _call_mcp_search_multi(
        collections=["a"],
        search_many_return=SearchPipelineResult(results=[r], acl_filtered=False),
    )
    assert result["results"][0]["collection"] == "a"


@pytest.mark.asyncio
async def test_mcp_search_result_includes_excluded_collections_key() -> None:
    from archon_search._types import ExcludedCollection

    _pipeline, result = await _call_mcp_search_multi(
        collections=["a", "b"],
        search_many_return=SearchPipelineResult(
            results=[],
            acl_filtered=False,
            excluded_collections=[ExcludedCollection(name="b", reason="embedding_model_mismatch")],
        ),
    )
    assert {"name": "b", "reason": "embedding_model_mismatch"} in result["excluded_collections"]


@pytest.mark.asyncio
async def test_mcp_search_missing_collection_returns_not_found() -> None:
    from archon_search.pipeline import CollectionNotFoundError

    _pipeline, result = await _call_mcp_search_multi(
        collections=["x"], search_many_raises=CollectionNotFoundError(["x"])
    )
    assert result["code"] == "not_found"


@pytest.mark.asyncio
async def test_mcp_search_fanout_timeout_returns_timeout() -> None:
    from archon_search.pipeline import FanoutTimeoutError

    _pipeline, result = await _call_mcp_search_multi(
        collections=["a", "b"], search_many_raises=FanoutTimeoutError()
    )
    assert result["code"] == "timeout"


@pytest.mark.asyncio
async def test_mcp_search_multi_emits_search_multi_telemetry() -> None:
    """The MCP search multi path enqueues a search_multi telemetry entry with
    fanout_count = requested - excluded and the correct excluded_count."""
    from archon_search._types import ExcludedCollection
    from archon_search.telemetry.entry import EndpointKind

    pipeline = MagicMock()
    pipeline.search_many = AsyncMock(
        return_value=SearchPipelineResult(
            results=[_make_result()],
            acl_filtered=False,
            excluded_collections=[ExcludedCollection(name="c", reason="embedding_model_mismatch")],
        )
    )
    writer = MagicMock()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=writer)
        await app.tools["search"](query="q", collections=["a", "b", "c"])

    writer.enqueue.assert_called_once()
    entry = writer.enqueue.call_args.args[0]
    assert entry.endpoint == EndpointKind.search_multi
    assert entry.fanout_count == 2
    assert entry.excluded_count == 1
    assert entry.collections == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Task 10.1 — MCP tool description updates for language parameter (C2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tool_language_param_described() -> None:
    """MCP search tool must advertise a description for the language parameter
    that contains 'ISO 639' so callers know it is a language-code filter."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))

    real_app = mcp_module.create_app(pipeline, "default", writer=None)
    tools_list = await real_app.list_tools()
    schema_by_name = {t.name: t.to_mcp_tool().inputSchema for t in tools_list}

    assert "search" in schema_by_name
    lang_prop = schema_by_name["search"].get("properties", {}).get("language", {})
    description = lang_prop.get("description", "")
    assert "ISO 639" in description, (
        f"search tool language param description {description!r} does not contain 'ISO 639'"
    )


@pytest.mark.asyncio
async def test_search_with_context_tool_language_param_described() -> None:
    """MCP search_with_context tool must advertise a description for the language
    parameter that contains 'ISO 639'."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    from archon_search.pipeline import SearchPipelineResult, SearchWithContextResult
    pipeline.search_with_context = AsyncMock(return_value=SearchWithContextResult(results=[], pipeline_result=SearchPipelineResult(results=[], acl_filtered=False)))

    real_app = mcp_module.create_app(pipeline, "default", writer=None)
    tools_list = await real_app.list_tools()
    schema_by_name = {t.name: t.to_mcp_tool().inputSchema for t in tools_list}

    assert "search_with_context" in schema_by_name
    lang_prop = schema_by_name["search_with_context"].get("properties", {}).get("language", {})
    description = lang_prop.get("description", "")
    assert "ISO 639" in description, (
        f"search_with_context tool language param description {description!r} does not contain 'ISO 639'"
    )


@pytest.mark.asyncio
async def test_mcp_search_invalid_language_returns_error() -> None:
    """MCP search tool must return a validation_error for language codes that are
    too long (> 3 chars) rather than an unhandled exception."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        fake_app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = fake_app.tools["search"]
        # "english" is 7 chars — fails ^[a-z]{2,3}$ validator
        result = await fn(query="hello", collection=None, language="english")

    assert isinstance(result, dict)
    assert result.get("code") == "validation_error", (
        f"Expected validation_error, got {result!r}"
    )


@pytest.mark.asyncio
async def test_mcp_search_language_with_collections_returns_validation_error() -> None:
    """MCP search tool must reject language filter when multi-collection fan-out
    (collections) is used — matches REST API behavior."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.search_many = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        fake_app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = fake_app.tools["search"]
        result = await fn(query="hello", collections=["col1", "col2"], language="fr")

    assert isinstance(result, dict)
    assert result.get("code") == "validation_error", (
        f"Expected validation_error for language+collections, got {result!r}"
    )
    # Must not have called search_many — rejected before fanout
    pipeline.search_many.assert_not_awaited()
