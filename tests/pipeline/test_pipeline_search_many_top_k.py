"""Per-request ``top_k`` on ``SearchPipeline.search_many`` and ``_fanout_merge_acl`` (C3, S21, S23, S72, S73).

Omitted (``None``) keeps today's behaviour exactly: the result count is ``top_k_return``, the candidate pool is
``max(top_k_retrieve * 3, 20)`` and every leg is trimmed to ``max(fanout_leg_trim, 1)``. A present ``top_k`` is the
result count at every multi-path cutoff, raises the pool to at least ``top_k`` and the leg trim to ``max(trim, top_k)``.
``explain`` never passes it. The stub store ranks a fixed per-collection corpus by index and the recording reranker
reorders it, so every outcome is deterministic and a cutoff applied before the rerank would show.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.chunker import DocumentChunker
from archon_search.collection_meta import CollectionMeta
from archon_search.config import GraphConfig, RAGFusionConfig
from archon_search.constants import DEFAULT_NAMESPACE, SEARCH_TIMEOUT_SECONDS
from archon_search.embedder import Embedder
from archon_search.filters import SearchFilters
from archon_search.graph_expander import ExpandedQuery
from archon_search.graph_types import Community, EntityType, GraphNode
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline, SearchPipelineResult
from archon_search.store_filters import GLOB_OVERFETCH_FACTOR

from .conftest import MockEmbedderBackend

_TOP_K_RETRIEVE = 15
_TOP_K_RETURN = 5
_DEFAULT_POOL = _TOP_K_RETRIEVE * 3
_LEG_TRIM = 40
_COLLECTIONS = ["alpha", "beta"]
_CORPUS_SIZE = 200
_SMALL_CORPUS_SIZE = 30
_LARGE_CORPUS_SIZE = 1000
_LARGE_TOP_K = 100  # the shipped `top_k_max`
_ABOVE_THE_TRIM = _LEG_TRIM * len(_COLLECTIONS) + 10  # more than every leg keeps by default
_GRAPH_ROWS = 60
_DOC_ID_PREFIX = "a" * 58
_RERANK_SCRAMBLE = 37  # a reranker score that reorders the store's ranking but stays deterministic
_RERANK_MODULUS = 997
_HELPER_POSITIONAL_ARGS = 5  # query, vector, collections, namespace, candidate_depth
_PATH_MODES = ("standard", "rag_fusion", "naive", "local")


def _rerank_score(candidate: ScoredSearchCandidate) -> float:
    return float((int(candidate.text.rsplit(" ", 1)[1]) * _RERANK_SCRAMBLE) % _RERANK_MODULUS)


class _RecordingReranker:
    """Scores a candidate by the number in its text, scrambled; records the size of every candidate set it receives."""

    def __init__(self) -> None:
        self.input_sizes: list[int] = []

    async def rerank_candidates(
        self, query: str, candidates: list[ScoredSearchCandidate], top_k: int
    ) -> list[ScoredSearchCandidate]:
        self.input_sizes.append(len(candidates))
        ranked = sorted(candidates, key=lambda c: (-_rerank_score(c), c.chunk_id))
        return ranked[:top_k]


def _candidate(collection: str, index: int) -> ScoredSearchCandidate:
    doc_id = f"{_DOC_ID_PREFIX}{_COLLECTIONS.index(collection)}{index:05d}"
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text=f"result {index}",
        source_path=f"/{collection}/{index}.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=None, vector_score=None, vector_score_kind=None,
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.5, reranker_score=None,
        ),
        collection=collection,
    )


def _row(index: int, collection: str) -> dict[str, Any]:
    """A raw store row for the global graph path (its candidates come from ``get_chunks_by_ids``)."""
    doc_id = f"{_DOC_ID_PREFIX}{_COLLECTIONS.index(collection) + 5}{index:05d}"
    return {
        "chunk_id": f"{doc_id}-000000",
        "source_path": f"/graph/{index}.md",
        "text": f"graph result {index}",
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
    """Hybrid search answers the first ``candidate_depth`` entries of the collection's corpus, ranked by index."""

    def __init__(self, corpus_size: int = _CORPUS_SIZE) -> None:
        self._corpus = {name: [_candidate(name, i) for i in range(corpus_size)] for name in _COLLECTIONS}
        self.depths: list[int] = []

    async def hybrid_search_with_trace(
        self, collection: str, vector: Any, query: str, *,
        candidate_depth: int, filters: Any = None, scope_filter: Any = None,
    ) -> list[ScoredSearchCandidate]:
        self.depths.append(candidate_depth)
        return list(self._corpus[collection][:candidate_depth])

    async def has_vector_index(self, collection: str) -> bool:
        return True

    async def get_chunks_by_ids(self, collection: str, chunk_ids: list[str]) -> list[dict[str, Any]]:
        return [_row(index, collection) for index in range(len(chunk_ids))]


