"""Per-request ``top_k`` on ``SearchPipeline.search`` (C3, S21, S22, S72, S73).

Omitted (``None``) keeps today's behaviour exactly: the result count is ``top_k_return`` and the candidate pool is
``max(top_k_retrieve * 3, 20)``. A present ``top_k`` is the result count at every cutoff of the single path and raises
the pool to at least ``top_k``. The stub store ranks a fixed corpus by index, so every outcome is deterministic; the
stub reranker reorders that ranking, so a cutoff applied before the rerank would show.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.chunker import DocumentChunker
from archon_search.config import GraphConfig, RAGFusionConfig
from archon_search.constants import DEFAULT_NAMESPACE, SEARCH_TIMEOUT_SECONDS
from archon_search.embedder import Embedder
from archon_search.filters import SearchFilters
from archon_search.graph_expander import ExpandedQuery
from archon_search.graph_types import Community, EntityType, GraphNode
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline, SearchPipelineResult
from archon_search.ppr_walker import PPRWalkResult
from archon_search.reranker import Reranker

from .conftest import MockEmbedderBackend

_TOP_K_RETRIEVE = 15
_TOP_K_RETURN = 5
_DEFAULT_POOL = _TOP_K_RETRIEVE * 3
_ABOVE_THE_POOL = _DEFAULT_POOL + 15
_CORPUS_SIZE = 200
_SMALL_CORPUS_SIZE = 30
_GRAPH_ROWS = 60
_MID_TOP_K = 12
_LARGE_CORPUS_SIZE = 1000
_LARGE_TOP_K = 100  # the shipped `top_k_max`
_COLLECTION = "col"
_ENTITY_ID = "entity-1"
_DOC_ID_PREFIX = "a" * 59
_GRAPH_ID_OFFSET = 10_000  # graph rows never share a chunk id with a hybrid candidate, so the merge keeps both
_DENIED_ACL = ["tenantX"]
_EVERY_THIRD = 3
_RERANK_SCRAMBLE = 37  # a reranker score that reorders the store's ranking but stays deterministic
_RERANK_MODULUS = 997
_ATTRITION_LOGGER = "archon_search.pipeline"
_PATH_MODES = ("standard", "rag_fusion", "ppr", "local", "global")
_POOL_MODES = ("standard", "rag_fusion", "ppr", "local")


class _ScramblingRerankerBackend:
    """Scores a candidate by the number in its text, scrambled: the rerank order differs from the store order."""

    is_warm: bool = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [float((int(text.rsplit(" ", 1)[1]) * _RERANK_SCRAMBLE) % _RERANK_MODULUS) for _, text in pairs]


def _candidate(index: int, *, acl: list[str] | None = None) -> ScoredSearchCandidate:
    doc_id = f"{_DOC_ID_PREFIX}{index:05d}"
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text=f"result {index}",
        source_path=f"/path/{index}.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=None, vector_score=None, vector_score_kind=None,
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.5, reranker_score=None,
        ),
        collection=_COLLECTION,
        acl=acl,
    )


def _row(index: int) -> dict[str, Any]:
    """A raw store row for the graph paths (their candidates come from ``get_chunks_by_ids``)."""
    graph_index = _GRAPH_ID_OFFSET + index
    doc_id = f"{_DOC_ID_PREFIX}{graph_index:05d}"
    return {
        "chunk_id": f"{doc_id}-000000",
        "source_path": f"/path/{graph_index}.md",
        "text": f"graph result {graph_index}",
        "doc_id": doc_id,
        "namespace": DEFAULT_NAMESPACE,
        "chunk_index": 0,
        "total_chunks": 1,
        "language": "en",
        "acl": [DEFAULT_NAMESPACE],
        "scopes": [],
        "metadata": "{}",
        "ingested_by": "cli",
        "file_type": "md",
        "indexed_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }


class _RankedStore:
    """Hybrid search answers the first ``candidate_depth`` corpus entries, like a store ranking by index."""

    def __init__(
        self, corpus: list[ScoredSearchCandidate], *, failing_searches: int = 0, vector_index: bool = True
    ) -> None:
        self._corpus = corpus
        self._failing_searches = failing_searches
        self._vector_index = vector_index
        self.depths: list[int] = []

    async def hybrid_search_with_trace(
        self, collection: str, vector: Any, query: str, *,
        candidate_depth: int, filters: Any = None, scope_filter: Any = None,
    ) -> list[ScoredSearchCandidate]:
        self.depths.append(candidate_depth)
        if len(self.depths) <= self._failing_searches:
            raise RuntimeError("hybrid search failed")
        return self._corpus[:candidate_depth]

    async def has_vector_index(self, collection: str) -> bool:
        return self._vector_index

    async def get_chunks_by_ids(self, collection: str, chunk_ids: list[str]) -> list[dict[str, Any]]:
        return [_row(index) for index in range(len(chunk_ids))]


def _graph_store(representative_chunks: int = _GRAPH_ROWS) -> MagicMock:
    community = Community(
        community_id="comm-1",
        entity_ids=[_ENTITY_ID],
        representative_chunk_ids=[f"chunk-{i}" for i in range(representative_chunks)],
        built_at=datetime(2024, 1, 1, tzinfo=UTC),
        summary_text=None,
    )
    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(return_value=[community])
    graph_store.find_nodes_by_name = AsyncMock(return_value=[
        GraphNode(
            id=_ENTITY_ID, entity_name="PaymentService", entity_type=EntityType.system,
            source_doc_id="doc-1", collection_name=_COLLECTION,
        )
    ])
    graph_store.communities_table_exists = AsyncMock(return_value=True)
    graph_store.get_communities_for_entities = AsyncMock(return_value=[community])
    return graph_store


def _graph_store_failing(method: str) -> MagicMock:
    graph_store = _graph_store()
    setattr(graph_store, method, AsyncMock(side_effect=RuntimeError(f"{method} failed")))
    return graph_store


def _ppr_walker(chunk_count: int = _GRAPH_ROWS, *, entities_matched: int = 1) -> MagicMock:
    walker = MagicMock()
    walker.walk = AsyncMock(return_value=PPRWalkResult(
        entity_ids=[_ENTITY_ID], chunk_ids=[f"chunk-{i}" for i in range(chunk_count)],
        entities_matched=entities_matched,
    ))
    return walker


def _pipeline(store: _RankedStore, **overrides: Any) -> SearchPipeline:
    parts: dict[str, Any] = {
        "graph_store": _graph_store(),
        "graph_config": GraphConfig(enabled=True),
        "ppr_walker": _ppr_walker(),
    }
    parts.update(overrides)
    return SearchPipeline(
        store=store,  # type: ignore[arg-type]
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(_ScramblingRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=_TOP_K_RETRIEVE,
        top_k_return=_TOP_K_RETURN,
        **parts,
    )


def _failing_ppr_walker() -> MagicMock:
    walker = MagicMock()
    walker.walk = AsyncMock(side_effect=RuntimeError("walk failed"))
    return walker


def _failing_rag_generator(error: Exception) -> MagicMock:
    generator = MagicMock()
    generator.generate_variants = AsyncMock(side_effect=error)
    return generator


def _search_kwargs(mode: str) -> dict[str, Any]:
    if mode == "rag_fusion":
        generator = MagicMock()
        generator.generate_variants = AsyncMock(return_value=["variant one"])
        return {
            "rag_fusion": True,
            "rag_fusion_generator": generator,
            "rag_fusion_config": RAGFusionConfig(enabled=True),
        }
    if mode in ("ppr", "local", "global"):
        return {"graph_mode": mode}
    return {}


async def _search(
    pipeline: SearchPipeline, mode: str = "standard", *, query: str = "query", **overrides: Any
) -> SearchPipelineResult:
    await pipeline._global_embedder.embed(["warmup"])
    search_kwargs = {**_search_kwargs(mode), **overrides}
    return await pipeline.search(query, _COLLECTION, embedder=pipeline._global_embedder, **search_kwargs)


def _ids(result: SearchPipelineResult) -> list[str]:
    return [hit.chunk_id for hit in result.results]


def _assert_mode_ran(mode: str, result: SearchPipelineResult) -> None:
    """The mode's own path ran: a silent fallback to the standard path would make the other assertions vacuous."""
    if mode == "rag_fusion":
        assert result.rag_fusion_applied is True
    elif mode == "ppr":
        assert result.ppr_entities_matched == 1
        assert result.graph_expansion_applied is True
    elif mode in ("local", "global"):
        assert result.graph_expansion_applied is True


