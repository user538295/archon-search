"""Tests for telemetry emission in the MCP search tools — FEAT-039b Tasks 3.2 & 3.3."""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub fastmcp so mcp.py can be imported without the real package.
if "fastmcp" not in sys.modules:
    _fastmcp = types.ModuleType("fastmcp")
    _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fastmcp

from archon_search._types import SearchResult
from archon_search.pipeline import SearchPipelineResult
from archon_search.telemetry.entry import TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter


# ---------------------------------------------------------------------------
# FastMCP stub — makes @app.tool() registration and direct invocation work
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
# Helper: build a stub pipeline
# ---------------------------------------------------------------------------


def _make_pipeline(
    results: list[SearchResult] | None = None,
    raises: Exception | None = None,
) -> MagicMock:
    pipeline = MagicMock()
    if raises is not None:
        pipeline.search = AsyncMock(side_effect=raises)
    else:
        pipeline.search = AsyncMock(
            return_value=SearchPipelineResult(results=results or [], acl_filtered=False)
        )
    return pipeline


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tool_logs_entry_on_success() -> None:
    """On success, writer.enqueue is called once with a retrieval entry."""
    results = [
        SearchResult(doc_id="doc1", chunk_id="c1", text="a", score=0.9, source_path="/a"),
        SearchResult(doc_id="doc2", chunk_id="c2", text="b", score=0.8, source_path="/b"),
    ]
    pipeline = _make_pipeline(results=results)
    writer = MagicMock(spec=TelemetryWriter)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=writer)  # type: ignore[call-arg]
        search_fn = app.tools["search"]  # type: ignore[attr-defined]
        await search_fn(query="hello world", collection=None)

    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.endpoint == "search"
    assert entry.status == "ok"
    assert entry.result_count == 2


@pytest.mark.asyncio
async def test_search_tool_logs_error_entry_on_exception(caplog: pytest.LogCaptureFixture) -> None:
    """On exception, writer.enqueue is called with an error entry and the exception is logged."""
    pipeline = _make_pipeline(raises=RuntimeError("boom"))
    writer = MagicMock(spec=TelemetryWriter)

    import logging

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=writer)  # type: ignore[call-arg]
        search_fn = app.tools["search"]  # type: ignore[attr-defined]

        with caplog.at_level(logging.ERROR, logger="archon.search"):
            await search_fn(query="boom test", collection=None)

    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.endpoint == "search"
    assert entry.status == "internal_error"
    assert entry.error_kind == "other"

    # caplog.text includes the full formatted output (message + exception traceback),
    # so "RuntimeError" appears there even though getMessage() returns only the message string.
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_search_tool_query_text_never_in_factory_args() -> None:
    """The query string must never reach TelemetryEntry factories."""
    sentinel = "SUPER_SECRET_QUERY_12345"
    results = [
        SearchResult(doc_id="docX", chunk_id="cX", text="x", score=1.0, source_path="/x"),
    ]
    pipeline = _make_pipeline(results=results)
    writer = MagicMock(spec=TelemetryWriter)

    recorded_kwargs: dict[str, Any] = {}

    def recording_factory(**kwargs: Any) -> TelemetryEntry:
        recorded_kwargs.update(kwargs)
        return TelemetryEntry.from_search_tool_result(**kwargs)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        with patch.object(TelemetryEntry, "from_search_tool_result", side_effect=recording_factory):
            from archon_search.server import mcp as mcp_module

            app = mcp_module.create_app(pipeline, "default", writer=writer)  # type: ignore[call-arg]
            search_fn = app.tools["search"]  # type: ignore[attr-defined]
            await search_fn(query=sentinel, collection=None)

    for value in recorded_kwargs.values():
        assert sentinel not in str(value), f"Sentinel found in factory kwarg value: {value!r}"


@pytest.mark.asyncio
async def test_search_tool_does_not_log_when_writer_none() -> None:
    """When writer=None, no exception is raised and no enqueue call happens."""
    results = [
        SearchResult(doc_id="doc1", chunk_id="c1", text="a", score=0.9, source_path="/a"),
    ]
    pipeline = _make_pipeline(results=results)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        search_fn = app.tools["search"]  # type: ignore[attr-defined]
        # Should not raise
        output = await search_fn(query="anything", collection=None)

    assert isinstance(output, dict)
    assert "results" in output
    assert len(output["results"]) == 1


# ---------------------------------------------------------------------------
# search_with_context tool tests (Task 3.3)
# ---------------------------------------------------------------------------


def _make_swc_pipeline(
    results: list[dict[str, Any]] | None = None,
    raises: Exception | None = None,
) -> MagicMock:
    """Build a stub pipeline with a search_with_context method."""
    pipeline = MagicMock()
    if raises is not None:
        pipeline.search_with_context = AsyncMock(side_effect=raises)
    else:
        pipeline.search_with_context = AsyncMock(return_value=results or [])
    return pipeline