def _graph_store(*, entity_match: bool = False) -> MagicMock:
    community = Community(
        community_id="comm-1",
        entity_ids=["entity-1"],
        representative_chunk_ids=[f"chunk-{i}" for i in range(_GRAPH_ROWS)],
        built_at=datetime(2024, 1, 1, tzinfo=UTC),
        summary_text=None,
    )
    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(return_value=[community])
    # Without an entity match every local leg takes its hybrid fallback; with one it merges community and hybrid candidates.
    nodes = [
        GraphNode(
            id="entity-1", entity_name="PaymentService", entity_type=EntityType.system,
            source_doc_id="doc-1", collection_name=_COLLECTIONS[0],
        )
    ] if entity_match else []
    graph_store.find_nodes_by_name = AsyncMock(return_value=nodes)
    graph_store.communities_table_exists = AsyncMock(return_value=True)
    graph_store.get_communities_for_entities = AsyncMock(return_value=[community])
    return graph_store


def _graph_expander() -> MagicMock:
    expander = MagicMock()

    async def _expand(query: str, collection: str, ns: str = DEFAULT_NAMESPACE) -> ExpandedQuery:
        return ExpandedQuery(original_query=query, expanded_text=query)

    expander.expand = AsyncMock(side_effect=_expand)
    return expander


def _pipeline(
    store: _RankedStore,
    reranker: _RecordingReranker | None,
    *,
    fanout_leg_trim: int = _LEG_TRIM,
    entity_match: bool = False,
) -> SearchPipeline:
    pipeline = SearchPipeline(
        store=store,  # type: ignore[arg-type]
        embedder=Embedder(MockEmbedderBackend()),
        reranker=reranker,  # type: ignore[arg-type]
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=_TOP_K_RETRIEVE,
        top_k_return=_TOP_K_RETURN,
        fanout_leg_trim=fanout_leg_trim,
        graph_store=_graph_store(entity_match=entity_match),
        graph_config=GraphConfig(enabled=True),
        graph_expander=_graph_expander(),
    )
    pipeline.get_all_collections_meta = AsyncMock(  # type: ignore[method-assign]
        return_value=[CollectionMeta(name=n, active_embedding_model="mock-embedder") for n in _COLLECTIONS]
    )
    return pipeline


def _search_kwargs(mode: str) -> dict[str, Any]:
    if mode == "rag_fusion":
        generator = MagicMock()
        generator.generate_variants = AsyncMock(return_value=["variant one"])
        return {
            "rag_fusion": True,
            "rag_fusion_generator": generator,
            "rag_fusion_config": RAGFusionConfig(enabled=True),
        }
    if mode in ("naive", "local", "global"):
        return {"graph_mode": mode}
    return {}


async def _search_many(
    pipeline: SearchPipeline, mode: str = "standard", **overrides: Any
) -> SearchPipelineResult:
    await pipeline._global_embedder.embed(["warmup"])
    kwargs = {**_search_kwargs(mode), **overrides}
    return await pipeline.search_many("query", list(_COLLECTIONS), **kwargs)


def _ids(result: SearchPipelineResult) -> list[str]:
    return [hit.chunk_id for hit in result.results]


def _expected_ranking(per_leg: int, count: int) -> list[str]:
    """The ids a leg trim of ``per_leg`` and a cutoff of ``count`` must give, computed without the pipeline."""
    kept = [_candidate(name, i) for name in _COLLECTIONS for i in range(per_leg)]
    kept.sort(key=lambda c: (-_rerank_score(c), c.chunk_id))
    return [c.chunk_id for c in kept[:count]]