# ---------------------------------------------------------------------------
# Pool sites (the four candidate pools of `search`: standard, RAG Fusion, PPR, local)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _POOL_MODES)
@pytest.mark.parametrize(
    ("top_k", "expected_pool"),
    [
        pytest.param(None, _DEFAULT_POOL, id="omitted-keeps-the-default-pool"),
        pytest.param(1, _DEFAULT_POOL, id="small-top-k-keeps-the-default-pool"),
        pytest.param(_DEFAULT_POOL, _DEFAULT_POOL, id="top-k-equal-to-the-pool"),
        pytest.param(_DEFAULT_POOL + 1, _DEFAULT_POOL + 1, id="top-k-above-the-pool-raises-it"),
        pytest.param(_LARGE_TOP_K, _LARGE_TOP_K, id="large-top-k-raises-the-pool"),
    ],
)
async def test_search_pool_is_raised_to_top_k_and_unchanged_without_it(
    mode: str, top_k: int | None, expected_pool: int
) -> None:
    store = _RankedStore([_candidate(i) for i in range(_CORPUS_SIZE)])

    result = await _search(_pipeline(store), mode, top_k=top_k)

    _assert_mode_ran(mode, result)
    assert store.depths, f"the {mode} path must run a hybrid search"
    assert set(store.depths) == {expected_pool}


