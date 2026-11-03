"""The MCP ``search`` tool on the shared ``SearchService`` (BE-15, BE-16): validation parity with REST, the one MCP error
table, the bounded embedder wait, telemetry on both recorded kinds, the identity gate and REST's response fields."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import archon_search.services.search_service as search_service_mod
from archon_search._types import SearchResult
from archon_search.collection_meta import CollectionMeta
from archon_search.config import GraphConfig, SearchConfig
from archon_search.embedder_cache import EMBEDDER_NOT_READY_CODE, EmbedderNotReadyError
from archon_search.filters import SearchFilters
from archon_search.pipeline import (
    METADATA_STORE_UNAVAILABLE_CODE,
    METADATA_STORE_UNAVAILABLE_DETAIL,
    CollectionNotFoundError,
    SearchPipelineResult,
)
from archon_search.server import mcp as mcp_module
from archon_search.server.routes_search import _render_outcome
from archon_search.services.authz import AuthIdentity
from archon_search.services.container import build_search_service
from archon_search.services.search_command import SearchCommand
from archon_search.services.validators import validate_scope_filter
from tests._fastmcp_stub import StubFastMCP, stubbed_fastmcp
from tests.pipeline.conftest import make_pipeline

_NAMESPACE = "default"
_COLLECTION = "col"
_CALLER = AuthIdentity(namespace=_NAMESPACE, admin=True)
_TINY_BOUND_SECONDS = 0.05
_MAX_FANOUT = 2
_CALL_GUARD_SECONDS = 5
_LONG_BUDGET_SECONDS = 30

_NOT_FOUND = {"error": "collection not found", "code": "not_found"}
_SEARCH_FAILED = {"error": "search failed", "code": "internal_error"}
_TIMED_OUT = {"error": "search timed out", "code": "timeout"}
_GRAPH_DISABLED_TEXT = "graph_mode requires [graph] enabled=true in server config"
_SCOPE_FILTER_GRAPH_MODE_TEXT = "scope_filter is not supported with graph_mode"
_PPR_WITH_COLLECTIONS_TEXT = "graph_mode is not supported with multi-collection fanout; use a single collection"


def _empty_result() -> SearchPipelineResult:
    return SearchPipelineResult(results=[], acl_filtered=False)


def _pipeline() -> Any:
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.search = AsyncMock(return_value=_empty_result())
    pipeline.search_many = AsyncMock(return_value=_empty_result())
    pipeline.warmup_models = AsyncMock(return_value=True)
    return pipeline


def _config(*, graph_enabled: bool = False) -> SearchConfig:
    config = SearchConfig()
    config.graph = GraphConfig(enabled=graph_enabled)
    config.max_fanout = _MAX_FANOUT
    return config


def _app(pipeline: Any, *, config: SearchConfig | None = None, **kwargs: Any) -> StubFastMCP:
    with stubbed_fastmcp():
        return mcp_module.create_app(pipeline, "configured-default", config=config or _config(), **kwargs)


async def _never_finishes(*_args: Any, **_kwargs: Any) -> Any:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


# --- selection and validation -------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "text"),
    [
        ({}, "supply either collection or collections"),
        ({"collection": "a", "collections": ["a"]}, "supply either collection or collections, not both"),
    ],
    ids=["neither", "both"],
)
async def test_mcp_search_requires_exactly_one_of_collection_and_collections(
    arguments: dict[str, Any], text: str
) -> None:
    pipeline = _pipeline()

    result = await _app(pipeline).tools["search"](query="q", **arguments)

    assert result == {"error": text, "code": "validation_error"}
    pipeline.get_collection_meta.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_search_without_a_collection_never_uses_the_factory_default_or_the_literal_default() -> None:
    pipeline = _pipeline()

    result = await _app(pipeline).tools["search"](query="q")

    assert result == {"error": "supply either collection or collections", "code": "validation_error"}
    pipeline.get_collection_meta.assert_not_called()
    pipeline.search.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_search_rejects_empty_query_and_ppr_with_collections_with_rests_text() -> None:
    pipeline = _pipeline()
    app = _app(pipeline, config=_config(graph_enabled=True))

    empty_query = await app.tools["search"](query="   ", collection="a")
    ppr_multi = await app.tools["search"](query="q", collections=["a", "b"], graph_mode="ppr")

    assert empty_query == {"error": "query must not be empty", "code": "validation_error"}
    assert ppr_multi == {"error": _PPR_WITH_COLLECTIONS_TEXT, "code": "validation_error"}
    pipeline.search_many.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_search_check_order_matches_rests() -> None:
    """Scope-filter rules come before ``graph_disabled`` (REST's order), each in REST's text."""
    pipeline = _pipeline()
    graph_off = _app(pipeline, config=_config(graph_enabled=False)).tools["search"]
    graph_on = _app(pipeline, config=_config(graph_enabled=True)).tools["search"]

    invalid_scope = await graph_off(query="q", collection="a", graph_mode="naive", scope_filter="*")
    valid_scope = await graph_off(query="q", collection="a", graph_mode="naive", scope_filter="user:alice")
    plain_graph = await graph_off(query="q", collection="a", graph_mode="naive")
    whitespace_name = await graph_off(query="q", collections=["  "])
    scope_with_graph = await graph_on(query="q", collection="a", graph_mode="naive", scope_filter="user:alice")
    bad_mode = await graph_on(query="q", collection="a", graph_mode="bogus")

    assert invalid_scope == {"error": validate_scope_filter("*"), "code": "invalid_scope_filter"}
    assert valid_scope == {"error": _SCOPE_FILTER_GRAPH_MODE_TEXT, "code": "scope_filter_graph_mode_incompatible"}
    assert scope_with_graph == valid_scope
    assert plain_graph == {"error": _GRAPH_DISABLED_TEXT, "code": "graph_disabled"}
    assert whitespace_name == {"error": "collection names must not be empty or whitespace", "code": "validation_error"}
    assert bad_mode["code"] == "invalid_graph_mode"
    pipeline.get_collection_meta.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_search_ppr_with_collections_is_rejected_before_hyde() -> None:
    pipeline = _pipeline()
    generator = MagicMock()
    generator.generate = AsyncMock()
    app = _app(pipeline, config=_config(graph_enabled=True), hyde_generator=generator)

    result = await app.tools["search"](query="q", collections=["a", "b"], graph_mode="ppr", hyde=True)

    assert result == {"error": _PPR_WITH_COLLECTIONS_TEXT, "code": "validation_error"}
    generator.generate.assert_not_called()
    pipeline.search_many.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_search_over_max_fanout_text_equals_rests() -> None:
    pipeline = _pipeline()

    over = await _app(pipeline).tools["search"](query="q", collections=["a", "b", "c"])
    deduped = await _app(pipeline).tools["search"](query="q", collections=["a", "a", "b", "b"])

    assert over == {"error": f"collections length exceeds maximum of {_MAX_FANOUT}", "code": "validation_error"}
    assert "error" not in deduped
    pipeline.search_many.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_search_strips_a_padded_query_and_collection_and_rejects_a_whitespace_only_collection() -> None:
    pipeline = _pipeline()
    app = _app(pipeline)

    padded = await app.tools["search"](query="  q  ", collection="  col  ")
    blank = await app.tools["search"](query="q", collection="   ")

    assert "error" not in padded
    pipeline.get_collection_meta.assert_awaited_once_with(_COLLECTION, namespace=_NAMESPACE)
    assert pipeline.search.call_args.args[0] == "q"
    assert pipeline.search.call_args.args[1] == _COLLECTION
    assert blank == {"error": "collection must not be empty", "code": "validation_error"}


# --- no configuration ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_without_config_answers_the_fixed_internal_error_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pipeline = _pipeline()
    pipeline.get_all_collections_meta = AsyncMock(return_value=[])
    with stubbed_fastmcp():
        app = mcp_module.create_app(pipeline, "configured-default")

    with caplog.at_level(logging.ERROR, logger=mcp_module.logger.name):
        result = await app.tools["search"](query="q", collection=_COLLECTION)
        others = await app.tools["list_collections"]()

    assert result == _SEARCH_FAILED
    assert others == []
    events = [r for r in caplog.records if getattr(r, "event_type", None) == "search_service_unavailable"]
    assert len(events) == 1
    assert events[0].levelno == logging.ERROR
    pipeline.search.assert_not_called()


# --- embedder: cold wait and the not-ready kind -----------------------------------------------------------


@asynccontextmanager
async def _gated_load() -> AsyncIterator[MagicMock]:
    """A cache whose load blocks until the context exits (the service's shielded load keeps running meanwhile)."""
    gate = asyncio.Event()

    async def load(_model: str) -> Any:
        await gate.wait()
        return MagicMock()

    cache = MagicMock()
    cache.get_or_load = AsyncMock(side_effect=load)
    try:
        yield cache
    finally:
        gate.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_mcp_search_cold_embedder_degrades_to_fts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_service_mod, "_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS", _TINY_BOUND_SECONDS)
    pipeline = _pipeline()
    pipeline.global_embedder = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name=_COLLECTION, namespace=_NAMESPACE, active_embedding_model="model-X")
    )

    async with _gated_load() as cache:
        app = _app(pipeline, embedder_cache=cache)
        result = await asyncio.wait_for(
            app.tools["search"](query="q", collection=_COLLECTION), timeout=_CALL_GUARD_SECONDS
        )

    assert "error" not in result, result
    assert pipeline.search.call_args.kwargs["fts_only"] is True


