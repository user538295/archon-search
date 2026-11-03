"""``SearchService.search`` multi path (``collections``): the global embedder, excluded collections, the error kinds,
HyDE and RAG Fusion suppression, the ``search_multi`` telemetry entry and the failure log events."""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import ExcludedCollection
from archon_search.embedder_cache import EmbedderNotReadyError
from archon_search.filters import SearchFilters
from archon_search.observability import correlation_id
from archon_search.pipeline import (
    CollectionNotFoundError,
    FanoutTimeoutError,
    GraphCommunitiesNotBuiltError,
    MetadataLookupError,
    SearchPipelineResult,
)
from archon_search.services.request_hyde import HYDE_EXPANSION_FAILED_WARNING
from archon_search.services.search_budget import SearchBudgetExceeded, SearchWarmup
from archon_search.services.search_command import SearchCommand
from archon_search.services.search_service import SearchService
from archon_search.services.service_error import (
    COLLECTION_NOT_FOUND_DETAIL,
    SEARCH_FAILED_DETAIL,
    ServiceErrorKind,
)
from archon_search.telemetry.entry import FilterFlags
from tests._hyde_stubs import stub_hyde_generator
from tests.services.test_search_service import (
    _CALLER,
    _EXCEPTION_TEXT,
    _GLOBAL_MODEL,
    _HYDE_VECTOR,
    _NAMESPACE,
    _SERVICE_LOGGER,
    _SHORT_WAIT_SECONDS,
    _SLOW_SEARCH_SECONDS,
    _config,
    _failure,
    _hit,
)
from tests.services.test_search_service_telemetry import (
    _CORRELATION_ID,
    _QUERY_TEXT,
    _RAG_FUSION_QUERIES,
    _entries,
    _hasher,
    _records,
)

_COLLECTIONS = ("a", "b")
_PPR_ENTITIES_MATCHED = 4


def _multi_pipeline(result: SearchPipelineResult | None = None, *, warm: bool = True, rerank: bool = True) -> MagicMock:
    pipeline = MagicMock()
    pipeline.global_embedder = MagicMock()
    pipeline.embedder_is_warm = warm
    pipeline.warmup_models = AsyncMock(return_value=rerank)
    pipeline.get_collection_meta = AsyncMock()
    pipeline.search = AsyncMock()
    pipeline.search_many = AsyncMock(
        return_value=result
        if result is not None
        else SearchPipelineResult(results=[_hit(collection="a"), _hit(doc_id="d2", collection="b")], acl_filtered=False)
    )
    return pipeline


def _service(
    pipeline: MagicMock | None = None,
    *,
    writer: Any = None,
    hasher: Any = None,
    cache: MagicMock | None = None,
    hyde_generator: Any = None,
    hyde_enabled: bool = False,
    rag_fusion_generator: Any = None,
    rag_fusion: bool = False,
    graph: bool = False,
) -> SearchService:
    config = _config()
    config.graph.enabled = graph
    config.hyde.enabled = hyde_enabled or hyde_generator is not None
    config.rag_fusion.enabled = rag_fusion
    return SearchService(
        pipeline if pipeline is not None else _multi_pipeline(),
        config,
        embedder_cache=cache,
        hyde_generator=hyde_generator,
        rag_fusion_generator=rag_fusion_generator,
        telemetry_writer=writer,
        doc_id_hasher=hasher,
    )


def _command(**overrides: Any) -> SearchCommand:
    fields: dict[str, Any] = {"query": "q", "collection": None, "collections": _COLLECTIONS}
    fields.update(overrides)
    return SearchCommand(**fields)


def _failing(exc: BaseException) -> MagicMock:
    pipeline = _multi_pipeline()
    pipeline.search_many = AsyncMock(side_effect=exc)
    return pipeline


# --- embedder, excluded collections -------------------------------------------------------------------


