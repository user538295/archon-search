"""Characterization tests for the MCP ``search`` tool as it behaves before it moves onto the shared service (S60).

They pin today's behaviour, including the parts that differ from REST. The rewiring task deletes or rewrites each of
them on purpose; until then a failure here means the tool's behaviour changed without anyone deciding it should.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import archon_search.services.search_service as search_service_mod
from archon_search.collection_meta import CollectionMeta
from archon_search.config import GraphConfig, SearchConfig
from archon_search.pipeline import FanoutTimeoutError, SearchPipelineResult
from archon_search.server import mcp as mcp_module
from tests._fastmcp_stub import StubFastMCP, stubbed_fastmcp

# Distinct from the request namespace ("default"), so a fallback to the namespace or a hard-coded literal cannot pass.
_DEFAULT_COLLECTION = "configured-default-collection"
_REQUEST_NAMESPACE = "default"
# Every bound the tool could apply is patched below this; the outer wait is longer, so it only fires for an unbounded hang.
_TINY_BOUND_SECONDS = 0.05
_OUTER_WAIT_SECONDS = 0.3

_GRAPH_DISABLED_TEXT = "graph_mode requires [graph] enabled=true"
_SCOPE_FILTER_GRAPH_MODE_TEXT = (
    "scope_filter is not supported with graph_mode; use scope_filter without graph_mode or use graph_mode without scope_filter"
)
_WHITESPACE_COLLECTION_TEXT = "collection names must not be whitespace"


def _empty_result() -> SearchPipelineResult:
    return SearchPipelineResult(results=[], acl_filtered=False)


def _make_pipeline() -> Any:
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.search = AsyncMock(return_value=_empty_result())
    pipeline.search_many = AsyncMock(return_value=_empty_result())
    pipeline.warmup_models = AsyncMock(return_value=True)
    return pipeline


def _make_app(
    pipeline: Any,
    *,
    graph_enabled: bool = False,
    writer: Any = None,
    embedder_cache: Any = None,
) -> StubFastMCP:
    config = SearchConfig()
    config.graph = GraphConfig(enabled=graph_enabled)
    with stubbed_fastmcp():
        return mcp_module.create_app(
            pipeline, _DEFAULT_COLLECTION, writer=writer, config=config, embedder_cache=embedder_cache
        )


async def _never_finishes(*_args: Any, **_kwargs: Any) -> Any:
    await asyncio.Event().wait()  # never set: outlives any bound
    raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_mcp_search_old_check_order_graph_before_scope_rules() -> None:
    """With graph off, ``graph_disabled`` wins over every scope rule (REST checks the scope rules first), in MCP's texts."""
    pipeline = _make_pipeline()
    app = _make_app(pipeline, graph_enabled=False)
    search = app.tools["search"]

    invalid_scope = await search(query="q", collection="col", graph_mode="naive", scope_filter="*")
    valid_scope = await search(query="q", collection="col", graph_mode="naive", scope_filter="user:alice")
    whitespace_name = await search(query="q", collections=["  "])
    scope_with_graph = await _make_app(pipeline, graph_enabled=True).tools["search"](
        query="q", collection="col", graph_mode="naive", scope_filter="user:alice"
    )

    assert invalid_scope == {"error": _GRAPH_DISABLED_TEXT, "code": "graph_disabled"}
    assert valid_scope == {"error": _GRAPH_DISABLED_TEXT, "code": "graph_disabled"}
    assert whitespace_name == {"error": _WHITESPACE_COLLECTION_TEXT, "code": "validation_error"}
    assert scope_with_graph == {
        "error": _SCOPE_FILTER_GRAPH_MODE_TEXT,
        "code": "scope_filter_graph_mode_incompatible",
    }
    pipeline.get_collection_meta.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_search_ppr_with_collections_reaches_search_many() -> None:
    """``ppr`` with ``collections`` is not rejected: it reaches ``search_many`` carrying ``graph_mode="ppr"``."""
    pipeline = _make_pipeline()
    app = _make_app(pipeline, graph_enabled=True)

    result = await app.tools["search"](query="q", collections=["a", "b"], graph_mode="ppr")

    assert "error" not in result, result
    pipeline.search_many.assert_awaited_once()
    assert pipeline.search_many.call_args.args[1] == ["a", "b"]
    assert pipeline.search_many.call_args.kwargs["graph_mode"] == "ppr"
    pipeline.search.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("multi_collection", [False, True], ids=["single", "multi"])
async def test_mcp_search_timeout_records_no_telemetry(
    monkeypatch: pytest.MonkeyPatch, multi_collection: bool
) -> None:
    """A pipeline overrun answers the ``timeout`` code and enqueues no telemetry entry, on either path."""
    monkeypatch.setattr(mcp_module, "_SEARCH_TIMEOUT_SECONDS", _TINY_BOUND_SECONDS)
    pipeline = _make_pipeline()
    pipeline.search = AsyncMock(side_effect=_never_finishes)
    pipeline.search_many = AsyncMock(side_effect=_never_finishes)
    writer = MagicMock()
    app = _make_app(pipeline, writer=writer)
    where = {"collections": ["a", "b"]} if multi_collection else {"collection": "col"}

    result = await app.tools["search"](query="q", **where)

    assert result == {"error": "search timed out", "code": "timeout"}
    assert writer.mock_calls == []


@pytest.mark.asyncio
async def test_mcp_search_multi_path_fanout_timeout_records_no_telemetry() -> None:
    """The fan-out's own timeout answers the same ``timeout`` code, again without telemetry."""
    pipeline = _make_pipeline()
    pipeline.search_many = AsyncMock(side_effect=FanoutTimeoutError("fan-out"))
    writer = MagicMock()
    app = _make_app(pipeline, writer=writer)

    result = await app.tools["search"](query="q", collections=["a", "b"])

    assert result == {"error": "search timed out", "code": "timeout"}
    assert writer.mock_calls == []


@pytest.mark.asyncio
async def test_mcp_search_single_path_embedder_wait_is_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A never-finishing embedder cache hangs the single-collection path until an outer ``wait_for`` fires.

    Both bounds that exist elsewhere (the search budget and the service's embedder hand-off wait) are patched below
    the outer wait, so only the absence of a bound on this path lets the outer ``wait_for`` be the one that ends it.
    """
    monkeypatch.setattr(mcp_module, "_SEARCH_TIMEOUT_SECONDS", _TINY_BOUND_SECONDS)
    monkeypatch.setattr(search_service_mod, "_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS", _TINY_BOUND_SECONDS)
    pipeline = _make_pipeline()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace=_REQUEST_NAMESPACE, active_embedding_model="model-X")
    )
    cache = MagicMock()
    cache.get_or_load = AsyncMock(side_effect=_never_finishes)
    app = _make_app(pipeline, embedder_cache=cache)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(app.tools["search"](query="q", collection="col"), timeout=_OUTER_WAIT_SECONDS)

    cache.get_or_load.assert_awaited_once_with("model-X")
    pipeline.search.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_search_without_collection_falls_back_to_the_default_collection() -> None:
    """With neither ``collection`` nor ``collections`` the tool searches ``create_app``'s default collection."""
    pipeline = _make_pipeline()
    app = _make_app(pipeline)

    result = await app.tools["search"](query="q")

    assert "error" not in result, result
    pipeline.get_collection_meta.assert_awaited_once_with(_DEFAULT_COLLECTION, namespace=_REQUEST_NAMESPACE)
    pipeline.search.assert_awaited_once()
    assert pipeline.search.call_args.args[1] == _DEFAULT_COLLECTION
