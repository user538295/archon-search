"""Tests — MCP search / search_with_context emit stage_timings log records (B1 Task 5.3)."""
from __future__ import annotations

import logging
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# Stub fastmcp so mcp.py can be imported without the real package.
if "fastmcp" not in sys.modules:
    _fastmcp = types.ModuleType("fastmcp")
    _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fastmcp

from archon_search._types import SearchResult
from archon_search.config import SearchConfig
from archon_search.observability import correlation_id as _correlation_id, record_stage
from archon_search.pipeline import SearchPipelineResult


# ---------------------------------------------------------------------------
# FastMCP stub
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


def _make_app(pipeline: MagicMock, timings_enabled: bool = True) -> _FakeApp:
    cfg = SearchConfig()
    cfg.observability.stage_timings_enabled = timings_enabled
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        return mcp_module.create_app(pipeline, "default", writer=None, config=cfg)  # type: ignore[call-arg]


def _make_search_pipeline() -> MagicMock:
    """Pipeline whose search() records embed stage (like the real pipeline would)."""
    pipeline = MagicMock()

    async def _search(*args: Any, **kwargs: Any) -> SearchPipelineResult:
        with record_stage("embed"):
            pass
        return SearchPipelineResult(results=[], acl_filtered=False)

    pipeline.search = AsyncMock(side_effect=_search)
    return pipeline


def _make_search_with_context_pipeline() -> MagicMock:
    """Pipeline whose search_with_context() records embed + context stages."""
    pipeline = MagicMock()

    async def _search_with_context(*args: Any, **kwargs: Any):
        from archon_search.pipeline import SearchPipelineResult, SearchWithContextResult
        with record_stage("embed"):
            pass
        with record_stage("context"):
            pass
        return SearchWithContextResult(results=[], pipeline_result=SearchPipelineResult(results=[], acl_filtered=False))

    pipeline.search_with_context = AsyncMock(side_effect=_search_with_context)
    return pipeline


def _get_stage_timing_records(caplog: pytest.LogCaptureFixture) -> list:
    return [r for r in caplog.records if getattr(r, "event_type", None) == "stage_timings"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_emits_stage_timings_record(caplog: pytest.LogCaptureFixture) -> None:
    """MCP search tool emits one stage_timings log record with 'total' key on success."""
    pipeline = _make_search_pipeline()
    app = _make_app(pipeline)

    with caplog.at_level(logging.INFO, logger="archon_search"):
        result = await app.tools["search"](query="hello", collection=None)

    assert isinstance(result, dict), f"Expected dict result, got {type(result)}"
    assert "error" not in result, f"Expected success, got error: {result}"

    records = _get_stage_timing_records(caplog)
    assert len(records) == 1, f"Expected 1 stage_timings record, got {len(records)}"
    rec = records[0]
    assert rec.endpoint == "search", f"endpoint should be 'search', got {rec.endpoint!r}"
    assert "total" in rec.stage_timings_ms, f"'total' key missing from {set(rec.stage_timings_ms)}"
    assert "embed" in rec.stage_timings_ms, f"'embed' key missing — ContextVar propagation failed: {set(rec.stage_timings_ms)}"


@pytest.mark.asyncio
async def test_mcp_search_stage_timings_disabled_no_log(caplog: pytest.LogCaptureFixture) -> None:
    """stage_timings_enabled=False → MCP search tool emits no stage_timings log record."""
    pipeline = _make_search_pipeline()
    app = _make_app(pipeline, timings_enabled=False)

    with caplog.at_level(logging.DEBUG, logger="archon_search"):
        await app.tools["search"](query="hello", collection=None)

    records = _get_stage_timing_records(caplog)
    assert len(records) == 0, f"Expected 0 stage_timings records when disabled, got {len(records)}"


@pytest.mark.asyncio
async def test_mcp_header_correlation_id_matches_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """MCP search tool log record's correlation_id equals the value set by middleware (AC#18).

    Simulates what RequestContextMiddleware does: sets the correlation_id ContextVar
    before the tool executes, then verifies the log record carries that value.
    """
    pipeline = _make_search_pipeline()
    app = _make_app(pipeline)

    test_id = "mcp-test-request-id-abc123"
    token = _correlation_id.set(test_id)
    try:
        with caplog.at_level(logging.INFO, logger="archon_search"):
            await app.tools["search"](query="hello", collection=None)
    finally:
        _correlation_id.reset(token)

    records = _get_stage_timing_records(caplog)
    assert len(records) == 1, f"Expected 1 stage_timings record, got {len(records)}"
    assert records[0].correlation_id == test_id, (
        f"Log record correlation_id should be {test_id!r}, got {records[0].correlation_id!r}"
    )


@pytest.mark.asyncio
async def test_mcp_search_with_context_emits_stage_timings_with_context_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MCP search_with_context tool emits stage_timings with 'context' key (AC#16)."""
    pipeline = _make_search_with_context_pipeline()
    app = _make_app(pipeline)

    with caplog.at_level(logging.INFO, logger="archon_search"):
        result = await app.tools["search_with_context"](query="hello", collection=None)

    records = _get_stage_timing_records(caplog)
    assert len(records) == 1, f"Expected 1 stage_timings record, got {len(records)}"
    rec = records[0]
    assert rec.endpoint == "search_with_context", (
        f"endpoint should be 'search_with_context', got {rec.endpoint!r}"
    )
    assert "embed" in rec.stage_timings_ms, f"'embed' key missing — ContextVar propagation failed: {set(rec.stage_timings_ms)}"
    assert "context" in rec.stage_timings_ms, (
        f"'context' key must be in stage_timings_ms, got {set(rec.stage_timings_ms)}"
    )
    assert "total" in rec.stage_timings_ms, f"'total' key missing from {set(rec.stage_timings_ms)}"


@pytest.mark.asyncio
async def test_mcp_search_with_context_correlation_id_matches_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """MCP search_with_context log record's correlation_id equals the value set by middleware."""
    pipeline = _make_search_with_context_pipeline()
    app = _make_app(pipeline)

    test_id = "swc-test-request-id-xyz789"
    token = _correlation_id.set(test_id)
    try:
        with caplog.at_level(logging.INFO, logger="archon_search"):
            await app.tools["search_with_context"](query="hello", collection=None)
    finally:
        _correlation_id.reset(token)

    records = _get_stage_timing_records(caplog)
    assert len(records) == 1, f"Expected 1 stage_timings record, got {len(records)}"
    assert records[0].correlation_id == test_id, (
        f"Log record correlation_id should be {test_id!r}, got {records[0].correlation_id!r}"
    )


@pytest.mark.asyncio
async def test_mcp_search_with_context_stage_timings_disabled_no_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """stage_timings_enabled=False → MCP search_with_context emits no stage_timings log."""
    pipeline = _make_search_with_context_pipeline()
    app = _make_app(pipeline, timings_enabled=False)

    with caplog.at_level(logging.DEBUG, logger="archon_search"):
        await app.tools["search_with_context"](query="hello", collection=None)

    records = _get_stage_timing_records(caplog)
    assert len(records) == 0, f"Expected 0 stage_timings records when disabled, got {len(records)}"
