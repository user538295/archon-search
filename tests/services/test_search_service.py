"""``SearchService.search`` (single path): collection lookup, embedder selection, warm-up, HyDE and RAG Fusion
resolution, the budget, the derived fields, the ``AclGate`` and the error kinds."""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._types import ExcludedCollection, SearchResult
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.embedder_cache import EmbedderNotReadyError
from archon_search.filters import SearchFilters
from archon_search.pipeline import GraphCommunitiesNotBuiltError, SearchPipelineResult
from archon_search.services import search_service as search_service_module
from archon_search.services.authz import AuthIdentity
from archon_search.services.request_hyde import HYDE_EXPANSION_FAILED_WARNING
from archon_search.services.search_budget import SearchWarmup
from archon_search.services.search_command import SearchCommand
from archon_search.services.search_service import AclGate, SearchOutcome, SearchService
from archon_search.services.service_error import (
    COLLECTION_NOT_FOUND_DETAIL,
    RETRY_AFTER_SECONDS,
    SEARCH_FAILED_DETAIL,
    ServiceError,
    ServiceErrorKind,
)
from tests._hyde_stubs import stub_hyde_generator

_NAMESPACE = "tenant"
_CALLER = AuthIdentity(namespace=_NAMESPACE, admin=False)
_COLLECTION = "docs"
_COLLECTION_MODEL = "collection-model"
_GLOBAL_MODEL = "global-model"
_SHORT_WAIT_SECONDS = 0.05
_HYDE_TIMEOUT_SECONDS = 7.5
_SLOW_SEARCH_SECONDS = 10
_TOP_K_MAX = 3
_NAME_LONGER_THAN_THE_LIMIT = "a" * 65
_HYDE_VECTOR = [0.1, 0.2]
_EXCEPTION_TEXT = "lance error: vector dimension 384 != 768 at /var/data/store"
_SERVICE_LOGGER = "archon_search.services.search_service"


def _embedder(*, warm: bool = True) -> MagicMock:
    embedder = MagicMock()
    embedder.is_warm = warm
    return embedder


def _hit(**overrides: Any) -> SearchResult:
    fields: dict[str, Any] = {
        "doc_id": "d1",
        "chunk_id": "c1",
        "text": "text",
        "score": 0.5,
        "source_path": "a.md",
        "metadata": {"k": "v"},
        "collection": _COLLECTION,
    }
    fields.update(overrides)
    return SearchResult(**fields)


def _pipeline(
    *,
    meta: CollectionMeta | None = None,
    result: SearchPipelineResult | None = None,
    rerank: bool = True,
) -> MagicMock:
    pipeline = MagicMock()
    pipeline.global_embedder = _embedder()
    pipeline.get_collection_meta = AsyncMock(
        return_value=meta
        if meta is not None
        else CollectionMeta(name=_COLLECTION, namespace=_NAMESPACE, active_embedding_model=_COLLECTION_MODEL)
    )
    pipeline.warmup_models = AsyncMock(return_value=rerank)
    pipeline.search = AsyncMock(
        return_value=result if result is not None else SearchPipelineResult(results=[_hit()], acl_filtered=False)
    )
    return pipeline


def _cache(embedder: MagicMock | None = None) -> MagicMock:
    cache = MagicMock()
    cache.get_or_load = AsyncMock(return_value=embedder if embedder is not None else _embedder())
    return cache


def _config(**overrides: Any) -> SearchConfig:
    config = SearchConfig()
    config.embedding_model = _GLOBAL_MODEL
    for name, value in overrides.items():
        setattr(config, name, value)
    return config


def _service(
    pipeline: MagicMock | None = None,
    *,
    cache: MagicMock | None = None,
    config: SearchConfig | None = None,
    hyde_generator: Any = None,
    rag_fusion_generator: Any = None,
) -> SearchService:
    return SearchService(
        pipeline if pipeline is not None else _pipeline(),
        config if config is not None else _config(),
        embedder_cache=cache,
        hyde_generator=hyde_generator,
        rag_fusion_generator=rag_fusion_generator,
    )


def _command(**overrides: Any) -> SearchCommand:
    fields: dict[str, Any] = {"query": "q", "collection": _COLLECTION}
    fields.update(overrides)
    return SearchCommand(**fields)


