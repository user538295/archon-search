"""Tests pinning that the MCP ``search_with_context`` tool strips the
``vector`` key from every neighbor chunk in ``context_before`` /
``context_after``.

Implements Task 5.1 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Resolve fastmcp: prefer the real mcp.server.fastmcp (shipped with the
# mcp package), fall back to a minimal stub. Avoid clobbering an existing
# entry — test_mcp_auth.py also relies on the real implementation being
# installed.
if "fastmcp" not in sys.modules:
    try:
        import mcp.server.fastmcp as _real_fastmcp  # type: ignore[import-not-found]
        sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]
    except ImportError:
        _fastmcp = types.ModuleType("fastmcp")
        _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _fastmcp

from archon_search._types import ChunkRecord, SearchResult


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


def _neighbor(idx: int) -> ChunkRecord:
    """A neighbor ChunkRecord with a non-empty vector to exercise the strip path."""
    doc_id = "d" * 64
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=f"neighbor text {idx}",
        vector=[0.1, 0.2, 0.3, 0.4],  # non-empty per the brief
        source_path="/tmp/x.md",
        indexed_at=datetime.now(timezone.utc).isoformat(),
        file_type="md",
        updated_at="2026-05-21T10:00:00+00:00",
        ingested_by="cli",
        metadata={"k": "v"},
    )


def _result(idx: int) -> SearchResult:
    doc_id = "d" * 64
    return SearchResult(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=f"matched text {idx}",
        score=0.9,
        source_path="/tmp/x.md",
        file_type="md",
        ingested_by="cli",
    )


async def _call_search_with_context(results_list):
    pipeline = MagicMock()
    pipeline.search_with_context = AsyncMock(return_value=results_list)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        fn = app.tools["search_with_context"]
        return await fn(query="hello", collection=None, context_window=1)


@pytest.mark.asyncio
async def test_search_with_context_strips_vector_from_neighbors() -> None:
    pipeline_result = [
        {
            "result": _result(1),
            "context_before": [_neighbor(0)],
            "context_after": [_neighbor(2)],
        }
    ]
    payload = await _call_search_with_context(pipeline_result)
    assert payload, "expected at least one result"
    entry = payload[0]
    for neighbor in entry["context_before"] + entry["context_after"]:
        assert "vector" not in neighbor, f"vector leaked in neighbor: {neighbor!r}"


@pytest.mark.asyncio
async def test_search_with_context_preserves_other_chunk_fields_in_neighbors() -> None:
    pipeline_result = [
        {
            "result": _result(1),
            "context_before": [_neighbor(0)],
            "context_after": [_neighbor(2)],
        }
    ]
    payload = await _call_search_with_context(pipeline_result)
    entry = payload[0]
    expected_keys = {
        "text",
        "chunk_id",
        "doc_id",
        "source_path",
        "file_type",
        "updated_at",
        "ingested_by",
        "metadata",
    }
    for neighbor in entry["context_before"] + entry["context_after"]:
        missing = expected_keys - set(neighbor.keys())
        assert not missing, f"neighbor missing keys {missing}: {neighbor!r}"
