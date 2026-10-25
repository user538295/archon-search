"""Tests pinning that the MCP ``search_with_context`` tool strips the
``vector`` key from every neighbor chunk in ``context_before`` /
``context_after``, and that it bounds ``context_window`` to ``[0, [search].context_window_max]``.

Implements Task 5.1 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import ChunkRecord, SearchResult
from archon_search.constants import DEFAULT_CONTEXT_WINDOW, DEFAULT_CONTEXT_WINDOW_MAX
from archon_search.pipeline import (
    SearchPipeline,
    SearchPipelineResult,
    SearchWithContextResult,
)
from tests._fastmcp_stub import stubbed_fastmcp
from tests._hyde_stubs import hyde_enabled_config, stub_hyde_generator
from tests._mcp_tools import registered_tools


def _neighbor(idx: int) -> ChunkRecord:
    """A neighbor ChunkRecord with a non-empty vector to exercise the strip path."""
    doc_id = "d" * 64
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=f"neighbor text {idx}",
        vector=[0.1, 0.2, 0.3, 0.4],  # non-empty per the brief
        source_path="/tmp/x.md",
        indexed_at=datetime.now(UTC).isoformat(),
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


def _make_pipeline(results_list: list[dict[str, Any]] | None = None) -> MagicMock:
    pipeline = MagicMock()
    pipeline.context_window_max = DEFAULT_CONTEXT_WINDOW_MAX
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.search_with_context = AsyncMock(return_value=SearchWithContextResult(
        results=results_list or [],
        pipeline_result=SearchPipelineResult(results=[], acl_filtered=False),
    ))
    return pipeline


async def _call_tool(pipeline: MagicMock, **kwargs: Any) -> Any:
    with stubbed_fastmcp():
        from archon_search.server import mcp as mcp_module

        app = mcp_module.create_app(pipeline, "default", writer=None)  # type: ignore[call-arg]
        return await app.tools["search_with_context"](query="hello", **kwargs)


async def _call_search_with_context(results_list, include_metadata: bool = False):
    return await _call_tool(
        _make_pipeline(results_list), collection=None, context_window=1, include_metadata=include_metadata
    )


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
    """Context chunks expose the ContextChunkSchema public fields.
    After the Task 2.2 Pydantic migration, custom_score/start_offset/end_offset
    are intentionally excluded from the MCP contract."""
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
        "acl",
    }
    excluded_keys = {"custom_score", "start_offset", "end_offset", "vector"}
    for neighbor in entry["context_before"] + entry["context_after"]:
        missing = expected_keys - set(neighbor.keys())
        assert not missing, f"neighbor missing keys {missing}: {neighbor!r}"
        leaked = excluded_keys & set(neighbor.keys())
        assert not leaked, f"internal fields leaked into neighbor: {leaked!r}"


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


# ---------------------------------------------------------------------------
# BE-3: expansion_used and expansion_warning fields in search_with_context tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_with_context_expansion_warning_on_hyde_failure() -> None:
    """search_with_context returns expansion_warning='HyDE expansion failed' when HyDE generates no vector with hyde=True."""
    generator = stub_hyde_generator(return_value=None)
    with stubbed_fastmcp():
        from archon_search.server import mcp as mcp_module

        fake_app = mcp_module.create_app(
            _make_pipeline(), "default", writer=None, config=hyde_enabled_config(),
            hyde_generator=generator,
        )
        result = await fake_app.tools["search_with_context"](query="hello", collection="col", hyde=True)

    generator.generate.assert_awaited_once()
    assert isinstance(result, dict)
    assert result.get("expansion_used") is False
    assert result.get("expansion_warning") == "HyDE expansion failed"


@pytest.mark.asyncio
async def test_mcp_search_with_context_no_expansion_by_default() -> None:
    """search_with_context returns expansion_used=False and expansion_warning=None when no expansion requested."""
    pipeline_result = []
    payload = await _call_search_with_context(pipeline_result)
    assert payload.get("expansion_used") is False
    assert payload.get("expansion_warning") is None


@pytest.mark.asyncio
async def test_mcp_search_with_context_expansion_used_true_on_hyde_success() -> None:
    """search_with_context returns expansion_used=True when HyDE succeeds."""
    import numpy as np

    fake_vector = np.ones(384, dtype=np.float32)
    with stubbed_fastmcp():
        from archon_search.server import mcp as mcp_module

        fake_app = mcp_module.create_app(
            _make_pipeline(), "default", writer=None, config=hyde_enabled_config(),
            hyde_generator=stub_hyde_generator(return_value=fake_vector),
        )
        result = await fake_app.tools["search_with_context"](query="hello", collection="col", hyde=True)

    assert isinstance(result, dict)
    assert result.get("expansion_used") is True
    assert result.get("expansion_warning") is None


@pytest.mark.asyncio
async def test_mcp_search_with_context_expansion_warning_on_rag_fusion_timeout() -> None:
    """search_with_context returns expansion_warning='RAG Fusion timed out' when pipeline has rag_fusion_warning."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.context_window_max = DEFAULT_CONTEXT_WINDOW_MAX
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.search_with_context = AsyncMock(
        return_value=SearchWithContextResult(
            results=[],
            pipeline_result=SearchPipelineResult(
                results=[], acl_filtered=False, rag_fusion_applied=False, rag_fusion_warning="RAG Fusion timed out"
            ),
        )
    )

    with stubbed_fastmcp():
        fake_app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = fake_app.tools["search_with_context"]
        result = await fn(query="hello", collection="col", rag_fusion=True)

    assert isinstance(result, dict)
    assert result.get("expansion_used") is False
    assert result.get("expansion_warning") == "RAG Fusion timed out"


