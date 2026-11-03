"""``SearchService``: the one implementation of a search that every interface shares (single and multi collection).

A search runs the canonical flow once: normalise and validate the command, then either check the collection name and look
the collection up in the caller's namespace and select its embedder through the cache (waiting at most
:data:`_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS`, then degrading to full-text-only) -- the single path -- or fan out over
``collections`` with the pipeline's global embedder (a model-mismatched collection is excluded with its reason, never an
error) -- the multi path.  Both then warm the models, resolve HyDE and RAG Fusion, run the pipeline under the search budget
and derive the response fields once.  Every failure is a :class:`ServiceError`; nothing transport-specific is accepted or
raised (``CancelledError`` is never swallowed, so a client abort still cancels the search and its provider call).  Each
outcome is recorded once through the existing ``TelemetryEntry`` factories (``search`` for a single success and for every
error, ``search_multi`` for a multi success), and each failure is logged here with its exception, because a
:class:`ServiceError` carries none.

The service returns raw pipeline hits plus the derived fields in a :class:`SearchOutcome`; each adapter renders its own
wire schema from it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NamedTuple

from archon_search._types import ExcludedCollection, SearchResult
from archon_search.config import resolve_active_model
from archon_search.constants import SEARCH_TIMEOUT_SECONDS as _SEARCH_TIMEOUT_SECONDS
from archon_search.constants import VALID_ACL_SOURCES
from archon_search.embedder_cache import EmbedderNotReadyError
from archon_search.filters import SearchFilters
from archon_search.observability import StageRecorder, bind_stage_recorder
from archon_search.observability import correlation_id as _correlation_id
from archon_search.pipeline import (
    CollectionNotFoundError,
    FanoutTimeoutError,
    GraphCommunitiesNotBuiltError,
    MetadataLookupError,
)
from archon_search.services.authz import AuthIdentity
from archon_search.services.request_hyde import RequestHyDE
from archon_search.services.search_budget import (
    EMBEDDER_WARMUP_WAIT_SECONDS,
    SearchBudgetExceeded,
    run_within_budget,
    warmup_for_search,
)
from archon_search.services.search_command import (
    SearchCommand,
    normalize_search_command,
)
from archon_search.services.service_error import ServiceError, ServiceErrorKind
from archon_search.store import is_valid_collection_name
from archon_search.telemetry.entry import FilterFlags, TelemetryEntry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from archon_search.collection_meta import CollectionMeta
    from archon_search.config import SearchConfig
    from archon_search.embedder import Embedder
    from archon_search.embedder_cache import EmbedderCache
    from archon_search.hyde import HyDEGenerator
    from archon_search.pipeline import SearchPipeline, SearchPipelineResult
    from archon_search.rag_fusion import RAGFusionGenerator
    from archon_search.services.request_hyde import HyDEOutcome
    from archon_search.telemetry.writer import TelemetryWriter

logger = logging.getLogger(__name__)

# Upper bound on how long a single search waits on ``embedder_cache.get_or_load()`` before degrading to the FTS leg alone.
#
# What can be slow here is NOT the model build: ``make_embedder()`` only constructs a lazy backend and the weights are built
# on the first ``encode()``, one stage later in ``warmup_for_search``.  This wait is the cache's *handoff*: queueing behind a
# saturated ``asyncio.to_thread`` executor, or blocking as a deduplicated waiter behind another caller's in-flight load,
# whose own bound (``_LOAD_WAIT_TIMEOUT_SECONDS`` in ``embedder_cache.py``, 120 s) is tuned for the waiter/loader handoff
# rather than one request's patience.
#
# Bound here by the same S290 ceiling the later ``warmup_for_search`` stage uses, because both answer the same question: how
# long a search may sit on an unavailable embedder before answering without a query vector (S299: waiting 30 s and then
# answering 503 is the very cold-connection parking the S290 policy exists to prevent).  The load itself is shielded, so it
# keeps running and populates the cache for later requests; this bound never cancels it or touches its registration.
_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS = EMBEDDER_WARMUP_WAIT_SECONDS

# The telemetry label and the stage-timings ``endpoint`` of every search (a multi-path success is labelled ``search_multi``).
_ENDPOINT: Final = "search"
_MILLISECONDS_PER_SECOND = 1000.0


@dataclass(frozen=True, slots=True)
class AclGate:
    """The ACL provenance envelope of one hit, built once here and only when ``acl_context`` was requested.

    ``allowed_principals`` is ``None`` when no ACL applies to the chunk; ``source`` is ``None`` when no provenance is stored
    or the stored value is not one of ``VALID_ACL_SOURCES``; ``warnings`` is always a list.
    """

    allowed_principals: list[str] | None
    source: str | None
    sidecar_path: str | None
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ranked chunk as the pipeline returned it, plus its :class:`AclGate` (``None`` unless ``acl_context``)."""

    result: SearchResult
    acl_gate: AclGate | None


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """What a search leaves the adapter with: the raw hits and every derived field, each computed once.

    ``embedding_model`` is the collection's active model on the single path (the configured model when the app has no
    embedder cache) and the configured global model on the multi path.  ``include_metadata`` is the two request flags
    ORed once: the hits carry their metadata untouched, and the adapter blanks it unless this is set.  ``applied_filters`` is
    ``None`` when no filter was given, else the filters as parsed.
    """

    results: tuple[SearchHit, ...]
    acl_filtered: bool
    excluded_collections: tuple[ExcludedCollection, ...]
    embedding_model: str
    hyde_applied: bool
    rag_fusion_applied: bool
    rag_fusion_queries_used: int
    rag_fusion_attempted: bool
    graph_expansion_applied: bool
    expansion_used: bool
    expansion_warning: str | None
    applied_filters: SearchFilters | None
    ppr_entities_matched: int | None
    include_metadata: bool