async def _failure(service: SearchService, command: SearchCommand) -> ServiceError:
    with pytest.raises(ServiceError) as caught:
        await service.search(command, _CALLER)
    return caught.value


# --- embedder selection -------------------------------------------------------------------------------


async def test_single_path_uses_the_collection_embedder_and_reports_its_model() -> None:
    pipeline = _pipeline()
    collection_embedder = _embedder()
    cache = _cache(collection_embedder)

    outcome = await _service(pipeline, cache=cache).search(_command(), _CALLER)

    cache.get_or_load.assert_awaited_once_with(_COLLECTION_MODEL)
    assert pipeline.search.await_args.kwargs["embedder"] is collection_embedder
    assert outcome.embedding_model == _COLLECTION_MODEL


async def test_collection_without_a_pinned_model_uses_the_global_model_name_through_the_cache() -> None:
    meta = CollectionMeta(name=_COLLECTION, namespace=_NAMESPACE)
    cache = _cache()

    outcome = await _service(_pipeline(meta=meta), cache=cache).search(_command(), _CALLER)

    cache.get_or_load.assert_awaited_once_with(_GLOBAL_MODEL)
    assert outcome.embedding_model == _GLOBAL_MODEL


async def test_absent_embedder_cache_falls_back_to_the_global_embedder_and_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pipeline = _pipeline()

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        outcome = await _service(pipeline, cache=None).search(_command(), _CALLER)

    assert pipeline.search.await_args.kwargs["embedder"] is pipeline.global_embedder
    assert outcome.embedding_model == _GLOBAL_MODEL
    assert [r.levelno for r in caplog.records if r.name == _SERVICE_LOGGER] == [logging.WARNING]


