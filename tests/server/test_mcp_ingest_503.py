"""Tests — MCP ingest_file / ingest_directory surface StoreBusyError as code='store_busy'."""
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

from archon_search.store import StoreBusyError


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
# Helper
# ---------------------------------------------------------------------------


def _make_app(pipeline: MagicMock) -> _FakeApp:
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
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