# ---------------------------------------------------------------------------
# context_window bound: [0, [search].context_window_max] — an unbounded window made
# SearchStore.fetch_adjacent_chunks build 2 * window chunk ids per hit.
# ---------------------------------------------------------------------------


def _passed_context_window(pipeline: MagicMock) -> int:
    """``context_window`` as the tool passed it to the pipeline, positional or keyword."""
    call = pipeline.search_with_context.call_args
    bound = inspect.signature(SearchPipeline.search_with_context).bind(None, *call.args, **call.kwargs)
    return bound.arguments["context_window"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context_window",
    [-1, DEFAULT_CONTEXT_WINDOW_MAX + 1, 10**9],
    ids=["negative", "just_above_max", "huge"],
)
async def test_search_with_context_rejects_out_of_range_context_window(context_window: int) -> None:
    pipeline = _make_pipeline()
    generator = stub_hyde_generator(return_value=None)

    with stubbed_fastmcp():
        from archon_search.server import mcp as mcp_module

        fake_app = mcp_module.create_app(
            pipeline, "default", writer=None, config=hyde_enabled_config(),
            hyde_generator=generator,
        )
        payload = await fake_app.tools["search_with_context"](
            query="hello", context_window=context_window, hyde=True
        )

    assert payload == {
        "error": f"context_window must be in [0, {DEFAULT_CONTEXT_WINDOW_MAX}]; got {context_window}",
        "code": "validation_error",
    }
    generator.generate.assert_not_awaited()
    pipeline.get_collection_meta.assert_not_called()
    pipeline.search_with_context.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("context_window", [0, DEFAULT_CONTEXT_WINDOW_MAX], ids=["zero", "max"])
async def test_search_with_context_accepts_boundary_context_window(context_window: int) -> None:
    pipeline = _make_pipeline()

    payload = await _call_tool(pipeline, context_window=context_window)

    assert "code" not in payload, payload
    assert payload["results"] == []
    pipeline.search_with_context.assert_called_once()
    assert _passed_context_window(pipeline) == context_window