# ---------------------------------------------------------------------------
# Result count at every single-path cutoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("rerank", [True, False], ids=["reranked", "not-reranked"])
@pytest.mark.parametrize("top_k", [None, _MID_TOP_K], ids=["omitted", "present"])
@pytest.mark.parametrize("mode", _PATH_MODES)
async def test_search_cutoff_uses_top_k_when_present_and_top_k_return_without_it(
    mode: str, top_k: int | None, rerank: bool
) -> None:
    store = _RankedStore([_candidate(i) for i in range(_CORPUS_SIZE)])

    result = await _search(_pipeline(store), mode, top_k=top_k, rerank=rerank)

    _assert_mode_ran(mode, result)
    assert len(result.results) == (_TOP_K_RETURN if top_k is None else top_k)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _POOL_MODES)
async def test_search_above_the_default_pool_returns_exactly_top_k_on_every_path(mode: str) -> None:
    store = _RankedStore([_candidate(i) for i in range(_CORPUS_SIZE)])

    result = await _search(_pipeline(store), mode, top_k=_ABOVE_THE_POOL)

    _assert_mode_ran(mode, result)
    assert len(result.results) == _ABOVE_THE_POOL


# ---------------------------------------------------------------------------
# Every fallback hands top_k on to the standard path
# ---------------------------------------------------------------------------

_FALLBACKS = [
    pytest.param("rag_fusion", {}, {"rag_fusion_generator": _failing_rag_generator(TimeoutError())},
                 id="rag-fusion-timeout"),
    pytest.param("rag_fusion", {}, {"rag_fusion_generator": _failing_rag_generator(RuntimeError("boom"))},
                 id="rag-fusion-generator-failure"),
    pytest.param("ppr", {"ppr_walker": _failing_ppr_walker()}, {}, id="ppr-walk-failure"),
    pytest.param("ppr", {"ppr_walker": None}, {}, id="ppr-without-a-walker"),
    pytest.param("ppr", {"ppr_walker": _ppr_walker(entities_matched=0)}, {}, id="ppr-no-entity-matched"),
    pytest.param("ppr", {"ppr_walker": _ppr_walker(chunk_count=0)}, {}, id="ppr-no-chunk-rows"),
    pytest.param("global", {"graph_store": None}, {}, id="global-without-a-graph-store"),
    pytest.param("global", {"graph_store": _graph_store(representative_chunks=0)}, {}, id="global-no-chunk-rows"),
    pytest.param("local", {"graph_store": None}, {}, id="local-without-a-graph-store"),
    pytest.param("local", {}, {"query": ""}, id="local-empty-query"),
    pytest.param("local", {"graph_store": _graph_store_failing("find_nodes_by_name")}, {}, id="local-entity-lookup-failure"),
    pytest.param("local", {"graph_store": _graph_store_failing("communities_table_exists")}, {},
                 id="local-communities-table-check-failure"),
    pytest.param("local", {"graph_store": _graph_store_failing("get_communities_for_entities")}, {},
                 id="local-community-lookup-failure"),
    pytest.param("local", {"graph_store": _graph_store(representative_chunks=0)}, {}, id="local-no-chunk-ids"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "pipeline_overrides", "search_overrides"), _FALLBACKS)