async def test_multi_path_uses_the_global_embedder_and_reports_excluded_collections() -> None:
    excluded = ExcludedCollection(name="b", reason="embedding_model_mismatch")
    result = SearchPipelineResult(results=[_hit(collection="a")], acl_filtered=False, excluded_collections=[excluded])
    pipeline = _multi_pipeline(result)
    cache = MagicMock()
    cache.get_or_load = AsyncMock()

    outcome = await _service(pipeline, cache=cache).search(_command(), _CALLER)

    assert outcome.embedding_model == _GLOBAL_MODEL
    assert outcome.excluded_collections == (excluded,)
    assert [hit.result.collection for hit in outcome.results] == ["a"]
    cache.get_or_load.assert_not_called()
    pipeline.get_collection_meta.assert_not_called()
    pipeline.search.assert_not_called()
    assert pipeline.warmup_models.await_args.args == (None,)
    assert pipeline.search_many.await_args.args == ("q", ["a", "b"])
    assert pipeline.search_many.await_args.kwargs["namespace"] == _NAMESPACE


async def test_all_collections_excluded_returns_an_empty_result_without_error() -> None:
    excluded = [ExcludedCollection(name=name, reason="embedding_model_mismatch") for name in _COLLECTIONS]
    pipeline = _multi_pipeline(SearchPipelineResult(results=[], acl_filtered=False, excluded_collections=excluded))
    writer = MagicMock()

    outcome = await _service(pipeline, writer=writer).search(_command(), _CALLER)

    assert outcome.results == ()
    assert outcome.excluded_collections == tuple(excluded)
    (entry,) = _entries(writer)
    assert (entry.endpoint, entry.status, entry.result_count, entry.excluded_count) == ("search_multi", "ok", 0, 2)


async def test_multi_derived_fields_come_from_the_pipeline_result_and_the_command() -> None:
    result = SearchPipelineResult(
        results=[_hit()],
        acl_filtered=True,
        rag_fusion_applied=True,
        rag_fusion_queries_used=_RAG_FUSION_QUERIES,
        rag_fusion_attempted=True,
        rag_fusion_warning="rag fusion warning",
        graph_expansion_applied=True,
        ppr_entities_matched=_PPR_ENTITIES_MATCHED,
    )
    filters = SearchFilters(file_type="md")

    outcome = await _service(_multi_pipeline(result)).search(
        _command(filters=filters, include_metadata=True, acl_context=True), _CALLER
    )

    assert outcome.acl_filtered is True
    assert (outcome.rag_fusion_applied, outcome.rag_fusion_queries_used, outcome.rag_fusion_attempted) == (
        True,
        _RAG_FUSION_QUERIES,
        True,
    )
    assert outcome.graph_expansion_applied is True
    assert outcome.expansion_used is True
    assert outcome.expansion_warning == "rag fusion warning"
    assert outcome.applied_filters == filters
    assert outcome.ppr_entities_matched == _PPR_ENTITIES_MATCHED
    assert outcome.include_metadata is True
    assert all(hit.acl_gate is not None for hit in outcome.results)


# --- errors -------------------------------------------------------------------------------------------


async def test_multi_collection_not_found_maps_to_not_found_and_records_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = MagicMock()
    service = _service(_failing(CollectionNotFoundError(["b"])), writer=writer)

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        error = await _failure(service, _command())

    assert error.kind is ServiceErrorKind.NOT_FOUND
    assert error.detail == COLLECTION_NOT_FOUND_DETAIL
    assert "b" not in error.detail.split()
    writer.enqueue.assert_not_called()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_multi_metadata_failure_maps_to_metadata_store_error(caplog: pytest.LogCaptureFixture) -> None:
    boom = MetadataLookupError(RuntimeError(_EXCEPTION_TEXT))
    writer = MagicMock()
    service = _service(_failing(boom), writer=writer)

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        error = await _failure(service, _command(query=_QUERY_TEXT))

    assert error.kind is ServiceErrorKind.METADATA_STORE_ERROR
    assert error.__cause__ is boom
    assert _EXCEPTION_TEXT not in error.detail
    (entry,) = _entries(writer)
    assert (entry.endpoint, entry.status, entry.error_kind) == ("search", "internal_error", "other")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[1] is boom
    assert _QUERY_TEXT not in caplog.text


