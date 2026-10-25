"""MCP ``search`` tool response shape contract.

Pattern follows ``tests/server/test_mcp_telemetry.py`` (FastMCP-stubbed,
invoking the registered tool function directly) — the MCP `search` tool
serializes each ``SearchResult`` via ``dataclasses.asdict``, so the new
fields appear automatically after Task 4.1.

Implements Task 4.3 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import SearchResult
from archon_search.pipeline import SearchPipelineResult
from tests._fastmcp_stub import stubbed_fastmcp


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
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(results=[sr], acl_filtered=False)
    )

    with stubbed_fastmcp():
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        search_fn = app.tools["search"]
        payload = await search_fn(query="hello", collection=None, include_metadata=True)

    assert payload.get("results")
    item = payload["results"][0]
    for key in ("file_type", "indexed_at", "updated_at", "ingested_by", "metadata", "acl"):
        assert key in item, f"missing key {key!r} in MCP search response"
    assert item["file_type"] == "md"
    assert item["ingested_by"] == "cli"
    assert item["metadata"] == {"k": "v"}
    assert item["acl"] == ["team-a"]