@pytest.mark.asyncio
async def test_search_with_context_tool_logs_entry_on_success() -> None:
    """On success, writer.enqueue is called once with a retrieval entry for search_with_context."""
    r1 = SearchResult(doc_id="doc1", chunk_id="c1", text="a", score=0.9, source_path="/a")
    r2 = SearchResult(doc_id="doc2", chunk_id="c2", text="b", score=0.8, source_path="/b")
    swc_results = [
        {"result": r1, "context_before": [], "context_after": []},
        {"result": r2, "context_before": [], "context_after": []},
    ]
    pipeline = _make_swc_pipeline(results=swc_results)
    writer = MagicMock(spec=TelemetryWriter)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=writer)  # type: ignore[call-arg]
        swc_fn = app.tools["search_with_context"]  # type: ignore[attr-defined]
        await swc_fn(query="hello", collection=None)

    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.endpoint == "search_with_context"
    assert entry.status == "ok"
    assert entry.result_count == 2
    assert entry.result_doc_ids == ["doc1", "doc2"]
    assert entry.collection == "default"


@pytest.mark.asyncio
async def test_search_with_context_tool_logs_error_entry_on_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """On exception, writer.enqueue is called with an error entry and the exception is logged."""
    pipeline = _make_swc_pipeline(raises=RuntimeError("swc boom"))
    writer = MagicMock(spec=TelemetryWriter)

    import logging

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=writer)  # type: ignore[call-arg]
        swc_fn = app.tools["search_with_context"]  # type: ignore[attr-defined]

        with caplog.at_level(logging.ERROR, logger="archon.search"):
            await swc_fn(query="boom test", collection=None)

    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.endpoint == "search_with_context"
    assert entry.status == "internal_error"
    assert entry.error_kind == "other"

    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_search_with_context_tool_query_text_never_in_factory_args() -> None:
    """The query string must never reach TelemetryEntry factories for search_with_context."""
    sentinel = "SUPER_SECRET_QUERY_SWC_67890"
    r1 = SearchResult(doc_id="docX", chunk_id="cX", text="x", score=1.0, source_path="/x")
    swc_results = [{"result": r1, "context_before": [], "context_after": []}]
    pipeline = _make_swc_pipeline(results=swc_results)
    writer = MagicMock(spec=TelemetryWriter)

    recorded_kwargs: dict[str, Any] = {}

    def recording_factory(**kwargs: Any) -> TelemetryEntry:
        recorded_kwargs.update(kwargs)
        return TelemetryEntry.from_search_tool_result(**kwargs)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        with patch.object(TelemetryEntry, "from_search_tool_result", side_effect=recording_factory):
            from archon_search.server import mcp as mcp_module

            app = mcp_module.create_app(pipeline, "default", writer=writer)  # type: ignore[call-arg]
            swc_fn = app.tools["search_with_context"]  # type: ignore[attr-defined]
            await swc_fn(query=sentinel, collection=None)

    for value in recorded_kwargs.values():
        assert sentinel not in str(value), f"Sentinel found in factory kwarg value: {value!r}"


# ---------------------------------------------------------------------------
# Task 4.2: search tool response shape tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_returns_results_and_acl_filtered() -> None:
    """search tool returns dict with 'results' list and 'acl_filtered' bool."""
    results = [
        SearchResult(doc_id="doc1", chunk_id="c1", text="a", score=0.9, source_path="/a"),
    ]
    pipeline = _make_pipeline(results=results, raises=None)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        search_fn = app.tools["search"]  # type: ignore[attr-defined]
        output = await search_fn(query="hello", collection=None)

    assert isinstance(output, dict)
    assert "results" in output
    assert "acl_filtered" in output
    assert isinstance(output["results"], list)
    assert isinstance(output["acl_filtered"], bool)
    assert len(output["results"]) == 1


@pytest.mark.asyncio
async def test_mcp_search_acl_filtered_propagated() -> None:
    """When pipeline returns acl_filtered=True, MCP response has acl_filtered: true."""
    pipeline = MagicMock()
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(results=[], acl_filtered=True)
    )

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        search_fn = app.tools["search"]  # type: ignore[attr-defined]
        output = await search_fn(query="secret", collection=None)

    assert output["acl_filtered"] is True


@pytest.mark.asyncio
async def test_mcp_search_error_returns_dict_not_list() -> None:
    """Error response from search tool is a dict, not a list."""
    pipeline = _make_pipeline(raises=RuntimeError("something went wrong"))

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        search_fn = app.tools["search"]  # type: ignore[attr-defined]
        output = await search_fn(query="fail", collection=None)

    assert isinstance(output, dict)
    assert not isinstance(output, list)
    assert "error" in output
    assert output.get("code") == "internal_error"


@pytest.mark.asyncio
async def test_search_with_context_tool_does_not_log_when_writer_none() -> None:
    """When writer=None, no exception is raised and results are still returned."""
    r1 = SearchResult(doc_id="doc1", chunk_id="c1", text="a", score=0.9, source_path="/a")
    swc_results = [{"result": r1, "context_before": [], "context_after": []}]
    pipeline = _make_swc_pipeline(results=swc_results)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        swc_fn = app.tools["search_with_context"]  # type: ignore[attr-defined]
        output = await swc_fn(query="anything", collection=None)

    assert isinstance(output, list)
    assert len(output) == 1
    assert "result" in output[0]