async def test_multi_graph_communities_not_built_maps_to_its_kind_with_the_typed_collection() -> None:
    service = _service(_failing(GraphCommunitiesNotBuiltError("b")), graph=True)

    error = await _failure(service, _command(graph_mode="local"))

    assert error.kind is ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT
    assert error.detail == GraphCommunitiesNotBuiltError.MESSAGE_TEMPLATE.format(collection="b")


async def test_multi_embedder_not_ready_maps_to_its_retryable_kind() -> None:
    writer = MagicMock()

    error = await _failure(_service(_failing(EmbedderNotReadyError("still loading")), writer=writer), _command())

    assert error.kind is ServiceErrorKind.EMBEDDER_NOT_READY
    (entry,) = _entries(writer)
    assert (entry.endpoint, entry.status, entry.error_kind) == ("search", "internal_error", "other")


@pytest.mark.parametrize("timeout", [FanoutTimeoutError(), SearchBudgetExceeded()], ids=["fanout", "budget"])
async def test_multi_timeout_maps_to_timeout_and_records_a_timeout_entry(
    timeout: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    writer = MagicMock()
    service = _service(_failing(timeout), writer=writer)

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        error = await _failure(service, _command(query=_QUERY_TEXT))

    assert error.kind is ServiceErrorKind.TIMEOUT
    (entry,) = _entries(writer)
    assert (entry.endpoint, entry.status, entry.error_kind) == ("search", "timeout", "timeout")
    (record,) = _records(caplog, "search_timeout")
    assert record.levelno == logging.ERROR
    assert record.collections == list(_COLLECTIONS)  # type: ignore[attr-defined]
    assert not hasattr(record, "collection")
    assert _QUERY_TEXT not in caplog.text


async def test_multi_budget_overrun_of_a_slow_search_maps_to_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from archon_search.services import search_service as search_service_module

    monkeypatch.setattr(search_service_module, "_SEARCH_TIMEOUT_SECONDS", _SHORT_WAIT_SECONDS)
    pipeline = _multi_pipeline()

    async def _slow(*_args: Any, **_kwargs: Any) -> SearchPipelineResult:
        await asyncio.sleep(_SLOW_SEARCH_SECONDS)
        raise AssertionError("unreachable")

    pipeline.search_many = AsyncMock(side_effect=_slow)

    error = await _failure(_service(pipeline), _command())

    assert error.kind is ServiceErrorKind.TIMEOUT


async def test_multi_unexpected_failure_maps_to_internal_and_records_internal_error_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    boom = RuntimeError(_EXCEPTION_TEXT)
    writer = MagicMock()
    service = _service(_failing(boom), writer=writer)

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        error = await _failure(service, _command(query=_QUERY_TEXT))

    assert error.kind is ServiceErrorKind.INTERNAL
    assert error.detail == SEARCH_FAILED_DETAIL
    assert _EXCEPTION_TEXT not in error.detail
    assert error.__cause__ is boom
    (entry,) = _entries(writer)
    assert (entry.endpoint, entry.status, entry.error_kind) == ("search", "internal_error", "other")
    (record,) = _records(caplog, "search_pipeline_failure")
    assert record.exc_info is not None
    assert record.exc_info[1] is boom
    assert _QUERY_TEXT not in caplog.text


async def test_multi_hyde_failure_maps_to_hyde_failed_and_records_internal_error_other() -> None:
    generator = stub_hyde_generator(side_effect=RuntimeError(_EXCEPTION_TEXT))
    writer = MagicMock()
    pipeline = _multi_pipeline()

    error = await _failure(_service(pipeline, writer=writer, hyde_generator=generator), _command(hyde=True))

    assert error.kind is ServiceErrorKind.HYDE_FAILED
    assert error.detail == SEARCH_FAILED_DETAIL
    assert isinstance(error.__cause__, RuntimeError)
    pipeline.search_many.assert_not_called()
    (entry,) = _entries(writer)
    assert (entry.endpoint, entry.status, entry.error_kind) == ("search", "internal_error", "other")


async def test_multi_a_cancelled_search_propagates_unrecorded() -> None:
    writer = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await _service(_failing(asyncio.CancelledError()), writer=writer).search(_command(), _CALLER)

    writer.enqueue.assert_not_called()


# --- telemetry ----------------------------------------------------------------------------------------


async def test_multi_success_records_search_multi_without_doc_ids_or_filter_flags() -> None:
    excluded = ExcludedCollection(name="b", reason="embedding_model_mismatch")
    result = SearchPipelineResult(
        results=[_hit(collection="a")],
        acl_filtered=False,
        excluded_collections=[excluded],
        rag_fusion_applied=True,
        rag_fusion_queries_used=_RAG_FUSION_QUERIES,
    )
    writer = MagicMock()
    token = correlation_id.set(_CORRELATION_ID)
    try:
        await _service(_multi_pipeline(result), writer=writer, hasher=_hasher).search(
            _command(filters=SearchFilters(file_type="md"), include_metadata=True), _CALLER
        )
    finally:
        correlation_id.reset(token)

    (entry,) = _entries(writer)
    assert entry.endpoint == "search_multi"
    assert entry.status == "ok"
    assert entry.collections == list(_COLLECTIONS)
    assert entry.fanout_count == 1
    assert entry.excluded_count == 1
    assert entry.result_count == 1
    assert entry.result_doc_ids is None
    assert entry.collection is None
    assert entry.filter_flags == FilterFlags()
    assert entry.rag_fusion_applied is True
    assert entry.rag_fusion_queries_used == _RAG_FUSION_QUERIES
    assert entry.correlation_id == _CORRELATION_ID
    assert entry.latency_ms >= 0


async def test_multi_a_failing_enqueue_never_breaks_the_request(caplog: pytest.LogCaptureFixture) -> None:
    writer = MagicMock()
    writer.enqueue.side_effect = OSError(_EXCEPTION_TEXT)

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        outcome = await _service(writer=writer).search(_command(), _CALLER)

    assert len(outcome.results) == 2
    assert "telemetry enqueue failed: OSError" in caplog.text
    assert _EXCEPTION_TEXT not in caplog.text


# --- HyDE, RAG Fusion, warm-up ------------------------------------------------------------------------


async def test_multi_rag_fusion_wins_over_hyde_and_graph_mode_suppresses_hyde() -> None:
    generator = stub_hyde_generator(return_value=_HYDE_VECTOR)
    service = _service(hyde_generator=generator, rag_fusion_generator=MagicMock(), rag_fusion=True, graph=True)

    rag_outcome = await service.search(_command(hyde=True, rag_fusion=True), _CALLER)
    graph_outcome = await service.search(_command(hyde=True, graph_mode="local"), _CALLER)

    generator.generate.assert_not_called()
    assert (rag_outcome.hyde_applied, graph_outcome.hyde_applied) == (False, False)
    assert rag_outcome.expansion_warning is None
    assert graph_outcome.expansion_warning is None


async def test_multi_hyde_without_a_generator_falls_back_silently_with_the_warning() -> None:
    pipeline = _multi_pipeline()

    outcome = await _service(pipeline, hyde_enabled=True).search(_command(hyde=True), _CALLER)

    assert outcome.hyde_applied is False
    assert outcome.expansion_warning == HYDE_EXPANSION_FAILED_WARNING
    assert pipeline.search_many.await_args.kwargs["query_vector"] is None


async def test_multi_hyde_vector_reaches_search_many_and_is_reported_applied() -> None:
    generator = stub_hyde_generator(return_value=_HYDE_VECTOR)
    pipeline = _multi_pipeline()

    outcome = await _service(pipeline, hyde_generator=generator).search(_command(hyde=True), _CALLER)

    assert pipeline.search_many.await_args.kwargs["query_vector"] == _HYDE_VECTOR
    assert outcome.hyde_applied is True
    assert outcome.expansion_used is True


async def test_multi_request_options_reach_search_many_unchanged() -> None:
    pipeline = _multi_pipeline()
    filters = SearchFilters(file_type="md")
    rag_generator = MagicMock()

    await _service(pipeline, rag_fusion_generator=rag_generator, rag_fusion=True, graph=True).search(
        _command(filters=filters, rag_fusion=True, graph_mode="local"), _CALLER
    )

    kwargs = pipeline.search_many.await_args.kwargs
    assert kwargs["filters"] == filters
    assert kwargs["rag_fusion"] is True
    assert kwargs["rag_fusion_generator"] is rag_generator
    assert kwargs["graph_mode"] == "local"
    assert kwargs["scope_filter"] is None


@pytest.mark.parametrize(
    ("warm", "rerank"), [(True, True), (False, True), (True, False), (False, False)], ids=lambda value: str(value)
)
async def test_multi_fts_only_is_passed_to_search_many_unchanged(warm: bool, rerank: bool) -> None:
    pipeline = _multi_pipeline(warm=warm, rerank=rerank)

    await _service(pipeline).search(_command(), _CALLER)

    kwargs = pipeline.search_many.await_args.kwargs
    assert kwargs["fts_only"] is (not warm)
    assert kwargs["rerank"] is rerank


async def test_multi_fts_only_follows_the_warm_up_result_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from archon_search.services import search_service as search_service_module

    pipeline = _multi_pipeline()
    warm_up = AsyncMock(return_value=SearchWarmup(rerank=False, fts_only=True))
    monkeypatch.setattr(search_service_module, "warmup_for_search", warm_up)

    await _service(pipeline).search(_command(), _CALLER)

    warm_up.assert_awaited_once_with(pipeline)
    kwargs = pipeline.search_many.await_args.kwargs
    assert (kwargs["fts_only"], kwargs["rerank"]) == (True, False)


# --- more failure shapes ------------------------------------------------------------------------------


async def test_multi_a_timeout_error_raised_by_the_pipeline_is_internal_not_a_budget_overrun() -> None:
    writer = MagicMock()

    error = await _failure(_service(_failing(TimeoutError("socket ETIMEDOUT")), writer=writer), _command())

    assert error.kind is ServiceErrorKind.INTERNAL
    (entry,) = _entries(writer)
    assert (entry.status, entry.error_kind) == ("internal_error", "other")


async def test_multi_embedder_not_ready_is_logged_as_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        await _failure(_service(_failing(EmbedderNotReadyError("still loading"))), _command())

    warnings = [r for r in caplog.records if "embedder not ready" in r.getMessage()]
    assert len(warnings) == 1
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_multi_hyde_embedder_not_ready_keeps_its_retryable_kind() -> None:
    generator = stub_hyde_generator(side_effect=EmbedderNotReadyError("wedged"))
    pipeline = _multi_pipeline()

    error = await _failure(_service(pipeline, hyde_generator=generator), _command(hyde=True))

    assert error.kind is ServiceErrorKind.EMBEDDER_NOT_READY
    pipeline.search_many.assert_not_called()


async def test_multi_hyde_failure_is_logged_with_its_exception_and_never_the_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    boom = RuntimeError(_EXCEPTION_TEXT)
    generator = stub_hyde_generator(side_effect=boom)

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        await _failure(_service(hyde_generator=generator), _command(query=_QUERY_TEXT, hyde=True))

    (record,) = _records(caplog, "search_pipeline_failure")
    assert record.exc_info is not None
    assert record.exc_info[1] is boom
    assert _QUERY_TEXT not in caplog.text


async def test_multi_not_found_graph_communities_and_metadata_failures_log_no_pipeline_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failures = [CollectionNotFoundError(["b"]), GraphCommunitiesNotBuiltError("b"), MetadataLookupError(OSError("x"))]

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        for failure in failures:
            await _failure(_service(_failing(failure), graph=True), _command(graph_mode="local"))

    assert _records(caplog, "search_pipeline_failure") == []


async def test_multi_graph_communities_not_built_is_not_recorded() -> None:
    writer = MagicMock()

    await _failure(_service(_failing(GraphCommunitiesNotBuiltError("b")), writer=writer, graph=True), _command(graph_mode="local"))

    writer.enqueue.assert_not_called()


async def test_multi_cancelling_the_search_cancels_the_in_flight_fan_out_unrecorded() -> None:
    writer = MagicMock()
    pipeline = _multi_pipeline()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _hang(*_args: Any, **_kwargs: Any) -> SearchPipelineResult:
        started.set()
        try:
            await asyncio.sleep(_SLOW_SEARCH_SECONDS)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    pipeline.search_many = _hang
    task = asyncio.ensure_future(_service(pipeline, writer=writer).search(_command(), _CALLER))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    writer.enqueue.assert_not_called()


# --- HyDE wiring and request forwarding ---------------------------------------------------------------


async def test_multi_hyde_is_embedded_with_the_global_embedder() -> None:
    generator = stub_hyde_generator(return_value=_HYDE_VECTOR)
    pipeline = _multi_pipeline()

    await _service(pipeline, hyde_generator=generator).search(_command(hyde=True), _CALLER)

    assert generator.generate.await_args.kwargs["embedder"] is pipeline.global_embedder


async def test_multi_cold_global_embedder_skips_hyde_and_reports_the_warning() -> None:
    generator = stub_hyde_generator(return_value=_HYDE_VECTOR)
    pipeline = _multi_pipeline(warm=False)

    outcome = await _service(pipeline, hyde_generator=generator).search(_command(hyde=True), _CALLER)

    generator.generate.assert_not_called()
    assert outcome.hyde_applied is False
    assert outcome.expansion_warning == HYDE_EXPANSION_FAILED_WARNING
    assert pipeline.search_many.await_args.kwargs["query_vector"] is None


async def test_multi_rag_fusion_and_graph_mode_still_reach_search_many_when_hyde_is_suppressed() -> None:
    generator = stub_hyde_generator(return_value=_HYDE_VECTOR)
    pipeline = _multi_pipeline()
    service = _service(pipeline, hyde_generator=generator, rag_fusion_generator=MagicMock(), rag_fusion=True, graph=True)

    await service.search(_command(hyde=True, rag_fusion=True), _CALLER)
    rag_kwargs = pipeline.search_many.await_args.kwargs
    await service.search(_command(hyde=True, graph_mode="local"), _CALLER)
    graph_kwargs = pipeline.search_many.await_args.kwargs

    assert (rag_kwargs["rag_fusion"], rag_kwargs["query_vector"]) == (True, None)
    assert (graph_kwargs["graph_mode"], graph_kwargs["query_vector"]) == ("local", None)


async def test_multi_names_are_stripped_and_de_duplicated_before_the_fan_out_and_the_entry() -> None:
    writer = MagicMock()
    pipeline = _multi_pipeline()

    await _service(pipeline, writer=writer).search(_command(collections=(" a", "a", "b ")), _CALLER)

    assert pipeline.search_many.await_args.args == ("q", ["a", "b"])
    (entry,) = _entries(writer)
    assert entry.collections == ["a", "b"]
    assert entry.fanout_count == len(_COLLECTIONS)