@pytest.mark.asyncio
async def test_mcp_search_multi_path_embedder_not_ready_is_embedder_not_ready() -> None:
    pipeline = _pipeline()
    pipeline.search_many = AsyncMock(side_effect=EmbedderNotReadyError("wedged"))

    result = await _app(pipeline).tools["search"](query="q", collections=["a", "b"])

    assert result["code"] == EMBEDDER_NOT_READY_CODE
    assert set(result) == {"error", "code"}


@pytest.mark.asyncio
async def test_mcp_search_single_path_metadata_failure_is_metadata_store_error() -> None:
    pipeline = _pipeline()
    pipeline.get_collection_meta = AsyncMock(side_effect=RuntimeError("lance error at /var/data/store"))

    result = await _app(pipeline).tools["search"](query="q", collection=_COLLECTION)

    assert result == {"error": METADATA_STORE_UNAVAILABLE_DETAIL, "code": METADATA_STORE_UNAVAILABLE_CODE}


# --- timeouts, internal errors and their telemetry ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("multi_collection", [False, True], ids=["single", "multi"])
async def test_mcp_search_timeout_and_internal_error_are_recorded(
    monkeypatch: pytest.MonkeyPatch, multi_collection: bool
) -> None:
    monkeypatch.setattr(search_service_mod, "_SEARCH_TIMEOUT_SECONDS", _TINY_BOUND_SECONDS)
    where = {"collections": ["a", "b"]} if multi_collection else {"collection": _COLLECTION}
    method = "search_many" if multi_collection else "search"

    timeout_pipeline = _pipeline()
    setattr(timeout_pipeline, method, AsyncMock(side_effect=_never_finishes))
    timeout_writer = MagicMock()
    timed_out = await _app(timeout_pipeline, writer=timeout_writer).tools["search"](query="q", **where)

    failing_pipeline = _pipeline()
    setattr(failing_pipeline, method, AsyncMock(side_effect=RuntimeError("boom at /var/data")))
    failing_writer = MagicMock()
    failed = await _app(failing_pipeline, writer=failing_writer).tools["search"](query="q", **where)

    assert timed_out == _TIMED_OUT
    assert failed == _SEARCH_FAILED
    for writer, status in ((timeout_writer, "timeout"), (failing_writer, "internal_error")):
        writer.enqueue.assert_called_once()
        entry = writer.enqueue.call_args.args[0]
        assert (entry.endpoint, entry.status) == ("search", status)