class _ControlledLoad:
    """A cache load the test drives: it records its task, waits for :meth:`release`, then returns or raises *failure*."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.started = asyncio.Event()
        self._released = asyncio.Event()
        self._failure = failure
        self.task: asyncio.Task[Any] | None = None

    async def __call__(self, _model: str) -> MagicMock:
        self.task = asyncio.current_task()
        self.started.set()
        await self._released.wait()
        if self._failure is not None:
            raise self._failure
        return _embedder()

    def release(self) -> None:
        self._released.set()

    def as_cache(self) -> MagicMock:
        cache = MagicMock()
        cache.get_or_load = self
        return cache


async def test_cold_embedder_degrades_to_fts_only_after_the_bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_service_module, "_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS", _SHORT_WAIT_SECONDS)
    pipeline = _pipeline()
    load = _ControlledLoad()

    outcome = await _service(pipeline, cache=load.as_cache()).search(_command(), _CALLER)

    kwargs = pipeline.search.await_args.kwargs
    assert kwargs["fts_only"] is True
    assert kwargs["embedder"] is pipeline.global_embedder
    assert outcome.embedding_model == _COLLECTION_MODEL
    assert load.task is not None
    assert not load.task.done(), "the bounded wait must not cancel the load"
    load.release()
    await load.task


async def test_abandoned_load_failure_is_retrieved_and_registration_untouched(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(search_service_module, "_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS", _SHORT_WAIT_SECONDS)
    load = _ControlledLoad(failure=RuntimeError("load blew up"))
    cache = load.as_cache()

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        await _service(cache=cache).search(_command(), _CALLER)
        load.release()
        assert load.task is not None
        with pytest.raises(RuntimeError):
            await load.task  # the service's done-callback was registered first, so it has run when this wakes

    assert any("abandoned embedder load failed" in r.getMessage() for r in caplog.records)
    assert cache.method_calls == [], "the service must never touch the cache's registration"


async def test_embedder_load_that_raises_timeout_itself_is_not_mistaken_for_the_bound() -> None:
    cache = MagicMock()
    cache.get_or_load = AsyncMock(side_effect=TimeoutError("the load itself"))

    failure = await _failure(_service(cache=cache), _command())

    assert failure.kind is ServiceErrorKind.INTERNAL


async def test_embedder_not_ready_is_its_own_kind_with_the_retry_hint() -> None:
    cache = MagicMock()
    cache.get_or_load = AsyncMock(side_effect=EmbedderNotReadyError("wedged"))

    failure = await _failure(_service(cache=cache), _command())

    assert failure.kind is ServiceErrorKind.EMBEDDER_NOT_READY
    assert failure.retry_after_seconds == RETRY_AFTER_SECONDS


async def test_embedder_not_ready_from_the_pipeline_keeps_its_retryable_kind() -> None:
    pipeline = _pipeline()
    pipeline.search = AsyncMock(side_effect=EmbedderNotReadyError("wedged"))

    failure = await _failure(_service(pipeline), _command())

    assert failure.kind is ServiceErrorKind.EMBEDDER_NOT_READY


async def test_cancelled_embedder_wait_leaves_the_load_running_with_its_outcome_consumed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    load = _ControlledLoad(failure=RuntimeError("late failure"))
    search = asyncio.ensure_future(_service(cache=load.as_cache()).search(_command(), _CALLER))
    await load.started.wait()

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        search.cancel()
        with pytest.raises(asyncio.CancelledError):
            await search
        load.release()
        assert load.task is not None
        with pytest.raises(RuntimeError):
            await load.task

    assert any("abandoned embedder load failed" in r.getMessage() for r in caplog.records)


# --- collection lookup --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["bad name!", "../etc", _NAME_LONGER_THAN_THE_LIMIT])
async def test_invalid_collection_name_is_not_found_without_a_store_call(name: str) -> None:
    pipeline = _pipeline()

    failure = await _failure(_service(pipeline), _command(collection=name))

    assert failure.kind is ServiceErrorKind.NOT_FOUND
    assert failure.detail == COLLECTION_NOT_FOUND_DETAIL
    pipeline.get_collection_meta.assert_not_awaited()


async def test_unknown_collection_or_one_of_another_namespace_is_not_found() -> None:
    pipeline = _pipeline()
    pipeline.get_collection_meta = AsyncMock(return_value=None)

    failure = await _failure(_service(pipeline), _command())

    assert failure.kind is ServiceErrorKind.NOT_FOUND
    pipeline.get_collection_meta.assert_awaited_once_with(_COLLECTION, namespace=_NAMESPACE)
    pipeline.search.assert_not_awaited()


async def test_metadata_store_failure_is_its_own_kind_and_leaks_no_exception_text() -> None:
    pipeline = _pipeline()
    pipeline.get_collection_meta = AsyncMock(side_effect=RuntimeError(_EXCEPTION_TEXT))

    failure = await _failure(_service(pipeline), _command())

    assert failure.kind is ServiceErrorKind.METADATA_STORE_ERROR
    assert _EXCEPTION_TEXT not in failure.detail
    assert isinstance(failure.__cause__, RuntimeError)


async def test_cancellation_is_not_swallowed_and_namespace_is_passed_by_keyword() -> None:
    pipeline = _pipeline()
    pipeline.search = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await _service(pipeline).search(_command(), _CALLER)

    assert pipeline.get_collection_meta.await_args.kwargs == {"namespace": _NAMESPACE}
    assert pipeline.search.await_args.kwargs["namespace"] == _NAMESPACE


async def test_cancelled_metadata_lookup_is_not_swallowed() -> None:
    pipeline = _pipeline()
    pipeline.get_collection_meta = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await _service(pipeline).search(_command(), _CALLER)


# --- normalisation ------------------------------------------------------------------------------------


async def test_command_is_normalised_before_any_store_call() -> None:
    pipeline = _pipeline()

    failure = await _failure(_service(pipeline), _command(query="   "))

    assert failure.kind is ServiceErrorKind.VALIDATION
    pipeline.get_collection_meta.assert_not_awaited()


async def test_the_operators_limits_apply() -> None:
    config = _config(top_k_max=_TOP_K_MAX)

    failure = await _failure(_service(config=config), _command(top_k=_TOP_K_MAX + 1))

    assert failure.kind is ServiceErrorKind.VALIDATION


async def test_padded_names_are_stripped_before_the_lookup() -> None:
    pipeline = _pipeline()

    await _service(pipeline).search(_command(query="  q  ", collection=f"  {_COLLECTION}  "), _CALLER)

    assert pipeline.get_collection_meta.await_args.args == (_COLLECTION,)
    assert pipeline.search.await_args.args == ("q", _COLLECTION)


async def test_multi_path_command_is_refused_until_the_multi_path_lands() -> None:
    with pytest.raises(NotImplementedError):
        await _service().search(_command(collection=None, collections=("a", "b")), _CALLER)


# --- HyDE and RAG Fusion ------------------------------------------------------------------------------


async def test_rag_fusion_wins_over_hyde_and_graph_mode_suppresses_hyde() -> None:
    generator = stub_hyde_generator(return_value=_HYDE_VECTOR)
    config = _config()
    config.hyde.enabled = True
    config.rag_fusion.enabled = True
    config.graph.enabled = True
    service = _service(config=config, hyde_generator=generator, rag_fusion_generator=MagicMock())

    rag_outcome = await service.search(_command(hyde=True, rag_fusion=True), _CALLER)
    graph_outcome = await service.search(_command(hyde=True, graph_mode="local"), _CALLER)

    generator.generate.assert_not_called()
    assert (rag_outcome.hyde_applied, graph_outcome.hyde_applied) == (False, False)
    assert rag_outcome.expansion_warning is None
    assert graph_outcome.expansion_warning is None


async def test_hyde_vector_reaches_the_pipeline_and_is_reported_applied() -> None:
    generator = stub_hyde_generator(return_value=_HYDE_VECTOR)
    config = _config()
    config.hyde.enabled = True
    pipeline = _pipeline()

    outcome = await _service(pipeline, config=config, hyde_generator=generator).search(
        _command(hyde=True), _CALLER
    )

    assert pipeline.search.await_args.kwargs["query_vector"] == _HYDE_VECTOR
    assert outcome.hyde_applied is True
    assert outcome.expansion_used is True
    assert outcome.expansion_warning is None


async def test_hyde_is_embedded_with_the_collection_embedder() -> None:
    generator = stub_hyde_generator(return_value=_HYDE_VECTOR)
    config = _config()
    config.hyde.enabled = True
    collection_embedder = _embedder()

    await _service(config=config, cache=_cache(collection_embedder), hyde_generator=generator).search(
        _command(hyde=True), _CALLER
    )

    assert generator.generate.await_args.kwargs["embedder"] is collection_embedder


async def test_cold_embedder_or_missing_generator_falls_back_to_the_plain_query_with_the_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    config.hyde.enabled = True
    no_generator = await _service(config=config, hyde_generator=None).search(_command(hyde=True), _CALLER)

    monkeypatch.setattr(search_service_module, "_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS", _SHORT_WAIT_SECONDS)
    load = _ControlledLoad()
    cold_cache = load.as_cache()
    generator = stub_hyde_generator(return_value=_HYDE_VECTOR)
    cold = await _service(cache=cold_cache, config=config, hyde_generator=generator).search(
        _command(hyde=True), _CALLER
    )

    for outcome in (no_generator, cold):
        assert outcome.hyde_applied is False
        assert outcome.expansion_warning == HYDE_EXPANSION_FAILED_WARNING
    generator.generate.assert_not_called()
    load.release()
    assert load.task is not None
    await load.task


async def test_hyde_failure_is_its_own_kind_and_leaks_no_exception_text() -> None:
    generator = stub_hyde_generator(side_effect=RuntimeError(_EXCEPTION_TEXT))
    config = _config()
    config.hyde.enabled = True

    failure = await _failure(_service(config=config, hyde_generator=generator), _command(hyde=True))

    assert failure.kind is ServiceErrorKind.HYDE_FAILED
    assert failure.detail == SEARCH_FAILED_DETAIL
    assert isinstance(failure.__cause__, RuntimeError)


async def test_hyde_embedder_not_ready_keeps_its_retryable_kind() -> None:
    generator = stub_hyde_generator(side_effect=EmbedderNotReadyError("wedged"))
    config = _config()
    config.hyde.enabled = True

    failure = await _failure(_service(config=config, hyde_generator=generator), _command(hyde=True))

    assert failure.kind is ServiceErrorKind.EMBEDDER_NOT_READY


async def test_hyde_is_called_with_the_timeout_read_from_the_service_module_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_service_module, "_SEARCH_TIMEOUT_SECONDS", _HYDE_TIMEOUT_SECONDS)
    real_for_request = search_service_module.RequestHyDE.for_request

    with patch.object(
        search_service_module.RequestHyDE, "for_request", side_effect=real_for_request
    ) as spy:
        await _service().search(_command(), _CALLER)

    assert spy.call_args.kwargs["timeout"] == _HYDE_TIMEOUT_SECONDS


async def test_rag_fusion_inputs_reach_the_pipeline_and_its_outcome_is_reported() -> None:
    config = _config()
    config.rag_fusion.enabled = True
    generator = MagicMock()
    result = SearchPipelineResult(
        results=[_hit()],
        acl_filtered=False,
        rag_fusion_applied=True,
        rag_fusion_queries_used=3,
        rag_fusion_attempted=True,
    )
    pipeline = _pipeline(result=result)

    outcome = await _service(pipeline, config=config, rag_fusion_generator=generator).search(
        _command(rag_fusion=True), _CALLER
    )

    kwargs = pipeline.search.await_args.kwargs
    assert kwargs["rag_fusion"] is True
    assert kwargs["rag_fusion_generator"] is generator
    assert kwargs["rag_fusion_config"] is config.rag_fusion
    assert (outcome.rag_fusion_applied, outcome.rag_fusion_queries_used, outcome.rag_fusion_attempted) == (
        True,
        3,
        True,
    )
    assert outcome.expansion_used is True


async def test_expansion_warning_prefers_hyde_then_rag_fusion() -> None:
    result = SearchPipelineResult(results=[], acl_filtered=False, rag_fusion_warning="rag fusion warning")
    pipeline = _pipeline(result=result)

    rag_only = await _service(pipeline).search(_command(), _CALLER)
    both = await _service(pipeline).search(_command(hyde=True), _CALLER)  # no generator: the HyDE warning wins

    assert rag_only.expansion_warning == "rag fusion warning"
    assert both.expansion_warning == HYDE_EXPANSION_FAILED_WARNING


# --- warm-up ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rerank", "fts_only"),
    [(True, True), (False, True), (True, False), (False, False)],
)
async def test_fts_only_is_passed_to_the_pipeline_unchanged_and_never_re_derived(
    rerank: bool, fts_only: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warm-up's verdict is forwarded as it is, even when it disagrees with what the embedder's own state implies."""
    warmup = AsyncMock(return_value=SearchWarmup(rerank=rerank, fts_only=fts_only))
    monkeypatch.setattr(search_service_module, "warmup_for_search", warmup)
    pipeline = _pipeline()
    embedder = _embedder(warm=fts_only)  # deliberately contradicts ``fts_only``

    await _service(pipeline, cache=_cache(embedder)).search(_command(), _CALLER)

    kwargs = pipeline.search.await_args.kwargs
    assert kwargs["rerank"] is rerank
    assert kwargs["fts_only"] is fts_only
    warmup.assert_awaited_once_with(pipeline, embedder)