async def test_search_fallbacks_keep_top_k_and_the_raised_pool(
    mode: str, pipeline_overrides: dict[str, Any], search_overrides: dict[str, Any]
) -> None:
    store = _RankedStore([_candidate(i) for i in range(_CORPUS_SIZE)])

    result = await _search(_pipeline(store, **pipeline_overrides), mode, top_k=_ABOVE_THE_POOL, **search_overrides)

    assert len(result.results) == _ABOVE_THE_POOL
    assert set(store.depths) == {_ABOVE_THE_POOL}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "store_options",
    [{"vector_index": False}, {"failing_searches": 2}],
    ids=["rag-fusion-without-a-vector-index", "rag-fusion-every-leg-fails"],
)
async def test_search_rag_fusion_store_fallbacks_keep_top_k_and_the_raised_pool(store_options: dict[str, Any]) -> None:
    store = _RankedStore([_candidate(i) for i in range(_CORPUS_SIZE)], **store_options)

    result = await _search(_pipeline(store), "rag_fusion", top_k=_ABOVE_THE_POOL)

    assert len(result.results) == _ABOVE_THE_POOL
    assert store.depths[-1] == _ABOVE_THE_POOL


@pytest.mark.asyncio
async def test_search_global_returns_at_most_the_community_candidates_even_for_a_larger_top_k() -> None:
    """Global mode ranks community representatives, not the pool: ``max_global_candidates`` bounds it."""
    store = _RankedStore([_candidate(i) for i in range(_CORPUS_SIZE)])
    pipeline = _pipeline(store, graph_config=GraphConfig(enabled=True, max_global_candidates=_MID_TOP_K))

    result = await _search(pipeline, "global", top_k=_ABOVE_THE_POOL)

    assert len(result.results) == _MID_TOP_K
    assert store.depths == []


@pytest.mark.asyncio
async def test_search_local_with_isolated_entities_falls_back_with_top_k() -> None:
    """Entities without a community fall back to the naive expansion, then to the standard path."""
    store = _RankedStore([_candidate(i) for i in range(_CORPUS_SIZE)])
    graph_store = _graph_store()
    graph_store.get_communities_for_entities = AsyncMock(return_value=[])

    result = await _search(_pipeline(store, graph_store=graph_store), "local", top_k=_ABOVE_THE_POOL)

    assert len(result.results) == _ABOVE_THE_POOL
    assert set(store.depths) == {_ABOVE_THE_POOL}


@pytest.mark.asyncio
async def test_search_naive_expansion_and_cold_embedder_degrade_keep_top_k() -> None:
    store = _RankedStore([_candidate(i) for i in range(_CORPUS_SIZE)])
    expander = MagicMock()
    expander.expand = AsyncMock(return_value=ExpandedQuery(
        original_query="query", expanded_text="query expanded", expansion_applied=True,
    ))
    pipeline = _pipeline(store, graph_expander=expander)

    naive = await _search(pipeline, graph_mode="naive", top_k=_ABOVE_THE_POOL)
    degraded = await _search(pipeline, fts_only=True, top_k=_ABOVE_THE_POOL)

    assert naive.graph_expansion_applied is True
    assert len(naive.results) == len(degraded.results) == _ABOVE_THE_POOL
    assert set(store.depths) == {_ABOVE_THE_POOL}