_LOWERED_CONTEXT_WINDOW_MAX = 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context_window",
    [_LOWERED_CONTEXT_WINDOW_MAX + 1, -1],
    ids=["just_above_lowered_max", "negative"],
)
async def test_search_with_context_rejects_window_outside_lowered_pipeline_max(context_window: int) -> None:
    """The bound is the pipeline's context_window_max, and the error cites that value."""
    pipeline = _make_pipeline()
    pipeline.context_window_max = _LOWERED_CONTEXT_WINDOW_MAX

    payload = await _call_tool(pipeline, context_window=context_window)

    assert payload == {
        "error": f"context_window must be in [0, {_LOWERED_CONTEXT_WINDOW_MAX}]; got {context_window}",
        "code": "validation_error",
    }
    pipeline.search_with_context.assert_not_called()


@pytest.mark.asyncio
async def test_search_with_context_accepts_window_up_to_raised_pipeline_max() -> None:
    """A raised ceiling admits windows the default would reject."""
    context_window = DEFAULT_CONTEXT_WINDOW_MAX + 1
    pipeline = _make_pipeline()
    pipeline.context_window_max = context_window

    payload = await _call_tool(pipeline, context_window=context_window)

    assert "code" not in payload, payload
    assert "results" in payload, payload
    assert _passed_context_window(pipeline) == context_window


@pytest.mark.asyncio
async def test_search_with_context_default_window_accepted_at_minimum_pipeline_max() -> None:
    """At the config floor (max == DEFAULT_CONTEXT_WINDOW) a call omitting context_window still searches."""
    pipeline = _make_pipeline()
    pipeline.context_window_max = DEFAULT_CONTEXT_WINDOW

    payload = await _call_tool(pipeline)

    assert "code" not in payload, payload
    assert "results" in payload, payload
    assert _passed_context_window(pipeline) == DEFAULT_CONTEXT_WINDOW


@pytest.mark.asyncio
async def test_search_with_context_context_window_defaults_to_one() -> None:
    pipeline = _make_pipeline()

    await _call_tool(pipeline)

    assert _passed_context_window(pipeline) == 1


@pytest.mark.asyncio
async def test_search_with_context_graph_mode_checked_before_context_window() -> None:
    pipeline = _make_pipeline()

    payload = await _call_tool(pipeline, context_window=DEFAULT_CONTEXT_WINDOW_MAX + 1, graph_mode="naive")

    assert payload["code"] == "graph_mode_not_supported"
    pipeline.search_with_context.assert_not_called()


@pytest.mark.asyncio
async def test_search_with_context_scope_filter_checked_before_context_window() -> None:
    pipeline = _make_pipeline()

    payload = await _call_tool(pipeline, context_window=DEFAULT_CONTEXT_WINDOW_MAX + 1, scope_filter="*")

    assert payload["code"] == "invalid_scope_filter"
    pipeline.search_with_context.assert_not_called()


def test_search_with_context_docstring_states_the_bound() -> None:
    import archon_search.server.mcp as mcp_module

    with stubbed_fastmcp():
        tool = mcp_module.create_app(_make_pipeline(), "default", writer=None).tools["search_with_context"]

    doc = tool.__doc__ or ""
    assert "[0, [search].context_window_max]" in doc
    assert f"default {DEFAULT_CONTEXT_WINDOW_MAX}" in doc


def test_search_with_context_published_schema_states_the_bound() -> None:
    """The real FastMCP inputSchema documents the bound without minimum/maximum keys,
    so out-of-range values reach the tool and return code="validation_error"."""
    tool = registered_tools(None)["search_with_context"]
    prop = tool.to_mcp_tool().inputSchema["properties"]["context_window"]

    assert prop["type"] == "integer"
    assert prop["default"] == DEFAULT_CONTEXT_WINDOW
    assert "[0, [search].context_window_max]" in prop["description"]
    assert f"default {DEFAULT_CONTEXT_WINDOW_MAX}" in prop["description"]
    assert "minimum" not in prop
    assert "maximum" not in prop
