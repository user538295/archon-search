"""Tests — MCP ingest_file / ingest_directory surface StoreBusyError as code='store_busy'."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.store import StoreBusyError
from tests._fastmcp_stub import StubFastMCP, stubbed_fastmcp




# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_app(pipeline: MagicMock) -> StubFastMCP:
    with stubbed_fastmcp():
        from archon_search.server import mcp as mcp_module
        return mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Tests — MCP ingest StoreBusyError surface (Task 2c.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_ingest_file_store_busy_returns_store_busy_code() -> None:
    """ingest_file surfaces StoreBusyError as McpErrorResponse with code='store_busy'."""
    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock(side_effect=StoreBusyError(timeout_s=30.0))
    app = _make_app(pipeline)
    result = await app.tools["ingest_file"](path="/tmp/x.md", collection=None)
    assert isinstance(result, dict)
    assert result["code"] == "store_busy"
    assert "error" in result


@pytest.mark.asyncio
async def test_mcp_ingest_directory_store_busy_returns_store_busy_code() -> None:
    """ingest_directory surfaces StoreBusyError as McpErrorResponse with code='store_busy'."""
    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(side_effect=StoreBusyError(timeout_s=30.0))
    app = _make_app(pipeline)
    result = await app.tools["ingest_directory"](path="/tmp/dir", collection=None)
    assert isinstance(result, dict)
    assert result["code"] == "store_busy"
    assert "error" in result


@pytest.mark.asyncio
async def test_mcp_ingest_file_generic_error_still_internal_error() -> None:
    """Generic exceptions in ingest_file still produce code='internal_error' (dedicated clause doesn't swallow them)."""
    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock(side_effect=RuntimeError("boom"))
    app = _make_app(pipeline)
    result = await app.tools["ingest_file"](path="/tmp/x.md", collection=None)
    assert isinstance(result, dict)
    assert result["code"] == "internal_error"
