"""``SearchService.search`` single path: telemetry entries, stage timings and the failure log events.

The service records each outcome once for every interface through the existing ``TelemetryEntry`` factories and logs
each failure where the exception is in hand (a ``ServiceError`` carries none), never with the query text.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.embedder_cache import EmbedderNotReadyError
from archon_search.filters import SearchFilters
from archon_search.observability import correlation_id
from archon_search.pipeline import GraphCommunitiesNotBuiltError, SearchPipelineResult
from archon_search.services.search_budget import SearchBudgetExceeded
from archon_search.services.search_command import SearchCommand
from archon_search.services.search_service import SearchService
from archon_search.services.service_error import ServiceErrorKind
from archon_search.telemetry.entry import TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter
from tests._hyde_stubs import stub_hyde_generator
from tests.services.test_search_service import (
    _CALLER,
    _COLLECTION,
    _EXCEPTION_TEXT,
    _SERVICE_LOGGER,
    _cache,
    _command,
    _config,
    _failure,
    _hit,
    _pipeline,
)

_QUERY_TEXT = "secret-query-text-0451"
_CORRELATION_ID = "corr-1"
_RAG_FUSION_QUERIES = 3
_HASH_PREFIX = "hashed-"


def _hasher(doc_id: str) -> str:
    return f"{_HASH_PREFIX}{doc_id}"


def _writer() -> MagicMock:
    return MagicMock()


def _service(
    pipeline: MagicMock | None = None,
    *,
    writer: Any = None,
    hasher: Any = None,
    cache: MagicMock | None = None,
    timings: bool = False,
    hyde_generator: Any = None,
    graph: bool = False,
) -> SearchService:
    config = _config()
    config.graph.enabled = graph
    config.observability.stage_timings_enabled = timings
    config.hyde.enabled = hyde_generator is not None
    return SearchService(
        pipeline if pipeline is not None else _pipeline(),
        config,
        embedder_cache=cache,
        hyde_generator=hyde_generator,
        telemetry_writer=writer,
        doc_id_hasher=hasher,
    )


def _entries(writer: MagicMock) -> list[TelemetryEntry]:
    return [call.args[0] for call in writer.enqueue.call_args_list]


def _records(caplog: pytest.LogCaptureFixture, event_type: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event_type", None) == event_type]


def _fail_pipeline(exc: BaseException) -> MagicMock:
    pipeline = _pipeline()
    pipeline.search = AsyncMock(side_effect=exc)
    return pipeline


# --- entries ------------------------------------------------------------------------------------------


async def test_success_records_a_search_entry_with_hashed_doc_ids_and_filter_flags() -> None:
    writer = _writer()
    pipeline = _pipeline()
    command = _command(include_metadata=True, filters=SearchFilters(file_type="md"))

    await _service(pipeline, writer=writer, hasher=_hasher).search(command, _CALLER)

    (entry,) = _entries(writer)
    assert entry.endpoint == "search"
    assert entry.status == "ok"
    assert entry.collection == _COLLECTION
    assert entry.result_doc_ids == [f"{_HASH_PREFIX}{_hit().doc_id}"]
    assert entry.doc_ids_hashed is True
    assert entry.result_count == 1
    assert entry.filter_flags.include_metadata is True
    assert entry.filter_flags.file_type is True


async def test_success_without_a_hasher_stores_plain_doc_ids_and_the_rag_fusion_figures() -> None:
    writer = _writer()

    await _service(writer=writer).search(_command(), _CALLER)

    (entry,) = _entries(writer)
    assert entry.result_doc_ids == [_hit().doc_id]
    assert entry.doc_ids_hashed is False
    assert entry.rag_fusion_applied is False
    assert entry.filter_flags.include_metadata is False


@pytest.mark.parametrize(
    ("top_level", "nested", "expected"),
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
async def test_include_metadata_flag_is_the_or_of_the_top_level_and_nested_flags(
    top_level: bool, nested: bool, expected: bool
) -> None:
    writer = _writer()
    command = _command(include_metadata=top_level, filters=SearchFilters(include_metadata=nested))

    await _service(writer=writer).search(command, _CALLER)

    assert _entries(writer)[0].filter_flags.include_metadata is expected


async def test_no_writer_records_nothing_and_still_answers() -> None:
    outcome = await _service(writer=None).search(_command(), _CALLER)

    assert len(outcome.results) == 1


def _embedder_not_ready_service(writer: MagicMock) -> SearchService:
    cache = _cache()
    cache.get_or_load = AsyncMock(side_effect=EmbedderNotReadyError("still loading"))
    return _service(writer=writer, cache=cache)


def _metadata_failure_service(writer: MagicMock) -> SearchService:
    pipeline = _pipeline()
    pipeline.get_collection_meta = AsyncMock(side_effect=RuntimeError(_EXCEPTION_TEXT))
    return _service(pipeline, writer=writer)


def _hyde_failure_service(writer: MagicMock) -> SearchService:
    generator = stub_hyde_generator(side_effect=RuntimeError(_EXCEPTION_TEXT))
    return _service(writer=writer, hyde_generator=generator)


@pytest.mark.parametrize(
    ("build", "kind", "status", "error_kind"),
    [
        (lambda w: _service(_fail_pipeline(SearchBudgetExceeded()), writer=w), ServiceErrorKind.TIMEOUT, "timeout", "timeout"),
        (lambda w: _service(_fail_pipeline(RuntimeError(_EXCEPTION_TEXT)), writer=w), ServiceErrorKind.INTERNAL, "internal_error", "other"),
        (_embedder_not_ready_service, ServiceErrorKind.EMBEDDER_NOT_READY, "internal_error", "other"),
        (_metadata_failure_service, ServiceErrorKind.METADATA_STORE_ERROR, "internal_error", "other"),
        (_hyde_failure_service, ServiceErrorKind.HYDE_FAILED, "internal_error", "other"),
    ],
    ids=["timeout", "internal", "embedder_not_ready", "metadata_store", "hyde_failed"],
)
async def test_recorded_error_kinds_map_to_timeout_or_internal_error_other(
    build: Any, kind: ServiceErrorKind, status: str, error_kind: str
) -> None:
    writer = _writer()
    service = build(writer)

    error = await _failure(service, _command(hyde=kind is ServiceErrorKind.HYDE_FAILED))

    assert error.kind is kind
    (entry,) = _entries(writer)
    assert entry.endpoint == "search"
    assert entry.status == status
    assert entry.error_kind == error_kind


def _unrecorded(name: str, writer: MagicMock | None = None) -> tuple[SearchService, SearchCommand, ServiceErrorKind]:
    """A service, a command and the kind a validation-class failure or a ``not_found`` ends in (cache wired: no absence warning)."""
    pipeline = _pipeline()
    command = _command()
    kind = ServiceErrorKind.VALIDATION
    graph = False
    if name == "not_found":
        pipeline.get_collection_meta = AsyncMock(return_value=None)
        kind = ServiceErrorKind.NOT_FOUND
    elif name == "invalid_name":
        command = _command(collection="bad name!")
        kind = ServiceErrorKind.NOT_FOUND
    elif name == "graph_disabled":
        command = _command(graph_mode="local")
        kind = ServiceErrorKind.GRAPH_DISABLED
    elif name == "scope_filter_graph_mode":
        command, graph = _command(graph_mode="local", scope_filter="docs/"), True
        kind = ServiceErrorKind.SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE
    elif name == "invalid_scope_filter":
        command = _command(scope_filter="*")
        kind = ServiceErrorKind.INVALID_SCOPE_FILTER
    elif name == "communities":
        pipeline = _fail_pipeline(GraphCommunitiesNotBuiltError(_COLLECTION))
        kind = ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT
    else:
        command = _command(query="   ")
    return _service(pipeline, writer=writer, cache=_cache(), graph=graph), command, kind


_UNRECORDED_NAMES = [
    "not_found",
    "invalid_name",
    "validation",
    "graph_disabled",
    "scope_filter_graph_mode",
    "invalid_scope_filter",
    "communities",
]


@pytest.mark.parametrize("name", _UNRECORDED_NAMES)
async def test_validation_class_kinds_and_not_found_record_nothing(name: str) -> None:
    writer = _writer()
    service, command, kind = _unrecorded(name, writer)

    error = await _failure(service, command)

    assert error.kind is kind
    writer.enqueue.assert_not_called()


@pytest.mark.parametrize("name", _UNRECORDED_NAMES)
async def test_validation_class_kinds_and_not_found_log_nothing(
    caplog: pytest.LogCaptureFixture, name: str
) -> None:
    service, command, kind = _unrecorded(name)

    with caplog.at_level(logging.DEBUG, logger=_SERVICE_LOGGER):
        error = await _failure(service, command)

    assert error.kind is kind
    assert caplog.records == []


async def test_a_cancelled_search_propagates_unrecorded_and_unlogged(caplog: pytest.LogCaptureFixture) -> None:
    writer = _writer()
    service = _service(_fail_pipeline(asyncio.CancelledError()), writer=writer, cache=_cache(), timings=True)

    with caplog.at_level(logging.DEBUG, logger=_SERVICE_LOGGER), pytest.raises(asyncio.CancelledError):
        await service.search(_command(), _CALLER)

    writer.enqueue.assert_not_called()
    assert caplog.records == []


async def test_entries_carry_the_correlation_id_and_a_latency() -> None:
    writer = _writer()
    token = correlation_id.set(_CORRELATION_ID)
    try:
        await _service(writer=writer).search(_command(), _CALLER)
        await _failure(_service(_fail_pipeline(SearchBudgetExceeded()), writer=writer), _command())
    finally:
        correlation_id.reset(token)

    entries = _entries(writer)
    assert [entry.correlation_id for entry in entries] == [_CORRELATION_ID, _CORRELATION_ID]
    assert all(entry.latency_ms >= 0 for entry in entries)


async def test_rag_fusion_figures_reach_the_success_entry() -> None:
    writer = _writer()
    result = SearchPipelineResult(
        results=[_hit()], acl_filtered=False, rag_fusion_applied=True, rag_fusion_queries_used=_RAG_FUSION_QUERIES
    )

    await _service(_pipeline(result=result), writer=writer).search(_command(), _CALLER)

    (entry,) = _entries(writer)
    assert entry.rag_fusion_applied is True
    assert entry.rag_fusion_queries_used == _RAG_FUSION_QUERIES


# --- stage timings ------------------------------------------------------------------------------------


def _timed_branch(name: str) -> tuple[SearchService, ServiceErrorKind | None]:
    """A service (stage timings on) and the kind its search ends in (``None``: success)."""
    if name == "success":
        return _service(timings=True), None
    if name == "timeout":
        return _service(_fail_pipeline(SearchBudgetExceeded()), timings=True), ServiceErrorKind.TIMEOUT
    if name == "internal":
        return _service(_fail_pipeline(RuntimeError("boom")), timings=True), ServiceErrorKind.INTERNAL
    if name == "communities":
        pipeline = _fail_pipeline(GraphCommunitiesNotBuiltError(_COLLECTION))
        return _service(pipeline, timings=True), ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT
    if name == "embedder_not_ready":
        cache = _cache()
        cache.get_or_load = AsyncMock(side_effect=EmbedderNotReadyError("still loading"))
        return _service(cache=cache, timings=True), ServiceErrorKind.EMBEDDER_NOT_READY
    generator = stub_hyde_generator(side_effect=RuntimeError("boom"))
    return _service(hyde_generator=generator, timings=True), ServiceErrorKind.HYDE_FAILED


@pytest.mark.parametrize("name", ["success", "timeout", "internal", "communities", "embedder_not_ready", "hyde_failed"])
async def test_stage_timings_are_logged_on_success_and_on_error_branches(
    caplog: pytest.LogCaptureFixture, name: str
) -> None:
    service, expected_kind = _timed_branch(name)

    with caplog.at_level(logging.INFO, logger=_SERVICE_LOGGER):
        if expected_kind is None:
            await service.search(_command(), _CALLER)
        else:
            assert (await _failure(service, _command(hyde=True))).kind is expected_kind

    (record,) = _records(caplog, "stage_timings")
    assert record.endpoint == "search"  # type: ignore[attr-defined]
    assert record.collection == _COLLECTION  # type: ignore[attr-defined]
    assert "total" in record.stage_timings_ms  # type: ignore[attr-defined]


async def test_stage_timings_are_not_logged_when_the_flag_is_off(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=_SERVICE_LOGGER):
        await _service(timings=False).search(_command(), _CALLER)

    assert _records(caplog, "stage_timings") == []


async def test_stage_timings_are_not_logged_for_a_failed_collection_lookup(caplog: pytest.LogCaptureFixture) -> None:
    pipeline = _pipeline()
    pipeline.get_collection_meta = AsyncMock(return_value=None)

    with caplog.at_level(logging.INFO, logger=_SERVICE_LOGGER):
        await _failure(_service(pipeline, timings=True), _command())

    assert _records(caplog, "stage_timings") == []


# --- failure log events -------------------------------------------------------------------------------


@pytest.mark.parametrize("hyde_failure", [False, True], ids=["internal", "hyde_failed"])
async def test_internal_and_hyde_failures_log_search_pipeline_failure_with_the_exception_and_no_query(
    caplog: pytest.LogCaptureFixture, hyde_failure: bool
) -> None:
    boom = RuntimeError(_EXCEPTION_TEXT)
    service = (
        _service(hyde_generator=stub_hyde_generator(side_effect=boom))
        if hyde_failure
        else _service(_fail_pipeline(boom))
    )

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        await _failure(service, _command(query=_QUERY_TEXT, hyde=hyde_failure))

    (record,) = _records(caplog, "search_pipeline_failure")
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is boom
    assert _QUERY_TEXT not in caplog.text


async def test_timeout_logs_search_timeout_with_the_collection_in_extra(caplog: pytest.LogCaptureFixture) -> None:
    service = _service(_fail_pipeline(SearchBudgetExceeded()))

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        await _failure(service, _command(query=_QUERY_TEXT))

    (record,) = _records(caplog, "search_timeout")
    assert record.levelno == logging.ERROR
    assert record.collection == _COLLECTION  # type: ignore[attr-defined]
    assert _QUERY_TEXT not in caplog.text


async def test_a_metadata_failure_is_logged_with_its_traceback(caplog: pytest.LogCaptureFixture) -> None:
    boom = RuntimeError(_EXCEPTION_TEXT)
    pipeline = _pipeline()
    pipeline.get_collection_meta = AsyncMock(side_effect=boom)

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        await _failure(_service(pipeline), _command(query=_QUERY_TEXT))

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[1] is boom
    assert "Traceback" in caplog.text
    assert _QUERY_TEXT not in caplog.text


async def test_embedder_not_ready_is_logged_as_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    cache = _cache()
    cache.get_or_load = AsyncMock(side_effect=EmbedderNotReadyError("still loading"))

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        await _failure(_service(cache=cache), _command())

    warnings = [r for r in caplog.records if "embedder not ready" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


# --- a failing writer ---------------------------------------------------------------------------------


async def test_a_failing_enqueue_on_success_is_logged_by_exception_type_and_never_breaks_the_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = _writer()
    writer.enqueue.side_effect = RuntimeError(_EXCEPTION_TEXT)

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        outcome = await _service(writer=writer).search(_command(), _CALLER)

    assert len(outcome.results) == 1
    assert "telemetry enqueue failed: RuntimeError" in caplog.text
    assert _EXCEPTION_TEXT not in caplog.text


async def test_a_failing_enqueue_on_an_error_is_logged_by_exception_type_and_the_error_kind_still_surfaces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = _writer()
    writer.enqueue.side_effect = RuntimeError(_EXCEPTION_TEXT)

    with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
        error = await _failure(_service(_fail_pipeline(SearchBudgetExceeded()), writer=writer), _command())

    assert error.kind is ServiceErrorKind.TIMEOUT
    assert "telemetry enqueue failed: RuntimeError" in caplog.text
    assert _EXCEPTION_TEXT not in caplog.text


# --- privacy ------------------------------------------------------------------------------------------


async def test_telemetry_jsonl_never_contains_the_query(tmp_path: Path) -> None:
    log_dir = tmp_path / "telemetry"
    writer = TelemetryWriter(log_dir)
    await writer.start()
    pipeline = _pipeline(result=None)
    pipeline.search = AsyncMock(
        side_effect=[
            _pipeline(result=None).search.return_value,
            SearchBudgetExceeded(),
        ]
    )
    service = _service(pipeline, writer=writer, hasher=_hasher)

    await service.search(_command(query=_QUERY_TEXT), _CALLER)
    await _failure(service, _command(query=_QUERY_TEXT))
    await writer.drain_and_stop()

    lines = [line for path in log_dir.glob("*.jsonl") for line in path.read_text().splitlines()]
    assert len(lines) == 2
    assert all(_QUERY_TEXT not in line for line in lines)
    assert [json.loads(line)["endpoint"] for line in lines] == ["search", "search"]