# ---------------------------------------------------------------------------
# Leg trim: max(trim, top_k) at all four sites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _PATH_MODES)
@pytest.mark.parametrize(
    ("top_k", "expected_trim"),
    [
        pytest.param(None, _LEG_TRIM, id="omitted-keeps-the-trim"),
        pytest.param(1, _LEG_TRIM, id="small-top-k-keeps-the-trim"),
        pytest.param(_LEG_TRIM, _LEG_TRIM, id="top-k-equal-to-the-trim"),
        pytest.param(_LEG_TRIM + 1, _LEG_TRIM + 1, id="top-k-above-the-trim-raises-it"),
        pytest.param(_LARGE_TOP_K, _LARGE_TOP_K, id="large-top-k-raises-the-trim"),
    ],
)
async def test_multi_leg_trim_is_max_of_trim_and_top_k(mode: str, top_k: int | None, expected_trim: int) -> None:
    reranker = _RecordingReranker()

    await _search_many(_pipeline(_RankedStore(), reranker), mode, top_k=top_k)

    assert reranker.input_sizes == [expected_trim * len(_COLLECTIONS)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("top_k", "expected_trim"),
    [pytest.param(None, 40, id="omitted"), pytest.param(10, 40, id="below"), pytest.param(75, 75, id="above")],
)
async def test_fanout_merge_acl_trims_each_leg_to_max_of_trim_and_top_k(top_k: int | None, expected_trim: int) -> None:
    pipeline = _pipeline(_RankedStore(), None)
    kwargs = {} if top_k is None else {"top_k": top_k}

    merged, _, _ = await pipeline._fanout_merge_acl(
        "query", [0.1] * 4, list(_COLLECTIONS), DEFAULT_NAMESPACE, _CORPUS_SIZE, **kwargs
    )

    assert len(merged) == expected_trim * len(_COLLECTIONS)


@pytest.mark.asyncio
async def test_a_trim_below_one_stays_one_without_top_k_and_follows_top_k_with_it() -> None:
    omitted = await _fanout(_pipeline(_RankedStore(), None, fanout_leg_trim=0))
    raised = await _fanout(_pipeline(_RankedStore(), None, fanout_leg_trim=0), top_k=7)

    assert (omitted, raised) == (len(_COLLECTIONS), 7 * len(_COLLECTIONS))


async def _fanout(pipeline: SearchPipeline, **kwargs: Any) -> int:
    merged, _, _ = await pipeline._fanout_merge_acl(
        "query", [0.1] * 4, list(_COLLECTIONS), DEFAULT_NAMESPACE, _CORPUS_SIZE, **kwargs
    )
    return len(merged)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("top_k", "expected_trim"),
    [
        pytest.param(None, _LEG_TRIM, id="omitted-keeps-the-trim"),
        pytest.param(_GRAPH_ROWS, _GRAPH_ROWS, id="top-k-above-the-trim-keeps-the-community-candidates"),
        pytest.param(_LARGE_TOP_K, _LARGE_TOP_K, id="large-top-k-adds-hybrid-candidates"),
    ],
)
async def test_local_graph_leg_with_an_entity_match_trims_the_merged_leg_to_max_of_trim_and_top_k(
    top_k: int | None, expected_trim: int
) -> None:
    reranker = _RecordingReranker()
    pipeline = _pipeline(_RankedStore(), reranker, entity_match=True)

    result = await _search_many(pipeline, "local", top_k=top_k)

    assert result.graph_expansion_applied is True
    assert reranker.input_sizes == [expected_trim * len(_COLLECTIONS)]


# ---------------------------------------------------------------------------
# Pool sites (the four candidate pools of `search_many`: standard, RAG Fusion, naive graph, local graph)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _PATH_MODES)
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
async def test_search_many_pool_is_raised_to_top_k_and_unchanged_without_it(
    mode: str, top_k: int | None, expected_pool: int
) -> None:
    store = _RankedStore()

    await _search_many(_pipeline(store, _RecordingReranker()), mode, top_k=top_k)

    assert store.depths, f"the {mode} path must run a hybrid search"
    assert set(store.depths) == {expected_pool}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("top_k", "expected_depth"),
    [(None, _DEFAULT_POOL * GLOB_OVERFETCH_FACTOR), (_LARGE_TOP_K, _LARGE_TOP_K * GLOB_OVERFETCH_FACTOR)],
)
async def test_search_many_glob_overfetch_multiplies_the_raised_pool(top_k: int | None, expected_depth: int) -> None:
    store = _RankedStore()

    result = await _search_many(
        _pipeline(store, _RecordingReranker()), filters=SearchFilters(source_path_glob="/alpha/*"), top_k=top_k
    )

    assert set(store.depths) == {expected_depth}
    assert all(hit.source_path.startswith("/alpha/") for hit in result.results)


