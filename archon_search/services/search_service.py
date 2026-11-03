"""``SearchService``: the one implementation of a search that every interface shares (single-collection path).

A search runs the canonical flow once: normalise and validate the command, check the collection name and look the
collection up in the caller's namespace, select the collection's embedder through the cache (waiting at most
:data:`_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS`, then degrading to full-text-only), warm the models, resolve HyDE and RAG
Fusion, run the pipeline under the search budget and derive the response fields once.  Every failure is a
:class:`ServiceError` (a multi-collection command is the one exception until that path lands: ``NotImplementedError``);
nothing transport-specific is accepted or raised (``CancelledError`` is never swallowed, so a client abort still cancels
the search and its provider call).

The service returns raw pipeline hits plus the derived fields in a :class:`SearchOutcome`; each adapter renders its own
wire schema from it.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from archon_search._types import ExcludedCollection, SearchResult
from archon_search.config import resolve_active_model
from archon_search.constants import SEARCH_TIMEOUT_SECONDS as _SEARCH_TIMEOUT_SECONDS
from archon_search.constants import VALID_ACL_SOURCES
from archon_search.embedder_cache import EmbedderNotReadyError
from archon_search.filters import SearchFilters
from archon_search.pipeline import GraphCommunitiesNotBuiltError
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

if TYPE_CHECKING:
    from archon_search.collection_meta import CollectionMeta
    from archon_search.config import SearchConfig
    from archon_search.embedder import Embedder
    from archon_search.embedder_cache import EmbedderCache
    from archon_search.hyde import HyDEGenerator
    from archon_search.pipeline import SearchPipeline, SearchPipelineResult
    from archon_search.rag_fusion import RAGFusionGenerator

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
    embedder cache).  ``include_metadata`` is the two request flags
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
    ) -> None:
        self._pipeline = pipeline
        self._config = config
        self._embedder_cache = embedder_cache
        self._hyde_generator = hyde_generator
        self._rag_fusion_generator = rag_fusion_generator

    async def search(self, command: SearchCommand, caller: AuthIdentity) -> SearchOutcome:
        """Search the single collection *command* names inside *caller*'s namespace.

        Raises :class:`ServiceError` for every failure; the exception that caused it is attached as ``__cause__`` and never
        copied into the error's detail.  Raises ``NotImplementedError`` for a ``collections`` command until the multi path
        is served by this class.
        """
        normalized = normalize_search_command(
            command,
            max_fanout=self._config.max_fanout,
            top_k_max=self._config.top_k_max,
            graph_enabled=self._config.graph.enabled,
        )
        collection = normalized.collection
        if collection is None:
            raise NotImplementedError("the multi-collection path is not served by SearchService yet")
        meta = await self._lookup_collection(collection, caller.namespace)
        try:
            return await self._search_collection(normalized, collection, caller.namespace, meta)
        except ServiceError:
            raise
        except EmbedderNotReadyError as exc:
            raise ServiceError(ServiceErrorKind.EMBEDDER_NOT_READY) from exc
        except GraphCommunitiesNotBuiltError as exc:
            raise ServiceError(
                ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT,
                GraphCommunitiesNotBuiltError.MESSAGE_TEMPLATE.format(collection=exc.collection),
            ) from exc
        except SearchBudgetExceeded as exc:
            raise ServiceError(ServiceErrorKind.TIMEOUT) from exc
        except Exception as exc:
            raise ServiceError(ServiceErrorKind.INTERNAL) from exc

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
        # Resolved only now that the collection's embedder is known, so the hypothesis lands in that collection's vector
        # space.  An FTS-only search (a cold embedder, or the global placeholder for a pending load) skips it.
        try:
            hyde_outcome = await request_hyde.resolve(embedder, embedder_warm=not fts_only)
        except EmbedderNotReadyError:
            raise  # retryable: mapped by the caller
        except Exception as exc:
            raise ServiceError(ServiceErrorKind.HYDE_FAILED) from exc
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