async def test_rag_fusion_request_does_not_suppress_hyde_when_the_operator_disabled_rag_fusion() -> None:
    generator = stub_hyde_generator(return_value=_HYDE_VECTOR)
    config = _config()
    config.hyde.enabled = True
    config.rag_fusion.enabled = False

    outcome = await _service(config=config, hyde_generator=generator).search(
        _command(hyde=True, rag_fusion=True), _CALLER
    )

    assert outcome.hyde_applied is True


# --- budget and failure kinds -------------------------------------------------------------------------


async def test_budget_overrun_is_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_service_module, "_SEARCH_TIMEOUT_SECONDS", _SHORT_WAIT_SECONDS)
    pipeline = _pipeline()

    async def slow_search(*_args: object, **_kwargs: object) -> SearchPipelineResult:
        await asyncio.sleep(_SLOW_SEARCH_SECONDS)
        raise AssertionError("unreachable")

    pipeline.search = slow_search

    failure = await _failure(_service(pipeline), _command())

    assert failure.kind is ServiceErrorKind.TIMEOUT


async def test_a_timeout_error_raised_by_the_pipeline_is_internal_not_a_budget_overrun() -> None:
    pipeline = _pipeline()
    pipeline.search = AsyncMock(side_effect=TimeoutError("socket ETIMEDOUT"))

    failure = await _failure(_service(pipeline), _command())

    assert failure.kind is ServiceErrorKind.INTERNAL


