"""MCP ``search`` tool response shape contract.

Pattern follows ``tests/server/test_mcp_telemetry.py`` (FastMCP-stubbed,
invoking the registered tool function directly) — the MCP `search` tool
serializes each ``SearchResult`` via ``dataclasses.asdict``, so the new
fields appear automatically after Task 4.1.

Implements Task 4.3 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# Resolve fastmcp: prefer the real mcp.server.fastmcp, fall back to a
# minimal stub. Avoid clobbering an existing entry — test_mcp_auth.py
# relies on the real implementation being installed.
if "fastmcp" not in sys.modules:
    try:
        import mcp.server.fastmcp as _real_fastmcp  # type: ignore[import-not-found]
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


@pytest.mark.asyncio
async def test_mcp_search_tool_includes_new_keys() -> None:
    sr = SearchResult(
        doc_id="d" * 64,
        chunk_id="d" * 64 + "-000000",
        text="hello",
        score=0.9,
        source_path="/tmp/x.md",
        file_type="md",
        indexed_at="2026-05-21T10:00:00+00:00",
        updated_at="2026-05-21T11:00:00+00:00",
        ingested_by="cli",
        metadata={"k": "v"},
        acl=["team-a"],
    )
    pipeline = MagicMock()
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(results=[sr], acl_filtered=False)
    )

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        search_fn = app.tools["search"]
        payload = await search_fn(query="hello", collection=None, include_metadata=True)

    assert "results" in payload and payload["results"]
    item = payload["results"][0]
    for key in ("file_type", "indexed_at", "updated_at", "ingested_by", "metadata", "acl"):
        assert key in item, f"missing key {key!r} in MCP search response"
    assert item["file_type"] == "md"
    assert item["ingested_by"] == "cli"
    assert item["metadata"] == {"k": "v"}
    assert item["acl"] == ["team-a"]
