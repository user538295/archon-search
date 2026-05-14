"""Tests for telemetry emission in the MCP `search` tool — FEAT-039b Task 3.2."""
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
        pipeline.search = AsyncMock(return_value=results or [])
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

    assert isinstance(output, list)
    assert len(output) == 1