@pytest.mark.asyncio
@pytest.mark.parametrize("multi_collection", [False, True], ids=["single", "multi"])
async def test_mcp_search_pipeline_overrun_answers_the_timeout_code_through_the_service_alias(
    monkeypatch: pytest.MonkeyPatch, multi_collection: bool
) -> None:
    """Patching the service module's budget (raising on a missing name) reaches the run: never a silent no-op."""
    monkeypatch.setattr(search_service_mod, "_SEARCH_TIMEOUT_SECONDS", _TINY_BOUND_SECONDS)
    pipeline = _pipeline()
    pipeline.search = AsyncMock(side_effect=_never_finishes)
    pipeline.search_many = AsyncMock(side_effect=_never_finishes)
    where = {"collections": ["a", "b"]} if multi_collection else {"collection": _COLLECTION}

    result = await asyncio.wait_for(_app(pipeline).tools["search"](query="q", **where), timeout=_CALL_GUARD_SECONDS)

    assert result == _TIMED_OUT

    # Guard: the MCP module's own (stale) alias of the budget is not the one the search tool honours.
    async def _slower_than_the_stale_bound(*_args: Any, **_kwargs: Any) -> SearchPipelineResult:
        await asyncio.sleep(_TINY_BOUND_SECONDS * 2)
        return _empty_result()

    monkeypatch.setattr(search_service_mod, "_SEARCH_TIMEOUT_SECONDS", _LONG_BUDGET_SECONDS)
    monkeypatch.setattr(mcp_module, "_SEARCH_TIMEOUT_SECONDS", _TINY_BOUND_SECONDS)
    quick_pipeline = _pipeline()
    quick_pipeline.search = AsyncMock(side_effect=_slower_than_the_stale_bound)
    quick_pipeline.search_many = AsyncMock(side_effect=_slower_than_the_stale_bound)

    still_answers = await _app(quick_pipeline).tools["search"](query="q", **where)

    assert "error" not in still_answers, still_answers