# ---------------------------------------------------------------------------
# Default path: byte-identical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _PATH_MODES)
@pytest.mark.parametrize("passes_top_k", [False, True], ids=["argument-absent", "argument-none"])
async def test_search_many_without_top_k_is_byte_identical_in_results_and_pool_sites(
    mode: str, passes_top_k: bool
) -> None:
    store = _RankedStore()
    reranker = _RecordingReranker()
    overrides = {"top_k": None} if passes_top_k else {}

    result = await _search_many(_pipeline(store, reranker), mode, **overrides)

    assert set(store.depths) == {_DEFAULT_POOL}
    assert reranker.input_sizes == [_LEG_TRIM * len(_COLLECTIONS)]
    assert _ids(result) == _expected_ranking(_LEG_TRIM, _TOP_K_RETURN)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [*_PATH_MODES, "global"])
@pytest.mark.parametrize("rerank", [True, False], ids=["reranked", "not-reranked"])
@pytest.mark.parametrize("top_k", [None, 12], ids=["omitted", "present"])
async def test_search_many_cutoff_uses_top_k_when_present_and_top_k_return_without_it(
    mode: str, top_k: int | None, rerank: bool
) -> None:
    result = await _search_many(_pipeline(_RankedStore(), _RecordingReranker()), mode, top_k=top_k, rerank=rerank)

    assert len(result.results) == (_TOP_K_RETURN if top_k is None else top_k)


@pytest.mark.asyncio
@pytest.mark.parametrize("rerank", [True, False], ids=["reranked", "not-reranked"])
async def test_search_many_rag_fusion_embedding_fallback_keeps_top_k_and_the_raised_trim(rerank: bool) -> None:
    store = _RankedStore()
    reranker = _RecordingReranker()
    pipeline = _pipeline(store, reranker)
    await pipeline._global_embedder.embed(["warmup"])
    real_embed = pipeline._global_embedder.embed_one
    calls = {"n": 0}

    async def _embed_one(text: str) -> list[float]:
        calls["n"] += 1
        if calls["n"] == 2:  # the variant embedding of the parallel gather fails, the standard fallback embeds again
            raise RuntimeError("embedding failed")
        return await real_embed(text)

    pipeline._global_embedder.embed_one = _embed_one  # type: ignore[method-assign]

    result = await pipeline.search_many(
        "query", list(_COLLECTIONS), top_k=_LARGE_TOP_K, rerank=rerank, **_search_kwargs("rag_fusion")
    )

    assert result.rag_fusion_attempted is True
    assert result.rag_fusion_applied is False
    assert len(result.results) == _LARGE_TOP_K
    assert reranker.input_sizes == ([_LARGE_TOP_K * len(_COLLECTIONS)] if rerank else [])
    assert set(store.depths) == {_LARGE_TOP_K}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("top_k", "expected_count"),
    [(_LARGE_TOP_K, _LARGE_TOP_K), (_GRAPH_ROWS * len(_COLLECTIONS) + 10, _GRAPH_ROWS * len(_COLLECTIONS))],
    ids=["within-the-community-candidates", "above-them-returns-all-available"],
)
async def test_search_many_global_returns_at_most_the_community_candidates(top_k: int, expected_count: int) -> None:
    result = await _search_many(_pipeline(_RankedStore(), _RecordingReranker()), "global", top_k=top_k)

    assert result.graph_expansion_applied is True
    assert len(set(_ids(result))) == len(result.results) == expected_count


