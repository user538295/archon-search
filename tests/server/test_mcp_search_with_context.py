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


def _result(idx: int, language: str = "") -> SearchResult:
    doc_id = "d" * 64
    return SearchResult(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=f"matched text {idx}",
        score=0.9,
        source_path="/tmp/x.md",
        file_type="md",
        ingested_by="cli",
        language=language,
        metadata={"res": "val"},
    )


async def _call_search_with_context(results_list, include_metadata: bool = False):
    from archon_search.pipeline import SearchPipelineResult, SearchWithContextResult
    pipeline = MagicMock()
    pipeline.search_with_context = AsyncMock(return_value=SearchWithContextResult(
        results=results_list,
        pipeline_result=SearchPipelineResult(results=[], acl_filtered=False),
    ))

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        fn = app.tools["search_with_context"]
        return await fn(query="hello", collection=None, context_window=1, include_metadata=include_metadata)


@pytest.mark.asyncio
async def test_search_with_context_strips_vector_from_neighbors() -> None:
    pipeline_result = [
        {
            "result": _result(1),
            "context_before": [_neighbor(0)],
            "context_after": [_neighbor(2)],
        }
    ]
    payload = await _call_search_with_context(pipeline_result, include_metadata=True)
    # search_with_context now returns {"results": [...], "hyde_applied": bool}
    assert "results" in payload, "expected results key in payload"
    results = payload["results"]
    assert results, "expected at least one result"
    entry = results[0]
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
    payload = await _call_search_with_context(pipeline_result, include_metadata=True)
    entry = payload["results"][0]
    expected_keys = {
        "text",
        "chunk_id",
        "doc_id",
        "source_path",
        "file_type",
        "updated_at",
        "ingested_by",
        "metadata",
        "indexed_at",
        "language",
        "custom_score",
        "acl",
    }
    for neighbor in entry["context_before"] + entry["context_after"]:
        missing = expected_keys - set(neighbor.keys())
        assert not missing, f"neighbor missing keys {missing}: {neighbor!r}"


@pytest.mark.asyncio
async def test_search_with_context_strips_metadata_from_context_chunks_when_include_metadata_false() -> None:
    """Context chunks must have metadata set to empty dict when include_metadata=False."""
    pipeline_result = [
        {
            "result": _result(1),
            "context_before": [_neighbor(0)],
            "context_after": [_neighbor(2)],
        }
    ]
    payload = await _call_search_with_context(pipeline_result, include_metadata=False)
    results = payload["results"]
    assert results, "expected at least one result"
    entry = results[0]
    for neighbor in entry["context_before"] + entry["context_after"]:
        assert neighbor["metadata"] == {}, f"metadata not suppressed in neighbor: {neighbor!r}"


@pytest.mark.asyncio
async def test_search_with_context_preserves_metadata_in_context_chunks_when_include_metadata_true() -> None:
    """Context chunks must retain actual metadata when include_metadata=True."""
    pipeline_result = [
        {
            "result": _result(1),
            "context_before": [_neighbor(0)],
            "context_after": [_neighbor(2)],
        }
    ]
    payload = await _call_search_with_context(pipeline_result, include_metadata=True)
    results = payload["results"]
    assert results, "expected at least one result"
    entry = results[0]
    for neighbor in entry["context_before"] + entry["context_after"]:
        assert neighbor["metadata"] == {"k": "v"}, f"metadata unexpectedly absent/modified in neighbor: {neighbor!r}"


@pytest.mark.asyncio
async def test_search_with_context_propagates_language_in_result() -> None:
    """The ``language`` field of the matched result must appear in ``entry["result"]``."""
    pipeline_result = [
        {
            "result": _result(1, language="en"),
            "context_before": [],
            "context_after": [],
        }
    ]
    payload = await _call_search_with_context(pipeline_result, include_metadata=False)
    results = payload["results"]
    assert results, "expected at least one result"
    entry = results[0]
    assert entry["result"]["language"] == "en", (
        f"language not propagated in result: {entry['result']!r}"
    )


@pytest.mark.asyncio
async def test_search_with_context_result_metadata_suppressed_and_preserved() -> None:
    """``entry["result"]["metadata"]`` must be ``{}`` when include_metadata=False and
    the actual metadata when include_metadata=True."""
    pipeline_result = [
        {
            "result": _result(1),
            "context_before": [],
            "context_after": [],
        }
    ]
    # include_metadata=False → metadata must be empty dict
    payload_false = await _call_search_with_context(pipeline_result, include_metadata=False)
    assert payload_false["results"][0]["result"]["metadata"] == {}, (
        f"metadata not suppressed: {payload_false['results'][0]['result']['metadata']!r}"
    )

    # include_metadata=True → metadata must be preserved
    payload_true = await _call_search_with_context(pipeline_result, include_metadata=True)
    assert payload_true["results"][0]["result"]["metadata"] == {"res": "val"}, (
        f"metadata not preserved: {payload_true['results'][0]['result']['metadata']!r}"
    )


@pytest.mark.asyncio
async def test_search_with_context_returns_hyde_applied_false_by_default() -> None:
    """search_with_context returns hyde_applied=False when hyde is not requested."""
    pipeline_result = []
    payload = await _call_search_with_context(pipeline_result)
    assert "hyde_applied" in payload
    assert payload["hyde_applied"] is False