async def test_unexpected_failure_is_internal_with_a_fixed_detail_and_the_cause_attached() -> None:
    pipeline = _pipeline()
    pipeline.search = AsyncMock(side_effect=RuntimeError(_EXCEPTION_TEXT))

    failure = await _failure(_service(pipeline), _command())

    assert failure.kind is ServiceErrorKind.INTERNAL
    assert failure.detail == SEARCH_FAILED_DETAIL
    assert isinstance(failure.__cause__, RuntimeError)


async def test_graph_communities_not_built_text_is_built_from_the_typed_collection() -> None:
    config = _config()
    config.graph.enabled = True
    pipeline = _pipeline()
    pipeline.search = AsyncMock(side_effect=GraphCommunitiesNotBuiltError(_COLLECTION))

    failure = await _failure(_service(pipeline, config=config), _command(graph_mode="global"))

    assert failure.kind is ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT
    assert failure.detail == f"No community representatives found for collection '{_COLLECTION}'. Run community detection first."


# --- outcome ------------------------------------------------------------------------------------------


async def test_outcome_carries_the_raw_hits_and_the_pipeline_flags() -> None:
    hit = _hit()
    result = SearchPipelineResult(
        results=[hit],
        acl_filtered=True,
        excluded_collections=[ExcludedCollection(name="x", reason="r")],
        graph_expansion_applied=True,
        ppr_entities_matched=4,
    )

    outcome = await _service(_pipeline(result=result)).search(_command(), _CALLER)

    assert isinstance(outcome, SearchOutcome)
    assert [h.result for h in outcome.results] == [hit]
    assert outcome.results[0].result is hit
    assert outcome.acl_filtered is True
    assert outcome.excluded_collections == (ExcludedCollection(name="x", reason="r"),)
    assert outcome.graph_expansion_applied is True
    assert outcome.expansion_used is True
    assert outcome.ppr_entities_matched == 4


