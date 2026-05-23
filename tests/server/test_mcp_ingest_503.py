"""A5c — verify MCP ingest_file/ingest_directory surface StoreBusyError synchronously.

MCP ingest tools call pipeline.ingest_file/ingest_directory directly (no async-job
wrapper), so StoreBusyError surfaces synchronously to the MCP client. The existing
except Exception block wraps it; we verify the error is surfaced (not swallowed).
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# FastMCP stub
if "fastmcp" not in sys.modules:
    _fm = types.ModuleType("fastmcp")
    _fm.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    _fm.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fm

from archon_search.store import StoreBusyError


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


def _build_app(pipeline: MagicMock) -> _FakeApp:
    with patch("archon_search.server.mcp.FastMCP", _FakeFastMCP):
        from archon_search.server.mcp import create_app
        return create_app(pipeline, "default-col", writer=None)  # type: ignore[return-value]


def test_mcp_ingest_file_surfaces_store_busy_error() -> None:
    """MCP ingest_file returns code='store_busy' for StoreBusyError (not 'internal_error')."""
    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock(side_effect=StoreBusyError(timeout_s=30.0))
    app = _build_app(pipeline)

    result = asyncio.run(app.tools["ingest_file"](path="/tmp/legit.md"))
    assert isinstance(result, dict)
    assert result.get("code") == "store_busy", f"Expected store_busy; got {result}"
    assert "doc_id" not in result


def test_mcp_ingest_directory_surfaces_store_busy_error() -> None:
    """MCP ingest_directory returns code='store_busy' for StoreBusyError."""
    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(side_effect=StoreBusyError(timeout_s=30.0))
    app = _build_app(pipeline)

    result = asyncio.run(app.tools["ingest_directory"](path="/tmp/legit"))
    assert isinstance(result, dict)
    assert result.get("code") == "store_busy", f"Expected store_busy; got {result}"