class SearchService:
    """Runs a search for a resolved caller.  One class and one code path; each adapter builds its own instance."""

    def __init__(
        self,
        pipeline: SearchPipeline,
        config: SearchConfig,
        *,
        embedder_cache: EmbedderCache | None = None,
        hyde_generator: HyDEGenerator | None = None,
        rag_fusion_generator: RAGFusionGenerator | None = None,
        telemetry_writer: TelemetryWriter | None = None,
        doc_id_hasher: Callable[[str], str] | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._config = config
        self._embedder_cache = embedder_cache
        self._hyde_generator = hyde_generator
        self._rag_fusion_generator = rag_fusion_generator
        self._telemetry_writer = telemetry_writer
        self._doc_id_hasher = doc_id_hasher

    async def search(self, command: SearchCommand, caller: AuthIdentity) -> SearchOutcome:
        """Search the collection (single path) or the ``collections`` (multi path) *command* names inside *caller*'s namespace.

        Raises :class:`ServiceError` for every failure; the exception that caused it is attached as ``__cause__`` and never
        copied into the error's detail.  The multi path runs with the pipeline's global embedder: its ``embedding_model`` is
        the configured one, and a collection pinned to another model is listed in ``excluded_collections`` (all of them
        excluded is an empty result, not an error).

        The outcome is recorded once: a success, a timeout and every internal failure become a telemetry entry (when a writer
        is wired), and each failure is logged here with its exception, never with the query.  Validation-class kinds and
        ``not_found`` are neither recorded nor logged as failures.
        """
        request_start = time.monotonic()
        normalized = normalize_search_command(
            command,
            max_fanout=self._config.max_fanout,
            top_k_max=self._config.top_k_max,
            graph_enabled=self._config.graph.enabled,
        )
        collection, collections = normalized.collection, normalized.collections
        if collection is not None:
            return await self._search_single(normalized, collection, caller.namespace, request_start)
        if collections is not None:
            return await self._search_multi(normalized, collections, caller.namespace, request_start)
        raise AssertionError("normalize_search_command guarantees one selection")

    async def _search_single(
        self, command: SearchCommand, collection: str, namespace: str, request_start: float
    ) -> SearchOutcome:
        try:
            meta = await self._lookup_collection(collection, namespace)
            outcome = await self._search_with_stage_timings(command, collection, namespace, meta)
        except ServiceError as error:
            self._report_failure(error, _single_target(collection), _elapsed_ms(request_start))
            raise
        self._record_success(command, collection, outcome, _elapsed_ms(request_start))
        return outcome

    async def _search_multi(
        self, command: SearchCommand, collections: tuple[str, ...], namespace: str, request_start: float
    ) -> SearchOutcome:
        try:
            outcome = await _map_failures(self._search_collections(command, collections, namespace))
        except ServiceError as error:
            self._report_failure(error, _multi_target(collections), _elapsed_ms(request_start))
            raise
        self._record_multi_success(collections, outcome, _elapsed_ms(request_start))
        return outcome

    async def _search_with_stage_timings(
        self, command: SearchCommand, collection: str, namespace: str, meta: CollectionMeta
    ) -> SearchOutcome:
        """Run the search stages, logging their timings (flag permitting) on success and on every ``Exception`` branch."""
        with ExitStack() as stack:
            recorder = (
                stack.enter_context(bind_stage_recorder()) if self._config.observability.stage_timings_enabled else None
            )
            stages_start = time.perf_counter()
            try:
                outcome = await _map_failures(self._search_collection(command, collection, namespace, meta))
            except Exception:
                _log_stage_timings(recorder, stages_start, collection)
                raise
            _log_stage_timings(recorder, stages_start, collection)
            return outcome

    def _report_failure(self, error: ServiceError, target: _FailureTarget, latency_ms: float) -> None:
        """Log *error* where its cause is in hand and enqueue its error entry when the kind is recorded."""
        cause = error.__cause__
        kind = error.kind
        if kind in (ServiceErrorKind.INTERNAL, ServiceErrorKind.HYDE_FAILED):
            logger.error(
                "search pipeline failed: %s",
                type(cause).__name__,
                extra={"event_type": "search_pipeline_failure"},
                exc_info=cause,
            )
        elif kind is ServiceErrorKind.TIMEOUT:
            logger.error("search pipeline timed out", extra={"event_type": "search_timeout", **target.log_extra})
        elif kind is ServiceErrorKind.METADATA_STORE_ERROR:
            logger.error("%s", target.metadata_failure_message, exc_info=cause)
        elif kind is ServiceErrorKind.EMBEDDER_NOT_READY:
            logger.warning("search: embedder not ready — %s", cause)
        recorded = error.telemetry
        if recorded is None:
            return
        self._enqueue(
            lambda: TelemetryEntry.from_error(
                endpoint=_ENDPOINT,
                status=recorded.status,
                error_kind=recorded.error_kind,
                latency_ms=latency_ms,
                correlation_id=_correlation_id.get(),
            )
        )

    def _record_success(
        self, command: SearchCommand, collection: str, outcome: SearchOutcome, latency_ms: float
    ) -> None:
        def build() -> TelemetryEntry:
            flags = FilterFlags.from_search_filters(command.filters) if command.filters is not None else FilterFlags()
            if outcome.include_metadata and not flags.include_metadata:
                flags = flags.model_copy(update={"include_metadata": True})
            return TelemetryEntry.from_search_tool_result(
                endpoint=_ENDPOINT,
                collection=collection,
                result_doc_ids=[hit.result.doc_id for hit in outcome.results],
                latency_ms=latency_ms,
                filter_flags=flags,
                correlation_id=_correlation_id.get(),
                rag_fusion_applied=outcome.rag_fusion_applied,
                rag_fusion_queries_used=outcome.rag_fusion_queries_used,
                doc_id_hasher=self._doc_id_hasher,
            )

        self._enqueue(build)

    def _record_multi_success(self, collections: tuple[str, ...], outcome: SearchOutcome, latency_ms: float) -> None:
        """Enqueue the ``search_multi`` entry: the requested names and counts only (no doc ids, no filter flags)."""
        excluded_count = len(outcome.excluded_collections)
        self._enqueue(
            lambda: TelemetryEntry.from_search_multi_result(
                collections=list(collections),
                fanout_count=len(collections) - excluded_count,
                result_count=len(outcome.results),
                latency_ms=latency_ms,
                excluded_count=excluded_count,
                correlation_id=_correlation_id.get(),
                rag_fusion_applied=outcome.rag_fusion_applied,
                rag_fusion_queries_used=outcome.rag_fusion_queries_used,
            )
        )

    def _enqueue(self, build_entry: Callable[[], TelemetryEntry]) -> None:
        """Build and enqueue one entry; telemetry never breaks a request, a failure is logged by its exception type only."""
        writer = self._telemetry_writer
        if writer is None:
            return
        try:
            writer.enqueue(build_entry())
        except Exception as exc:  # noqa: BLE001 — telemetry must never break the request
            logger.warning("telemetry enqueue failed: %s", type(exc).__name__)

    async def _lookup_collection(self, collection: str, namespace: str) -> CollectionMeta:
        """The collection's metadata in *namespace*; an invalid name or an unknown collection is ``not_found``.

        The name is checked before any store call: the real store raises ``ValueError`` for an invalid name, which is not a
        metadata-store outage.
        """
        if not is_valid_collection_name(collection):
            raise ServiceError(ServiceErrorKind.NOT_FOUND)
        try:
            meta = await self._pipeline.get_collection_meta(collection, namespace=namespace)
        except Exception as exc:
            raise ServiceError(ServiceErrorKind.METADATA_STORE_ERROR) from exc
        if meta is None:
            raise ServiceError(ServiceErrorKind.NOT_FOUND)
        return meta

    async def _search_collection(
        self, command: SearchCommand, collection: str, namespace: str, meta: CollectionMeta
    ) -> SearchOutcome:
        pipeline = self._pipeline
        selected = await self._select_embedder(meta)
        embedder = selected.embedder
        # Outside the budget by design (S184): a cold model build must not consume the search budget.  Both legs are bounded
        # and degrade instead of parking the connection: the reranker to the fused ranking (S288), the embedder to the FTS
        # leg alone (S290).  A failed warm-up attempt is not latched: the next call retries it (S283).
        warmup = await warmup_for_search(pipeline, embedder)
        fts_only = warmup.fts_only or selected.cold
        hyde_outcome = await self._resolve_hyde(command, embedder, fts_only=fts_only)
        # ``run_within_budget``, not a bare ``asyncio.wait_for``: since 3.11 ``asyncio.TimeoutError`` is the builtin
        # ``TimeoutError`` (an ``OSError``), so a socket ETIMEDOUT raised inside the store would be mapped to a timeout.
        result = await run_within_budget(
            pipeline.search(
                command.query,
                collection,
                namespace=namespace,
                embedder=embedder,
                filters=command.filters,
                query_vector=hyde_outcome.vector,
                rag_fusion=command.rag_fusion,
                rag_fusion_generator=self._rag_fusion_generator,
                rag_fusion_config=self._config.rag_fusion,
                graph_mode=command.graph_mode,
                scope_filter=command.scope_filter,
                rerank=warmup.rerank,
                fts_only=fts_only,
            ),
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
        return _build_outcome(
            command,
            result,
            embedding_model=selected.model,
            hyde_applied=hyde_outcome.applied,
            hyde_warning=hyde_outcome.expansion_warning,
        )

    async def _search_collections(self, command: SearchCommand, collections: tuple[str, ...], namespace: str) -> SearchOutcome:
        pipeline = self._pipeline
        # The fan-out embeds with the pipeline's global embedder and excludes every collection pinned to another model, so
        # the warm-up and HyDE use it too.  ``fts_only`` is whatever the warm-up says: the service never re-derives it.
        warmup = await warmup_for_search(pipeline)
        hyde_outcome = await self._resolve_hyde(command, pipeline.global_embedder, fts_only=warmup.fts_only)
        # Same overall budget as the single path: ``search_many``'s own fan-out timeout bounds the per-collection legs only.
        result = await run_within_budget(
            pipeline.search_many(
                command.query,
                list(collections),
                namespace=namespace,
                query_vector=hyde_outcome.vector,
                rag_fusion=command.rag_fusion,
                rag_fusion_generator=self._rag_fusion_generator,
                rag_fusion_config=self._config.rag_fusion,
                filters=command.filters,
                graph_mode=command.graph_mode,
                scope_filter=command.scope_filter,
                rerank=warmup.rerank,
                fts_only=warmup.fts_only,
            ),
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
        return _build_outcome(
            command,
            result,
            embedding_model=self._config.embedding_model,
            hyde_applied=hyde_outcome.applied,
            hyde_warning=hyde_outcome.expansion_warning,
        )

    async def _resolve_hyde(self, command: SearchCommand, embedder: Embedder, *, fts_only: bool) -> HyDEOutcome:
        """Resolve the request's HyDE vector with *embedder*, the one the search embeds with.

        Resolved only once that embedder is known, so the hypothesis lands in the searched vector space.  An FTS-only search
        (a cold embedder, or a placeholder for a pending load) skips it.  An ``EmbedderNotReadyError`` stays as it is (it is
        retryable and mapped by the caller); any other failure is ``hyde_failed``.
        """
        request_hyde = RequestHyDE.for_request(
            command.query,
            requested=command.hyde,
            rag_fusion=command.rag_fusion,
            rag_fusion_enabled=self._config.rag_fusion.enabled,
            graph_mode=command.graph_mode,
            generator=self._hyde_generator,
            config=self._config.hyde,
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
        try:
            return await request_hyde.resolve(embedder, embedder_warm=not fts_only)
        except EmbedderNotReadyError:
            raise
        except Exception as exc:
            raise ServiceError(ServiceErrorKind.HYDE_FAILED) from exc

    async def _select_embedder(self, meta: CollectionMeta) -> _SelectedEmbedder:
        """The embedder serving the collection, its model name, and whether the load is still pending (cold).

        Through the cache only; the service never builds embedders.  Without a cache (an app built without the lifespan) the
        pipeline's global embedder and ``config.embedding_model`` serve the request.
        """
        pipeline = self._pipeline
        cache = self._embedder_cache
        if cache is None:
            logger.warning("search: embedder cache absent — falling back to the global embedder")
            return _SelectedEmbedder(pipeline.global_embedder, self._config.embedding_model, cold=False)
        active_model = resolve_active_model(meta, self._config)
        load = asyncio.ensure_future(cache.get_or_load(active_model))
        try:
            embedder = await asyncio.wait_for(asyncio.shield(load), timeout=_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS)
        except TimeoutError:
            if load.done():
                # Not the bound expiring: since 3.11 ``asyncio.TimeoutError`` *is* the builtin ``TimeoutError``, so one the
                # load itself raised lands here too.  Re-raise it for its own mapping instead of reporting a failed load as a
                # merely-unavailable one.  ``shield`` has already retrieved it, so no done-callback is needed.
                raise
            # S299/S290: the collection's embedder is not available in time.  Degrade to the FTS leg exactly as
            # ``warmup_for_search`` does, instead of parking the connection: the FTS index is built at ingest, so the
            # collection is answerable without a query vector.  ``shield`` keeps the load running (cancelling it would throw
            # away work another request is already waiting on), so it still populates the cache for later requests; the
            # embedder returned is a placeholder that ``fts_only`` guarantees is never embedded with.  A cache-level
            # ``EmbedderNotReadyError`` (a load wedged past the cache's own 120 s dedup wait) still propagates.
            load.add_done_callback(_discard_abandoned_load)
            logger.warning(
                "search: embedder %r unavailable after %ss (cache load still in flight) — serving this request FTS-only "
                "while it completes",
                active_model,
                _EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS,
            )
            return _SelectedEmbedder(pipeline.global_embedder, active_model, cold=True)
        except BaseException:
            # Anything else that unwinds this await (most commonly the ``CancelledError`` of a disconnecting client) leaves
            # the shielded load running with nobody left to read its outcome, which asyncio reports as a never-retrieved task
            # exception at GC time.
            if not load.done():
                load.add_done_callback(_discard_abandoned_load)
            raise
        return _SelectedEmbedder(embedder, active_model, cold=False)


class _SelectedEmbedder(NamedTuple):
    """The embedder serving a search, the model it stands for, and whether its load is still pending (so: FTS-only)."""

    embedder: Embedder
    model: str
    cold: bool


class _FailureTarget(NamedTuple):
    """What a failure log names: the ``extra`` fields of a timeout and the text of a metadata-store failure."""

    log_extra: dict[str, object]
    metadata_failure_message: str


def _single_target(collection: str) -> _FailureTarget:
    return _FailureTarget(
        log_extra={"collection": collection},
        metadata_failure_message=f"search: meta lookup failed for collection {collection!r}",
    )


def _multi_target(collections: tuple[str, ...]) -> _FailureTarget:
    return _FailureTarget(
        log_extra={"collections": list(collections)},
        metadata_failure_message="multi-collection search: metadata lookup failed",
    )


async def _map_failures(run: Awaitable[SearchOutcome]) -> SearchOutcome:
    """Await *run*, turning every exception it raises into the one :class:`ServiceError` that describes it."""
    try:
        return await run
    except ServiceError:
        raise
    except EmbedderNotReadyError as exc:
        raise ServiceError(ServiceErrorKind.EMBEDDER_NOT_READY) from exc
    except GraphCommunitiesNotBuiltError as exc:
        raise ServiceError(
            ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT,
            GraphCommunitiesNotBuiltError.MESSAGE_TEMPLATE.format(collection=exc.collection),
        ) from exc
    except (SearchBudgetExceeded, FanoutTimeoutError) as exc:
        raise ServiceError(ServiceErrorKind.TIMEOUT) from exc
    except CollectionNotFoundError as exc:
        raise ServiceError(ServiceErrorKind.NOT_FOUND) from exc
    except MetadataLookupError as exc:
        raise ServiceError(ServiceErrorKind.METADATA_STORE_ERROR) from exc
    except Exception as exc:
        raise ServiceError(ServiceErrorKind.INTERNAL) from exc


def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * _MILLISECONDS_PER_SECOND


def _log_stage_timings(recorder: StageRecorder | None, started: float, collection: str) -> None:
    if recorder is None:
        return
    recorder.record_total(started)
    logger.info(
        "stage timings",
        extra={
            "event_type": "stage_timings",
            "correlation_id": _correlation_id.get(),
            "endpoint": _ENDPOINT,
            "collection": collection,
            "stage_timings_ms": recorder.stage_timings_ms,
        },
    )


def _discard_abandoned_load(task: asyncio.Future[object]) -> None:
    """Consume the outcome of an embedder load a search stopped waiting for.

    The load is shielded so it keeps running and caches its embedder for later requests; retrieving its exception here
    consumes it and reports the failure.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("search: abandoned embedder load failed — %s", exc)


def _build_acl_gate(hit: SearchResult) -> AclGate:
    return AclGate(
        allowed_principals=None if hit.acl is None else list(hit.acl),
        source=hit.acl_source if hit.acl_source in VALID_ACL_SOURCES else None,
        sidecar_path=hit.acl_sidecar_path,
        warnings=list(hit.acl_warning),
    )


def _build_outcome(
    command: SearchCommand,
    result: SearchPipelineResult,
    *,
    embedding_model: str,
    hyde_applied: bool,
    hyde_warning: str | None,
) -> SearchOutcome:
    return SearchOutcome(
        results=tuple(
            SearchHit(result=hit, acl_gate=_build_acl_gate(hit) if command.acl_context else None)
            for hit in result.results
        ),
        acl_filtered=result.acl_filtered,
        excluded_collections=tuple(result.excluded_collections),
        embedding_model=embedding_model,
        hyde_applied=hyde_applied,
        rag_fusion_applied=result.rag_fusion_applied,
        rag_fusion_queries_used=result.rag_fusion_queries_used,
        rag_fusion_attempted=result.rag_fusion_attempted,
        graph_expansion_applied=result.graph_expansion_applied,
        expansion_used=hyde_applied or result.rag_fusion_applied or result.graph_expansion_applied,
        expansion_warning=hyde_warning or result.rag_fusion_warning,
        applied_filters=command.filters,
        ppr_entities_matched=result.ppr_entities_matched,
        include_metadata=command.wants_metadata,
    )