# --- identity ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_mcp_auth_context")
@pytest.mark.parametrize("with_config", [False, True], ids=["no_config", "with_config"])
async def test_mcp_search_missing_identity_is_refused_before_the_service(with_config: bool) -> None:
    from fastmcp.exceptions import ToolError

    pipeline = _pipeline()
    writer = MagicMock()
    config = _config() if with_config else None  # with a config the service could be built: the refusal still comes first
    with stubbed_fastmcp():
        app = mcp_module.create_app(pipeline, "configured-default", config=config, writer=writer)

    with pytest.raises(ToolError, match=mcp_module.NO_AUTH_CONTEXT_DETAIL):
        await app.tools["search"](query="")  # invalid on every other count: no validation ran first

    pipeline.get_collection_meta.assert_not_called()
    writer.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_namespace_isolation_through_the_identity_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def meta_of(name: str, *, namespace: str) -> CollectionMeta | None:
        seen.append(namespace)
        return CollectionMeta(name=name, namespace="tenant-a") if namespace == "tenant-a" else None

    pipeline = _pipeline()
    pipeline.get_collection_meta = AsyncMock(side_effect=meta_of)
    app = _app(pipeline)

    monkeypatch.setattr(mcp_module, "_get_request_identity", lambda: AuthIdentity(namespace="tenant-b", admin=False))
    other = await app.tools["search"](query="q", collection="theirs")
    monkeypatch.setattr(mcp_module, "_get_request_identity", lambda: AuthIdentity(namespace="tenant-a", admin=False))
    own = await app.tools["search"](query="q", collection="theirs")

    assert other == _NOT_FOUND
    assert "error" not in own
    assert seen == ["tenant-b", "tenant-a"]


# --- not found ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_not_found_has_the_fixed_text_on_both_paths_and_for_an_invalid_name(connected_store: Any) -> None:
    writer = MagicMock()
    real = make_pipeline(connected_store)
    app = _app(real, writer=writer)

    invalid = await app.tools["search"](query="q", collection="bad name!")
    unknown = await app.tools["search"](query="q", collection="ghost-collection")
    multi = await app.tools["search"](query="q", collections=["ghost-collection", "other-ghost"])

    mocked = _pipeline()
    mocked.search_many = AsyncMock(side_effect=CollectionNotFoundError("ghost-collection"))
    mocked_multi = await _app(mocked).tools["search"](query="q", collections=["a", "b"])

    for answer in (invalid, unknown, multi, mocked_multi):
        assert answer == _NOT_FOUND
    writer.enqueue.assert_not_called()


# --- response fields ---------------------------------------------------------------------------------------


def _hit() -> SearchResult:
    return SearchResult(
        doc_id="d1",
        chunk_id="c1",
        text="text",
        score=0.5,
        source_path="a.md",
        file_type="md",
        metadata={"k": "v"},
        acl=["group:eng"],
        acl_source="frontmatter",
        acl_warning=["w"],
        collection=_COLLECTION,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("with_filter", [False, True], ids=["no-filter", "filter"])
async def test_mcp_search_response_carries_rests_fields_and_per_result_acl_gate(with_filter: bool) -> None:
    pipeline = _pipeline()
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[_hit()], acl_filtered=True))
    config = _config()
    app = _app(pipeline, config=config)
    filter_arguments = {"file_type": "md"} if with_filter else {}

    plain = await app.tools["search"](query="q", collection=_COLLECTION, **filter_arguments)
    gated = await app.tools["search"](query="q", collection=_COLLECTION, acl_context=True, **filter_arguments)

    service = build_search_service(pipeline, config)
    command = SearchCommand(
        query="q",
        collection=_COLLECTION,
        filters=SearchFilters(file_type="md") if with_filter else None,
        acl_context=True,
    )
    rest = _render_outcome(await service.search(command, _CALLER)).model_dump(mode="json")

    assert set(gated) == set(rest)
    for name in ("embedding_model", "rag_fusion_applied", "rag_fusion_queries_used", "rag_fusion_attempted", "applied_filters"):
        assert gated[name] == rest[name], name
    assert (gated["applied_filters"] is not None) is with_filter
    assert plain["results"][0]["acl_gate"] is None
    assert gated["results"][0]["acl_gate"] == rest["results"][0]["acl_gate"]
    assert gated["results"][0]["acl_gate"] == {
        "allowed_principals": ["group:eng"],
        "source": "frontmatter",
        "sidecar_path": None,
        "warnings": ["w"],
    }