# ---------------------------------------------------------------------------
# S23: explain stays unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_pool_sites_and_trim_stay_unchanged_whatever_top_k_search_receives() -> None:
    fresh_store, fresh_reranker = _RankedStore(), _RecordingReranker()
    await _pipeline(fresh_store, fresh_reranker).explain("query", collections=list(_COLLECTIONS))

    store, reranker = _RankedStore(), _RecordingReranker()
    pipeline = _pipeline(store, reranker)
    await _search_many(pipeline, top_k=_LARGE_TOP_K)
    store.depths.clear()
    reranker.input_sizes.clear()
    helper_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_helper = pipeline._fanout_merge_acl

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        helper_calls.append((args, kwargs))
        return await real_helper(*args, **kwargs)

    pipeline._fanout_merge_acl = _spy  # type: ignore[method-assign]
    await pipeline.explain("query", collections=list(_COLLECTIONS))

    assert fresh_store.depths == [_DEFAULT_POOL] * len(_COLLECTIONS)
    assert store.depths == fresh_store.depths
    assert reranker.input_sizes == fresh_reranker.input_sizes == [_LEG_TRIM * len(_COLLECTIONS)]
    assert len(helper_calls) == 1
    helper_args, helper_kwargs = helper_calls[0]
    assert len(helper_args) == _HELPER_POSITIONAL_ARGS
    assert "top_k" not in helper_kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["standard", "rag_fusion", "naive", "local"])
async def test_explain_single_collection_pool_sites_stay_unchanged_whatever_top_k_search_receives(mode: str) -> None:
    kwargs = _search_kwargs(mode)

    async def _explain_depths(*, after_a_large_top_k_search: bool) -> list[int]:
        store = _RankedStore()
        pipeline = _pipeline(store, _RecordingReranker(), entity_match=True)
        if after_a_large_top_k_search:
            await _search_many(pipeline, mode, top_k=_LARGE_TOP_K)
            store.depths.clear()
        await pipeline._global_embedder.embed(["warmup"])
        await pipeline.explain(
            "query", _COLLECTIONS[0], embedder=pipeline._global_embedder,
            graph_mode=kwargs.get("graph_mode"),
            **{k: v for k, v in kwargs.items() if k.startswith("rag_fusion")},
        )
        return store.depths

    fresh = await _explain_depths(after_a_large_top_k_search=False)
    after = await _explain_depths(after_a_large_top_k_search=True)

    assert fresh, f"the {mode} explain path must run a hybrid search"
    assert set(fresh) == {_DEFAULT_POOL}
    assert after == fresh


# ---------------------------------------------------------------------------
# S21, S72, S73 on the multi path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _PATH_MODES)
async def test_multi_top_k_above_the_corpus_returns_all_and_above_the_leg_trim_returns_exactly_top_k(mode: str) -> None:
    small = await _search_many(
        _pipeline(_RankedStore(_SMALL_CORPUS_SIZE), _RecordingReranker()), mode, top_k=_LARGE_TOP_K
    )
    large = await _search_many(_pipeline(_RankedStore(), _RecordingReranker()), mode, top_k=_ABOVE_THE_TRIM)

    assert len(small.results) == _SMALL_CORPUS_SIZE * len(_COLLECTIONS)
    assert len(large.results) == _ABOVE_THE_TRIM


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _PATH_MODES)
async def test_multi_returns_exactly_a_large_top_k_within_the_budget(mode: str) -> None:
    pipeline = _pipeline(_RankedStore(_LARGE_CORPUS_SIZE), _RecordingReranker())

    started = time.monotonic()
    result = await _search_many(pipeline, mode, top_k=_LARGE_TOP_K)

    assert time.monotonic() - started < SEARCH_TIMEOUT_SECONDS
    assert len(result.results) == _LARGE_TOP_K


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _PATH_MODES)
@pytest.mark.parametrize(("larger", "smaller"), [(_LEG_TRIM, 10), (_LEG_TRIM, 1), (25, 5), (10, 10)])
async def test_multi_exact_n_and_prefix_stable_within_the_leg_trim_of_40(mode: str, larger: int, smaller: int) -> None:
    big = await _search_many(_pipeline(_RankedStore(), _RecordingReranker()), mode, top_k=larger)
    small = await _search_many(_pipeline(_RankedStore(), _RecordingReranker()), mode, top_k=smaller)

    assert len(big.results) == larger
    assert len(small.results) == smaller
    assert _ids(big)[:smaller] == _ids(small)