@pytest.mark.parametrize(
    ("top_level", "nested", "expected"),
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
async def test_outcome_exposes_the_ored_include_metadata_flag(top_level: bool, nested: bool, expected: bool) -> None:
    filters = SearchFilters(include_metadata=nested)

    outcome = await _service().search(_command(include_metadata=top_level, filters=filters), _CALLER)

    assert outcome.include_metadata is expected


async def test_applied_filters_is_none_without_a_filter_and_the_parsed_filters_otherwise() -> None:
    filters = SearchFilters(file_type="md", include_metadata=True)

    without = await _service().search(_command(include_metadata=True), _CALLER)
    with_filters = await _service().search(_command(filters=filters), _CALLER)

    assert without.applied_filters is None
    assert with_filters.applied_filters is filters


async def test_filters_and_scope_filter_reach_the_pipeline() -> None:
    filters = SearchFilters(language="en")
    pipeline = _pipeline()

    await _service(pipeline).search(_command(filters=filters, scope_filter="docs"), _CALLER)

    kwargs = pipeline.search.await_args.kwargs
    assert kwargs["filters"] is filters
    assert kwargs["scope_filter"] == "docs"
    assert kwargs["graph_mode"] is None


# --- acl gate -----------------------------------------------------------------------------------------


async def test_acl_gate_is_built_per_hit_only_when_requested() -> None:
    hits = [
        _hit(doc_id="a", acl=["alice"], acl_source="sidecar", acl_sidecar_path="a.acl", acl_warning=["w1"]),
        _hit(doc_id="b", acl=None, acl_source="made-up"),
    ]
    pipeline = _pipeline(result=SearchPipelineResult(results=hits, acl_filtered=False))

    requested = await _service(pipeline).search(_command(acl_context=True), _CALLER)
    not_requested = await _service(pipeline).search(_command(), _CALLER)

    assert [h.acl_gate for h in requested.results] == [
        AclGate(allowed_principals=["alice"], source="sidecar", sidecar_path="a.acl", warnings=["w1"]),
        AclGate(allowed_principals=None, source=None, sidecar_path=None, warnings=[]),
    ]
    assert [h.acl_gate for h in not_requested.results] == [None, None]


async def test_acl_gate_lists_are_copies_of_the_hits() -> None:
    hit = _hit(acl=["alice"], acl_warning=["w"])
    pipeline = _pipeline(result=SearchPipelineResult(results=[hit], acl_filtered=False))

    outcome = await _service(pipeline).search(_command(acl_context=True), _CALLER)

    gate = outcome.results[0].acl_gate
    assert gate is not None
    assert gate.warnings == hit.acl_warning
    assert gate.warnings is not hit.acl_warning
    assert gate.allowed_principals == hit.acl
    assert gate.allowed_principals is not hit.acl