# ---------------------------------------------------------------------------
# Shortfall warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("survivors", "top_k", "warns", "expected_fragment"),
    [
        pytest.param(7, 10, True, "only 7/10", id="effective-top-k-above-the-survivors-warns"),
        pytest.param(3, 2, False, "", id="effective-top-k-below-the-survivors-stays-silent"),
        pytest.param(3, None, True, "only 3/5", id="omitted-compares-against-top-k-return"),
    ],
)
async def test_search_shortfall_warning_uses_the_effective_top_k(
    caplog: pytest.LogCaptureFixture, survivors: int, top_k: int | None, warns: bool, expected_fragment: str
) -> None:
    store = _RankedStore([_candidate(i) for i in range(survivors)])

    with caplog.at_level(logging.WARNING, logger=_ATTRITION_LOGGER):
        await _search(_pipeline(store), filters=SearchFilters(file_type="md"), top_k=top_k)

    messages = [r.message for r in caplog.records if "filter+ACL combined attrition" in r.message]
    assert bool(messages) is warns
    if warns:
        assert expected_fragment in messages[0]


# ---------------------------------------------------------------------------
# S21, S72, S73 on the single path (stubbed ranking)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_exact_n_when_enough_chunks_pass_the_post_filters() -> None:
    # Every third candidate is denied by the ACL gate and, in the second search, every odd one by the glob.
    corpus = [_candidate(i, acl=_DENIED_ACL if i % _EVERY_THIRD == 0 else None) for i in range(_CORPUS_SIZE)]

    acl_only = await _search(_pipeline(_RankedStore(corpus)), top_k=_MID_TOP_K)
    with_glob = await _search(
        _pipeline(_RankedStore(corpus)), filters=SearchFilters(source_path_glob="*[02468].md"), top_k=_MID_TOP_K,
    )

    assert len(acl_only.results) == _MID_TOP_K
    assert acl_only.acl_filtered is True
    assert all(int(hit.doc_id[-5:]) % _EVERY_THIRD != 0 for hit in acl_only.results)
    assert len(with_glob.results) == _MID_TOP_K
    assert all(int(hit.doc_id[-5:]) % 2 == 0 for hit in with_glob.results)


@pytest.mark.asyncio
async def test_search_top_k_above_the_corpus_returns_all_available_and_above_the_pool_returns_exactly_top_k() -> None:
    small = _pipeline(_RankedStore([_candidate(i) for i in range(_SMALL_CORPUS_SIZE)]))
    large = _pipeline(_RankedStore([_candidate(i) for i in range(_CORPUS_SIZE)]))

    fewer_than_top_k = await _search(small, top_k=_DEFAULT_POOL + 5)
    above_the_pool = await _search(large, top_k=_ABOVE_THE_POOL)

    assert len(fewer_than_top_k.results) == _SMALL_CORPUS_SIZE
    assert len(above_the_pool.results) == _ABOVE_THE_POOL


@pytest.mark.asyncio
async def test_search_returns_exactly_a_large_top_k_within_the_budget() -> None:
    pipeline = _pipeline(_RankedStore([_candidate(i) for i in range(_LARGE_CORPUS_SIZE)]))

    started = time.monotonic()
    result = await _search(pipeline, top_k=_LARGE_TOP_K)
    elapsed = time.monotonic() - started

    assert len(result.results) == _LARGE_TOP_K
    assert elapsed < SEARCH_TIMEOUT_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("larger", "smaller"),
    [(_DEFAULT_POOL, _TOP_K_RETURN), (_DEFAULT_POOL, _DEFAULT_POOL - 1), (_MID_TOP_K, 1)],
)
async def test_search_prefix_stable_while_n_and_m_fit_the_pool_of_45(larger: int, smaller: int) -> None:
    pipeline = _pipeline(_RankedStore([_candidate(i) for i in range(_CORPUS_SIZE)]))

    wide = await _search(pipeline, top_k=larger)
    narrow = await _search(pipeline, top_k=smaller)

    assert _ids(wide)[:smaller] == _ids(narrow)
    assert _ids(narrow) != [_candidate(i).chunk_id for i in range(smaller)] or smaller == 1, (
        "the reranker must reorder the store ranking, otherwise the prefix check proves nothing"
    )
