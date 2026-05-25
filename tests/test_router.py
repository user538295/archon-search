"""Tests for MultiCollectionRouter ."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.router import MultiCollectionRouter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_embedder(vector: list[float] | None = None) -> MagicMock:
    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=vector or [1.0, 0.0])
    return embedder


def _router(
    shortlist_size: int = 5,
    confidence_threshold: float = 0.5,
    embedding_model: str = "model-a",
    embedder: MagicMock | None = None,
    initial_metadata: list[CollectionMeta] | None = None,
) -> MultiCollectionRouter:
    return MultiCollectionRouter(
        search_url="http://localhost:9999",
        embedder=embedder or _make_embedder(),
        shortlist_size=shortlist_size,
        confidence_threshold=confidence_threshold,
        embedding_model=embedding_model,
        initial_metadata=initial_metadata,
    )


def _meta(
    name: str,
    centroid: list[float] | None = None,
    embedding_model: str = "model-a",
    description: str | None = None,
) -> CollectionMeta:
    return CollectionMeta(
        name=name,
        centroid=centroid,
        embedding_model=embedding_model,
        description=description,
    )


# ---------------------------------------------------------------------------
# rank() tests
# ---------------------------------------------------------------------------


def test_rank_returns_sorted_by_similarity() -> None:
    router = _router(shortlist_size=5, confidence_threshold=0.0)
    # query vector [1, 0] — cosine sim: col-a=[1,0]→1.0, col-b=[0,1]→0.0, col-c=[0.7,0.7]→~0.7
    collections = [
        _meta("col-b", centroid=[0.0, 1.0]),
        _meta("col-c", centroid=[0.7, 0.7]),
        _meta("col-a", centroid=[1.0, 0.0]),
    ]
    result = router.rank([1.0, 0.0], collections)
    assert [m.name for m in result] == ["col-a", "col-c", "col-b"]


def test_rank_confidence_gate_returns_empty() -> None:
    router = _router(shortlist_size=5, confidence_threshold=0.9)
    # max similarity will be ~0.707 < 0.9 → empty
    collections = [
        _meta("col-a", centroid=[0.7, 0.7]),
        _meta("col-b", centroid=[0.6, 0.8]),
    ]
    result = router.rank([1.0, 0.0], collections)
    assert result == []


def test_rank_none_centroid_placed_last() -> None:
    router = _router(shortlist_size=5, confidence_threshold=0.0)
    collections = [
        _meta("no-centroid", centroid=None),
        _meta("col-a", centroid=[1.0, 0.0]),
    ]
    result = router.rank([1.0, 0.0], collections)
    assert result[0].name == "col-a"
    assert result[1].name == "no-centroid"


def test_rank_shortlist_size_cap() -> None:
    router = _router(shortlist_size=2, confidence_threshold=0.0)
    collections = [
        _meta("col-a", centroid=[1.0, 0.0]),
        _meta("col-b", centroid=[0.9, 0.1]),
        _meta("col-c", centroid=[0.8, 0.2]),
    ]
    result = router.rank([1.0, 0.0], collections)
    assert len(result) == 2


def test_rank_skips_collections_with_mismatched_embedding_model() -> None:
    router = _router(shortlist_size=5, confidence_threshold=0.0, embedding_model="model-a")
    collections = [
        _meta("col-good", centroid=[1.0, 0.0], embedding_model="model-a"),
        _meta("col-wrong-model", centroid=[1.0, 0.0], embedding_model="model-b"),
    ]
    result = router.rank([1.0, 0.0], collections)
    # col-good should come first (valid centroid), col-wrong-model last (treated as no centroid)
    assert result[0].name == "col-good"
    assert result[1].name == "col-wrong-model"


# ---------------------------------------------------------------------------
# fetch_metadata() timeout test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_fetch_metadata_timeout_returns_empty() -> None:
    import httpx

    router = _router()
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

        result = await router.fetch_metadata()

    assert result == []


# ---------------------------------------------------------------------------
# get_pre_context() tier tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_skips_decomposer_searches_all() -> None:
    """≤3 routable collections → returns None, no rank call, _decomposer_was_invoked=False."""
    router = _router(shortlist_size=5)
    routable = [
        _meta("col-a"),
        _meta("col-b"),
    ]
    with patch.object(router, "fetch_metadata", new=AsyncMock(return_value=routable)):
        with patch.object(router, "rank", wraps=router.rank) as mock_rank:
            result = await router.get_pre_context("test query", pinned_names=[], available_slots=3)

    assert result is None
    assert router._decomposer_was_invoked is False
    assert router._last_routable_names == ["col-a", "col-b"]
    mock_rank.assert_not_called()


@pytest.mark.asyncio
async def test_tier2_skips_centroid_preranking() -> None:
    """4–shortlist_size routable collections: rank() NOT called, all included in block."""
    router = _router(shortlist_size=5)
    # 4 routable collections (shortlist_size=5, so 4 falls in Tier 2)
    routable = [_meta(f"col-{i}") for i in range(4)]
    with patch.object(router, "fetch_metadata", new=AsyncMock(return_value=routable)):
        with patch.object(router, "rank") as mock_rank:
            result = await router.get_pre_context("test query", pinned_names=[], available_slots=3)

    assert result is not None
    assert "<search_collections>" in result
    for m in routable:
        assert m.name in result
    assert router._decomposer_was_invoked is True
    assert router._last_routable_names == [m.name for m in routable]
    mock_rank.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_metadata_empty_returns_empty_routable_names() -> None:
    """fetch_metadata returns [] → get_pre_context returns None, _last_routable_names=[]."""
    router = _router()
    with patch.object(router, "fetch_metadata", new=AsyncMock(return_value=[])):
        result = await router.get_pre_context("test query", pinned_names=[], available_slots=3)

    assert result is None
    assert router._last_routable_names == []


# ---------------------------------------------------------------------------
# Tier 3 test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_centroid_preranking_called() -> None:
    """n_routable > shortlist_size: rank() called, _last_routable_names narrowed to shortlist."""
    embedder = _make_embedder(vector=[1.0, 0.0])
    router = _router(shortlist_size=3, embedder=embedder)
    # 4 routable → exceeds shortlist_size=3 → Tier 3
    routable = [
        _meta("col-a", centroid=[1.0, 0.0]),  # most similar to [1,0]
        _meta("col-b", centroid=[0.0, 1.0]),
        _meta("col-c", centroid=[0.5, 0.5]),
        _meta("col-d", centroid=[0.3, 0.7]),
    ]
    with patch.object(router, "fetch_metadata", new=AsyncMock(return_value=routable)):
        result = await router.get_pre_context("test query", pinned_names=[], available_slots=2)

    assert result is not None
    assert "<search_collections>" in result
    assert router._decomposer_was_invoked is True
    # shortlist_size=3, so only top 3 by similarity should be in _last_routable_names
    assert len(router._last_routable_names) <= 3
    # col-a should be first (most similar to [1,0])
    assert router._last_routable_names[0] == "col-a"
    # verify embedder was called (Tier 3 needs embedding)
    embedder.embed_one.assert_awaited_once_with("test query")


# ---------------------------------------------------------------------------
# select() test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_embeds_and_ranks() -> None:
    """select() embeds query, fetches metadata, ranks, and returns shortlist."""
    embedder = _make_embedder(vector=[1.0, 0.0])
    router = _router(shortlist_size=5, confidence_threshold=0.0, embedder=embedder)
    collections = [
        _meta("col-a", centroid=[1.0, 0.0]),
        _meta("col-b", centroid=[0.0, 1.0]),
    ]
    with patch.object(router, "fetch_metadata", new=AsyncMock(return_value=collections)):
        result = await router.select("test query")

    embedder.embed_one.assert_awaited_once_with("test query")
    assert len(result) == 2
    assert result[0].name == "col-a"


# ---------------------------------------------------------------------------
# Coverage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_metadata_cached_on_second_call() -> None:
    """fetch_metadata() is cached — HTTP called only once."""
    router = _router()

    # Manually set the cache to simulate a successful first fetch
    router._cached_metadata = [_meta("col-a")]

    # Second call should return cache without HTTP
    with patch("httpx.AsyncClient") as mock_client_cls:
        result = await router.fetch_metadata()
        mock_client_cls.assert_not_called()

    assert len(result) == 1
    assert result[0].name == "col-a"


@pytest.mark.asyncio
async def test_get_pre_context_splits_pinned_from_routable() -> None:
    """Pinned collections are excluded from routable_meta and the block."""
    router = _router(shortlist_size=5)
    all_meta = [
        _meta("pinned-col"),
        _meta("routable-a"),
        _meta("routable-b"),
        _meta("routable-c"),
        _meta("routable-d"),  # 4 routable → Tier 2
    ]
    with patch.object(router, "fetch_metadata", new=AsyncMock(return_value=all_meta)):
        result = await router.get_pre_context("query", pinned_names=["pinned-col"], available_slots=3)

    # 4 routable (Tier 2) → block returned
    assert result is not None
    assert "pinned-col" not in result
    assert "routable-a" in result
    assert router._last_routable_names == ["routable-a", "routable-b", "routable-c", "routable-d"]


@pytest.mark.asyncio
async def test_get_pre_context_slot_exhaustion_returns_none() -> None:
    """available_slots <= 0 → slot exhaustion shortcut, returns None."""
    router = _router(shortlist_size=5)
    routable = [_meta(f"col-{i}") for i in range(4)]
    with patch.object(router, "fetch_metadata", new=AsyncMock(return_value=routable)):
        result = await router.get_pre_context("query", pinned_names=[], available_slots=0)

    assert result is None
    assert router._decomposer_was_invoked is False


def test_rank_all_none_centroid_bypasses_confidence_gate() -> None:
    """All-None-centroid collections bypass the confidence gate — all returned up to shortlist_size."""
    router = _router(shortlist_size=5, confidence_threshold=0.99)  # high threshold
    collections = [
        _meta("col-a", centroid=None),
        _meta("col-b", centroid=None),
        _meta("col-c", centroid=None),
    ]
    result = router.rank([1.0, 0.0], collections)
    # Gate bypassed — all 3 returned
    assert len(result) == 3


# ---------------------------------------------------------------------------
# invalidate() / initial_metadata tests (Task 2.1)
# ---------------------------------------------------------------------------


def test_invalidate_clears_cached_metadata() -> None:
    """invalidate() resets a populated cache back to None."""
    router = _router()
    router._cached_metadata = [_meta("col-a")]
    router.invalidate()
    assert router._cached_metadata is None


def test_invalidate_is_idempotent() -> None:
    """invalidate() is safe to call repeatedly on an already-empty cache."""
    router = _router()
    assert router._cached_metadata is None
    router.invalidate()
    router.invalidate()
    assert router._cached_metadata is None


@pytest.mark.asyncio
async def test_invalidate_triggers_refetch_with_fresh_data() -> None:
    """After invalidate(), the next fetch_metadata() issues a real HTTP fetch and
    returns fresh post-invalidation data, replacing the stale seeded cache."""
    router = _router(initial_metadata=[_meta("stale-col")])

    # Before-state anchor: the populated cache is served without any HTTP.
    with patch("httpx.AsyncClient") as no_http_cls:
        before = await router.fetch_metadata()
        no_http_cls.assert_not_called()
    assert [m.name for m in before] == ["stale-col"]

    router.invalidate()
    assert router._cached_metadata is None

    # A successful JSON-RPC envelope: list[dict] serialised as one TextContent block.
    fresh_payload = json.dumps([{"name": "fresh-col", "embedding_model": "model-a"}])
    response = MagicMock()
    response.raise_for_status = MagicMock(return_value=None)
    response.json = MagicMock(
        return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": fresh_payload}]},
        }
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=response)

        result = await router.fetch_metadata()

        # (a) an actual HTTP fetch occurred this time
        mock_client_cls.assert_called()
        mock_client.post.assert_awaited_once()

    # (b) fresh data is returned, not the stale seeded data
    assert [m.name for m in result] == ["fresh-col"]
    # (c) the cache now holds the fresh list
    assert router._cached_metadata == result
    assert [m.name for m in router._cached_metadata] == ["fresh-col"]


def test_initial_metadata_populates_cache() -> None:
    """initial_metadata seeds the cache without any fetch."""
    some_meta = _meta("col-a")
    router = _router(initial_metadata=[some_meta])
    assert router._cached_metadata == [some_meta]


def test_initial_metadata_none_leaves_cache_empty() -> None:
    """Omitting initial_metadata (None) leaves the cache as None."""
    router = _router(initial_metadata=None)
    assert router._cached_metadata is None


@pytest.mark.asyncio
async def test_initial_metadata_empty_list_marks_cache_populated() -> None:
    """An empty list is a populated cache (distinct from None): fetch_metadata returns [] without HTTP."""
    router = _router(initial_metadata=[])
    assert router._cached_metadata == []

    with patch("httpx.AsyncClient") as mock_client_cls:
        result = await router.fetch_metadata()
        mock_client_cls.assert_not_called()

    assert result == []


def test_initial_metadata_is_copied() -> None:
    """initial_metadata is defensively copied: mutating the original does not affect the cache."""
    original = [_meta("col-a")]
    router = _router(initial_metadata=original)
    original.append(_meta("col-b"))
    assert router._cached_metadata == [_meta("col-a")]


@pytest.mark.asyncio
async def test_select_uses_initial_metadata_without_http() -> None:
    """select() uses initial_metadata cache without triggering an HTTP fetch."""
    embedder = _make_embedder(vector=[1.0, 0.0])
    router = _router(
        confidence_threshold=0.0,
        embedder=embedder,
        initial_metadata=[_meta("col-a", centroid=[1.0, 0.0])],
    )
    with patch("httpx.AsyncClient") as mock_client_cls:
        result = await router.select("test query")
        mock_client_cls.assert_not_called()

    assert [m.name for m in result] == ["col-a"]
    embedder.embed_one.assert_awaited_once_with("test query")


@pytest.mark.asyncio
async def test_tier3_confidence_gate_failure_returns_none() -> None:
    """Tier 3: rank() returns [] (confidence gate failed) → get_pre_context returns None."""
    embedder = _make_embedder(vector=[1.0, 0.0])
    # shortlist_size=2; need 4 routable (> 3 so NOT Tier 1; > shortlist_size=2 so Tier 3)
    router = _router(shortlist_size=2, confidence_threshold=0.99, embedder=embedder)
    routable = [
        _meta("col-a", centroid=[0.1, 0.9]),  # sim to [1,0] ≈ 0.11
        _meta("col-b", centroid=[0.2, 0.8]),  # sim ≈ 0.24
        _meta("col-c", centroid=[0.3, 0.7]),  # sim ≈ 0.39
        _meta("col-d", centroid=[0.4, 0.6]),  # sim ≈ 0.55 — all below 0.99 threshold
    ]
    with patch.object(router, "fetch_metadata", new=AsyncMock(return_value=routable)):
        result = await router.get_pre_context("query", pinned_names=[], available_slots=2)

    assert result is None
    assert router._decomposer_was_invoked is False
    assert router._last_routable_names == []


# ---------------------------------------------------------------------------
# rank_with_scores() tests
# ---------------------------------------------------------------------------


def test_rank_with_scores_returns_all_collections() -> None:
    """5 collections with valid matching-model centroids → result length == 5."""
    router = _router(shortlist_size=5, confidence_threshold=0.5)
    collections = [
        _meta(f"col-{i}", centroid=[float(i), 1.0]) for i in range(1, 6)
    ]
    result = router.rank_with_scores([1.0, 0.0], collections)
    assert len(result) == 5


def test_rank_with_scores_bypasses_confidence_gate() -> None:
    """confidence_threshold=0.99 → rank() returns []; rank_with_scores() still returns all."""
    router = _router(shortlist_size=5, confidence_threshold=0.99)
    collections = [
        _meta("col-a", centroid=[0.1, 0.9]),
        _meta("col-b", centroid=[0.2, 0.8]),
        _meta("col-c", centroid=[0.3, 0.7]),
    ]
    assert router.rank([1.0, 0.0], collections) == []
    result = router.rank_with_scores([1.0, 0.0], collections)
    assert len(result) == len(collections)
    # Scores should all be non-None and sorted descending
    scores = [s for _, s in result]
    assert all(s is not None for s in scores)
    assert scores == sorted(scores, reverse=True)


def test_rank_with_scores_handles_mismatched_model() -> None:
    """Collection with different embedding_model gets score=None and is placed after scored."""
    router = _router(shortlist_size=5, confidence_threshold=0.0, embedding_model="model-a")
    collections = [
        _meta("col-scored", centroid=[1.0, 0.0], embedding_model="model-a"),
        _meta("col-mismatch", centroid=[1.0, 0.0], embedding_model="model-b"),
    ]
    result = router.rank_with_scores([1.0, 0.0], collections)
    assert len(result) == 2
    names = [m.name for m, _ in result]
    scores = [s for _, s in result]
    assert names[0] == "col-scored"
    assert scores[0] is not None
    assert names[1] == "col-mismatch"
    assert scores[1] is None


def test_rank_with_scores_handles_none_centroid() -> None:
    """Collection with centroid=None gets score=None and is placed last."""
    router = _router(shortlist_size=5, confidence_threshold=0.0)
    collections = [
        _meta("col-scored", centroid=[1.0, 0.0]),
        _meta("col-no-centroid", centroid=None),
    ]
    result = router.rank_with_scores([1.0, 0.0], collections)
    assert len(result) == 2
    names = [m.name for m, _ in result]
    scores = [s for _, s in result]
    assert names[0] == "col-scored"
    assert scores[0] is not None
    assert names[1] == "col-no-centroid"
    assert scores[1] is None


def test_rank_with_scores_alphabetical_tie_break() -> None:
    """Two collections with identical centroids (equal similarity) are ordered by name ascending."""
    router = _router(shortlist_size=5, confidence_threshold=0.0)
    same_centroid = [1.0, 0.0]
    collections = [
        _meta("zeta", centroid=same_centroid),
        _meta("alpha", centroid=same_centroid),
    ]
    result = router.rank_with_scores([1.0, 0.0], collections)
    names = [m.name for m, _ in result]
    assert names == ["alpha", "zeta"]


def test_rank_with_scores_does_not_truncate_to_shortlist() -> None:
    """shortlist_size=2, 5 matching collections → rank_with_scores() returns all 5."""
    router = _router(shortlist_size=2, confidence_threshold=0.0)
    collections = [
        _meta(f"col-{i}", centroid=[float(i), 1.0]) for i in range(1, 6)
    ]
    result = router.rank_with_scores([1.0, 0.0], collections)
    assert len(result) == 5


# ---------------------------------------------------------------------------
# rank() deterministic tie-break + regression tests
# ---------------------------------------------------------------------------


def test_rank_uses_alpha_tie_break() -> None:
    """Two collections with identical similarity are returned in ascending-name order."""
    router = _router(shortlist_size=5, confidence_threshold=0.0)
    same_centroid = [1.0, 0.0]
    collections = [
        _meta("zeta", centroid=same_centroid),
        _meta("alpha", centroid=same_centroid),
    ]
    result = router.rank([1.0, 0.0], collections)
    assert [m.name for m in result] == ["alpha", "zeta"]


def test_rank_preserves_confidence_gate_after_refactor() -> None:
    """All scored collections below threshold → rank() returns []; one above → non-empty."""
    router_strict = _router(shortlist_size=5, confidence_threshold=0.99)
    collections = [
        _meta("col-a", centroid=[0.1, 0.9]),
        _meta("col-b", centroid=[0.2, 0.8]),
    ]
    assert router_strict.rank([1.0, 0.0], collections) == []

    router_lenient = _router(shortlist_size=5, confidence_threshold=0.0)
    result = router_lenient.rank([1.0, 0.0], collections)
    assert len(result) > 0


def test_rank_preserves_shortlist_truncation_after_refactor() -> None:
    """shortlist_size=2, 5 matching collections → rank() returns exactly 2."""
    router = _router(shortlist_size=2, confidence_threshold=0.0)
    collections = [
        _meta(f"col-{i}", centroid=[float(i), 1.0]) for i in range(1, 6)
    ]
    result = router.rank([1.0, 0.0], collections)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Task 2.2 — new coverage tests
# ---------------------------------------------------------------------------


def test_rank_with_scores_none_scored_sorts_after_scored_by_position() -> None:
    """Scored entry beats an unscored entry regardless of alphabetical order.

    'aaa-unscored' (name sorts first) is unscored; 'zzz-scored' (name sorts last)
    is scored — result must be ['zzz-scored', 'aaa-unscored'].
    """
    router = _router(embedding_model="model-a")
    meta_aaa = _meta("aaa-unscored", centroid=None)
    meta_zzz = _meta("zzz-scored", centroid=[1.0, 0.0])
    result = router.rank_with_scores([1.0, 0.0], [meta_aaa, meta_zzz])
    names = [m.name for m, _ in result]
    scores = [s for _, s in result]
    assert names == ["zzz-scored", "aaa-unscored"]
    assert scores[0] is not None
    assert scores[1] is None


def test_rank_with_scores_all_unscored_sorted_by_name() -> None:
    """All unscored collections → result is in ascending name order, all scores None."""
    router = _router(embedding_model="model-a")
    # Pass in reverse-alpha order to confirm sorting is applied
    collections = [
        _meta("z", centroid=None),
        _meta("a", centroid=None),
    ]
    result = router.rank_with_scores([1.0, 0.0], collections)
    names = [m.name for m, _ in result]
    scores = [s for _, s in result]
    assert names == ["a", "z"]
    assert all(s is None for s in scores)


def test_rank_with_scores_orders_by_descending_score_concrete() -> None:
    """Three scored collections with distinct similarities → result name order and strict score order."""
    router = _router(confidence_threshold=0.0)
    # query [1, 0]; cosine sims: high=[1,0]→1.0, mid=[1,1]→~0.707, low=[0.1,1]→~0.0995
    meta_low = _meta("low", centroid=[0.1, 1.0])
    meta_mid = _meta("mid", centroid=[1.0, 1.0])
    meta_high = _meta("high", centroid=[1.0, 0.0])
    # pass in non-sorted order
    result = router.rank_with_scores([1.0, 0.0], [meta_low, meta_mid, meta_high])
    names = [m.name for m, _ in result]
    scores = [s for _, s in result]
    assert names == ["high", "mid", "low"]
    assert scores[0] is not None and scores[1] is not None and scores[2] is not None
    assert scores[0] > scores[1] > scores[2]


def test_rank_and_rank_with_scores_empty_input() -> None:
    """Empty collection list → rank() returns [] and rank_with_scores() returns []."""
    router = _router()
    assert router.rank([1.0, 0.0], []) == []
    assert router.rank_with_scores([1.0, 0.0], []) == []


# ---------------------------------------------------------------------------
# eval/runner.py constructor-injection guard tests (Task 2.2)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task 3.4 — record_stage("route") in _score_collections
# ---------------------------------------------------------------------------


def test_score_collections_records_route_stage() -> None:
    """Bind a recorder, call _score_collections directly; assert 'route' key in timings."""
    from archon_search.observability import bind_stage_recorder

    router = _router()
    metas = [_meta("col-a", centroid=[1.0, 0.0])]
    vec = [1.0, 0.0]

    with bind_stage_recorder() as recorder:
        router._score_collections(vec, metas)

    assert "route" in recorder.stage_timings_ms
    assert recorder.stage_timings_ms["route"] >= 0.0


def test_rank_with_scores_records_route_stage() -> None:
    """Bind a recorder, call rank_with_scores; assert 'route' key in timings (via _score_collections)."""
    from archon_search.observability import bind_stage_recorder

    router = _router()
    metas = [_meta("col-b", centroid=[0.0, 1.0]), _meta("col-a", centroid=[1.0, 0.0])]
    vec = [1.0, 0.0]

    with bind_stage_recorder() as recorder:
        router.rank_with_scores(vec, metas)

    assert "route" in recorder.stage_timings_ms
    assert recorder.stage_timings_ms["route"] >= 0.0


def test_eval_runner_no_direct_cached_metadata_write() -> None:
    """Source-level guard: no code under archon_search/ (excluding router.py)
    assigns to the private _cached_metadata field.

    The router class owns that field; every other caller must populate it via
    the ``initial_metadata`` constructor argument. This is the automated
    regression guard for Task 2.2. Test files are intentionally out of scope
    (tests legitimately poke the private field).
    """
    import re
    from pathlib import Path

    import archon_search

    package_dir = Path(archon_search.__file__).parent
    router_path = (package_dir / "router.py").resolve()
    # Negative lookahead `(?!=)` matches assignment (`_cached_metadata = ...`)
    # but NOT the equality comparison form (`_cached_metadata == ...`), so a
    # legitimate read in a comparison elsewhere won't trip this write guard.
    pattern = re.compile(r"_cached_metadata\s*=(?!=)")

    offenders: list[str] = []
    for py_file in package_dir.rglob("*.py"):
        if py_file.resolve() == router_path:
            continue
        source = py_file.read_text(encoding="utf-8")
        if pattern.search(source):
            offenders.append(str(py_file))

    assert not offenders, (
        "Direct _cached_metadata assignment found outside router.py: "
        f"{offenders}. Use the initial_metadata constructor argument instead."
    )


@pytest.mark.asyncio
async def test_run_router_for_query_uses_initial_metadata() -> None:
    """_run_router_for_query seeds the router via initial_metadata and triggers
    no HTTP fetch when ranking the injected collections."""
    from archon_search.eval.runner import _run_router_for_query

    embedder = _make_embedder(vector=[1.0, 0.0])
    # model_name must match the injected metas' embedding_model; otherwise the
    # router skips centroid scoring and the non-empty assertion is meaningless.
    embedder.model_name = "model-a"

    # `_run_router_for_query` only touches `pipeline._embedder`.
    pipeline = MagicMock()
    pipeline._embedder = embedder

    collection_metas = [
        _meta("col-a", centroid=[1.0, 0.0], embedding_model="model-a"),
        _meta("col-b", centroid=[0.0, 1.0], embedding_model="model-a"),
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        shortlist = await _run_router_for_query(pipeline, "test query", collection_metas)
        mock_client_cls.assert_not_called()

    assert shortlist, "expected a non-empty shortlist from the injected metadata"
    # col-a's centroid aligns with the query vector (sim 1.0); col-b is
    # orthogonal (sim 0.0). With threshold 0.0 both pass, ranked by descending
    # similarity, so col-a must come first.
    assert shortlist == ["col-a", "col-b"]
    embedder.embed_one.assert_awaited_once_with("test query")
