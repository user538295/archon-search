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
    strategy: str = "centroid",
    description_weight: float = 0.3,
) -> MultiCollectionRouter:
    return MultiCollectionRouter(
        search_url="http://localhost:9999",
        embedder=embedder or _make_embedder(),
        shortlist_size=shortlist_size,
        confidence_threshold=confidence_threshold,
        embedding_model=embedding_model,
        initial_metadata=initial_metadata,
        strategy=strategy,  # type: ignore[arg-type]
        description_weight=description_weight,
    )


def _meta(
    name: str,
    centroid: list[float] | None = None,
    active_embedding_model: str = "model-a",
    description: str | None = None,
) -> CollectionMeta:
    return CollectionMeta(
        name=name,
        centroid=centroid,
        active_embedding_model=active_embedding_model,
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
        _meta("col-good", centroid=[1.0, 0.0], active_embedding_model="model-a"),
        _meta("col-wrong-model", centroid=[1.0, 0.0], active_embedding_model="model-b"),
    ]
    result = router.rank([1.0, 0.0], collections)
    # col-good should come first (valid centroid), col-wrong-model last (treated as no centroid)
    assert result[0].name == "col-good"
    assert result[1].name == "col-wrong-model"


def test_router_excludes_mismatched_active_embedding_model() -> None:
    """Collection with active_embedding_model != global model gets score=None (unscored)."""
    router = _router(shortlist_size=5, confidence_threshold=0.0, embedding_model="model-Y")
    collections = [
        _meta("col-match", centroid=[1.0, 0.0], active_embedding_model="model-Y"),
        _meta("col-mismatch", centroid=[1.0, 0.0], active_embedding_model="model-X"),
    ]
    scored = router.rank_with_scores([1.0, 0.0], collections)
    # col-match has a score; col-mismatch has None (unscored = excluded from centroid routing)
    by_name = {m.name: s for m, s in scored}
    assert by_name["col-match"] is not None
    assert by_name["col-mismatch"] is None


def test_router_does_not_default_to_empty_string() -> None:
    """Collection with active_embedding_model='' is treated as unscored (not silently matched)."""
    router = _router(shortlist_size=5, confidence_threshold=0.0, embedding_model="model-Y")
    collections = [
        _meta("col-empty-model", centroid=[1.0, 0.0], active_embedding_model=""),
    ]
    scored = router.rank_with_scores([1.0, 0.0], collections)
    assert scored[0][1] is None, "empty active_embedding_model must not match any real model name"


def test_router_does_not_match_none_active_embedding_model() -> None:
    """Collection with active_embedding_model=None (legacy) is treated as unscored."""
    from archon_search.collection_meta import CollectionMeta

    router = _router(shortlist_size=5, confidence_threshold=0.0, embedding_model="model-Y")
    # Bypass _meta helper to set None explicitly (pre-migration legacy data)
    col = CollectionMeta(name="col-none-model", namespace="default")
    col.active_embedding_model = None  # type: ignore[assignment]
    col.centroid = [1.0, 0.0]
    scored = router.rank_with_scores([1.0, 0.0], [col])
    assert scored[0][1] is None, "None active_embedding_model must not match any real model name"


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
    """≤3 routable collections → returns None, _decomposer_was_invoked=False.

    rank() IS called (S503: confidence gate enforced in all tiers).
    Collections with no centroid bypass the gate, so both are routable.
    """
    embedder = _make_embedder(vector=[1.0, 0.0])
    router = _router(shortlist_size=5, embedder=embedder)
    routable = [
        _meta("col-a"),
        _meta("col-b"),
    ]
    with patch.object(router, "fetch_metadata", new=AsyncMock(return_value=routable)):
        result = await router.get_pre_context("test query", pinned_names=[], available_slots=3)

    assert result is None
    assert router._decomposer_was_invoked is False
    assert router._last_routable_names == ["col-a", "col-b"]
    embedder.embed_one.assert_awaited_once_with("test query")


@pytest.mark.asyncio
async def test_tier2_skips_centroid_preranking() -> None:
    """4–shortlist_size routable collections: all included in block (no centroid → gate bypassed).

    rank() IS called (S503: confidence gate enforced in all tiers).
    Collections with no centroid bypass the gate, so all are routable.
    """
    embedder = _make_embedder(vector=[1.0, 0.0])
    router = _router(shortlist_size=5, embedder=embedder)
    # 4 routable collections (shortlist_size=5, so 4 falls in Tier 2)
    routable = [_meta(f"col-{i}") for i in range(4)]
    with patch.object(router, "fetch_metadata", new=AsyncMock(return_value=routable)):
        result = await router.get_pre_context("test query", pinned_names=[], available_slots=3)

    assert result is not None
    assert "<search_collections>" in result
    for m in routable:
        assert m.name in result
    assert router._decomposer_was_invoked is True
    assert router._last_routable_names == [m.name for m in routable]
    embedder.embed_one.assert_awaited_once_with("test query")


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
    fresh_payload = json.dumps([{"name": "fresh-col", "active_embedding_model": "model-a"}])
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


@pytest.mark.asyncio
async def test_tier1_confidence_gate_filters_routable_names() -> None:
    """S503: <=3 routable with all scores below threshold → _last_routable_names=[]."""
    embedder = _make_embedder(vector=[1.0, 0.0])
    router = _router(shortlist_size=5, confidence_threshold=1.0, embedder=embedder)
    routable = [
        _meta("s503_a", centroid=[0.5, 0.5]),  # sim ~0.707 < 1.0
        _meta("s503_b", centroid=[0.3, 0.7]),  # sim ~0.39 < 1.0
    ]
    with patch.object(router, "fetch_metadata", new=AsyncMock(return_value=routable)):
        result = await router.get_pre_context("query", pinned_names=[], available_slots=3)

    assert result is None
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
        _meta("col-scored", centroid=[1.0, 0.0], active_embedding_model="model-a"),
        _meta("col-mismatch", centroid=[1.0, 0.0], active_embedding_model="model-b"),
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

    # `_run_router_for_query` only touches `pipeline._global_embedder`.
    pipeline = MagicMock()
    pipeline._global_embedder = embedder

    collection_metas = [
        _meta("col-a", centroid=[1.0, 0.0], active_embedding_model="model-a"),
        _meta("col-b", centroid=[0.0, 1.0], active_embedding_model="model-a"),
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


# ---------------------------------------------------------------------------
# Task 4.1 — description_embedding in _ROUTING_FIELDS and fetch_metadata
# ---------------------------------------------------------------------------


def test_routing_fields_includes_description_embedding() -> None:
    """_ROUTING_FIELDS must contain 'description_embedding' so it is passed to CollectionMeta."""
    from archon_search.router import _ROUTING_FIELDS

    assert "description_embedding" in _ROUTING_FIELDS


@pytest.mark.asyncio
async def test_fetch_metadata_deserializes_description_embedding() -> None:
    """fetch_metadata returns CollectionMeta with description_embedding populated from response."""
    router = _router()

    payload = json.dumps([
        {"name": "col-a", "active_embedding_model": "model-a", "description_embedding": [0.1, 0.2]}
    ])
    response = MagicMock()
    response.raise_for_status = MagicMock(return_value=None)
    response.json = MagicMock(
        return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": payload}]},
        }
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=response)

        result = await router.fetch_metadata()

    assert len(result) == 1
    assert result[0].description_embedding == [0.1, 0.2]


@pytest.mark.asyncio
async def test_fetch_metadata_missing_description_embedding_yields_none() -> None:
    """fetch_metadata silently yields description_embedding=None when field is absent in response."""
    router = _router()

    payload = json.dumps([{"name": "col-a", "active_embedding_model": "model-a"}])
    response = MagicMock()
    response.raise_for_status = MagicMock(return_value=None)
    response.json = MagicMock(
        return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": payload}]},
        }
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=response)

        result = await router.fetch_metadata()

    assert len(result) == 1
    assert result[0].description_embedding is None


@pytest.mark.asyncio
async def test_fetch_metadata_passes_include_flag_under_hybrid() -> None:
    """When _strategy == 'hybrid', fetch_metadata sends include_description_embedding: True."""
    router = _router()
    router._strategy = "hybrid"  # Task 4.2 will add this via __init__; manually set for now

    captured_payloads: list[dict] = []

    async def _capture_post(url: str, **kwargs: object) -> MagicMock:
        captured_payloads.append(kwargs.get("json", {}))  # type: ignore[arg-type]
        payload = json.dumps([{"name": "col-a", "active_embedding_model": "model-a"}])
        resp = MagicMock()
        resp.raise_for_status = MagicMock(return_value=None)
        resp.json = MagicMock(
            return_value={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": payload}]},
            }
        )
        return resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=_capture_post)

        await router.fetch_metadata()

    assert len(captured_payloads) == 1
    assert captured_payloads[0]["params"]["arguments"] == {"include_description_embedding": True}


@pytest.mark.asyncio
async def test_fetch_metadata_omits_include_flag_under_centroid() -> None:
    """Without _strategy set (default centroid), fetch_metadata sends empty arguments dict."""
    router = _router(strategy="centroid")  # explicit centroid strategy

    captured_payloads: list[dict] = []

    async def _capture_post(url: str, **kwargs: object) -> MagicMock:
        captured_payloads.append(kwargs.get("json", {}))  # type: ignore[arg-type]
        payload = json.dumps([{"name": "col-a", "active_embedding_model": "model-a"}])
        resp = MagicMock()
        resp.raise_for_status = MagicMock(return_value=None)
        resp.json = MagicMock(
            return_value={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": payload}]},
            }
        )
        return resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=_capture_post)

        await router.fetch_metadata()

    assert len(captured_payloads) == 1
    assert captured_payloads[0]["params"]["arguments"] == {}


# ---------------------------------------------------------------------------
# Task 4.2 — hybrid routing strategy
# ---------------------------------------------------------------------------


def _meta_with_desc_emb(
    name: str,
    centroid: list[float] | None = None,
    description_embedding: list[float] | None = None,
    active_embedding_model: str = "model-a",
) -> CollectionMeta:
    return CollectionMeta(
        name=name,
        centroid=centroid,
        active_embedding_model=active_embedding_model,
        description_embedding=description_embedding,
    )


def test_centroid_strategy_identical_to_pre_b4() -> None:
    """Centroid strategy returns exact cosine scores, ignoring description_embedding."""
    import math

    router = _router(strategy="centroid", confidence_threshold=0.0)
    q = [1.0, 0.0, 0.0]
    # c1 aligns with query → cosine 1.0; c2 = [0.6,0.8,0] → cosine 0.6
    c1 = [1.0, 0.0, 0.0]
    c2 = [0.6, 0.8, 0.0]
    # description_embeddings point away from query; should be ignored
    d1 = [0.0, 0.0, 1.0]
    d2 = [0.0, 1.0, 0.0]
    collections = [
        _meta_with_desc_emb("col-a", centroid=c1, description_embedding=d1),
        _meta_with_desc_emb("col-b", centroid=c2, description_embedding=d2),
    ]
    result = router._score_collections(q, collections)
    # Both scored; sorted descending
    names = [m.name for m, _ in result]
    scores = [s for _, s in result]
    assert names == ["col-a", "col-b"]
    assert scores[0] is not None and abs(scores[0] - 1.0) < 1e-9
    expected_b = 0.6 / math.sqrt(0.6**2 + 0.8**2)
    assert scores[1] is not None and abs(scores[1] - expected_b) < 1e-9


def test_hybrid_outranks_tight_off_topic_centroid() -> None:
    """Hybrid: collection A with better description alignment beats B with better centroid."""
    # query aligned with [1,0]
    # col-a: diffuse centroid (low cosine to query) but description aligns well
    # col-b: tight centroid scores higher alone
    q = [1.0, 0.0]
    col_a = _meta_with_desc_emb("col-a", centroid=[0.0, 1.0], description_embedding=[1.0, 0.0])
    col_b = _meta_with_desc_emb("col-b", centroid=[1.0, 0.0], description_embedding=[0.0, 1.0])
    # With w=0.8 blend:
    # col-a: 0.2 * cos(q,c_a) + 0.8 * cos(q,d_a) = 0.2*0.0 + 0.8*1.0 = 0.8
    # col-b: 0.2 * cos(q,c_b) + 0.8 * cos(q,d_b) = 0.2*1.0 + 0.8*0.0 = 0.2
    router = _router(strategy="hybrid", description_weight=0.8, confidence_threshold=0.0)
    result = router._score_collections(q, [col_a, col_b])
    names = [m.name for m, _ in result]
    scores = [s for _, s in result]
    assert names[0] == "col-a", f"Expected col-a first but got {names}"
    assert scores[0] is not None and scores[0] > scores[1]  # type: ignore[operator]


def test_per_collection_centroid_fallback() -> None:
    """Hybrid: collection with description_embedding=None uses centroid-only score, not unscored."""
    q = [1.0, 0.0]
    col_a = _meta_with_desc_emb("col-a", centroid=[1.0, 0.0], description_embedding=[0.5, 0.5])
    col_b = _meta_with_desc_emb("col-b", centroid=[0.8, 0.6], description_embedding=None)
    router = _router(strategy="hybrid", confidence_threshold=0.0)
    result = router._score_collections(q, [col_a, col_b])
    # col-b has no description_embedding but still gets a centroid score (not None)
    score_by_name = {m.name: s for m, s in result}
    assert score_by_name["col-b"] is not None, "col-b should be scored via centroid fallback"


def test_hybrid_all_description_embeddings_none_degrades_to_centroid() -> None:
    """Hybrid with all description_embedding=None → scores equal centroid scores exactly."""
    q = [1.0, 0.0]
    c_a = [1.0, 0.0]
    c_b = [0.6, 0.8]
    col_a = _meta_with_desc_emb("col-a", centroid=c_a, description_embedding=None)
    col_b = _meta_with_desc_emb("col-b", centroid=c_b, description_embedding=None)

    router_centroid = _router(strategy="centroid", confidence_threshold=0.0)
    router_hybrid = _router(strategy="hybrid", confidence_threshold=0.0)

    centroid_result = router_centroid._score_collections(q, [col_a, col_b])
    hybrid_result = router_hybrid._score_collections(q, [col_a, col_b])

    centroid_scores = {m.name: s for m, s in centroid_result}
    hybrid_scores = {m.name: s for m, s in hybrid_result}

    assert centroid_scores == hybrid_scores
    names_centroid = [m.name for m, _ in centroid_result]
    names_hybrid = [m.name for m, _ in hybrid_result]
    assert names_centroid == names_hybrid


def test_hybrid_dimensionality_mismatch_falls_back_to_centroid() -> None:
    """Hybrid: description_embedding with wrong dimensionality → centroid-only score, no exception."""
    q = [1.0, 0.0]
    col_a = _meta_with_desc_emb("col-a", centroid=[1.0, 0.0], description_embedding=[0.5, 0.5, 0.7])
    router = _router(strategy="hybrid", confidence_threshold=0.0)
    result = router._score_collections(q, [col_a])
    assert len(result) == 1
    score = result[0][1]
    assert score is not None
    # centroid cosine of [1,0] vs [1,0] = 1.0
    assert abs(score - 1.0) < 1e-9


def test_hybrid_zero_norm_description_embedding_falls_back_to_centroid() -> None:
    """Hybrid: description_embedding = all zeros → centroid-only score, no exception."""
    q = [1.0, 0.0]
    col_a = _meta_with_desc_emb("col-a", centroid=[1.0, 0.0], description_embedding=[0.0, 0.0])
    router = _router(strategy="hybrid", confidence_threshold=0.0)
    result = router._score_collections(q, [col_a])
    score = result[0][1]
    assert score is not None
    assert abs(score - 1.0) < 1e-9


def test_model_mismatch_description_embedding_ignored() -> None:
    """Collection with description_embedding but mismatched model → goes to unscored tail."""
    q = [1.0, 0.0]
    col_mismatch = _meta_with_desc_emb(
        "col-mismatch",
        centroid=[1.0, 0.0],
        description_embedding=[1.0, 0.0],
        active_embedding_model="wrong-model",
    )
    router = _router(strategy="hybrid", embedding_model="model-a", confidence_threshold=0.0)
    result = router._score_collections(q, [col_mismatch])
    assert len(result) == 1
    assert result[0][1] is None  # unscored because model mismatch


def test_empty_embedding_model_remains_unscored_under_hybrid() -> None:
    """Collection with active_embedding_model='' and description_embedding set → unscored tail."""
    q = [1.0, 0.0]
    col_empty_model = _meta_with_desc_emb(
        "col-empty",
        centroid=[1.0, 0.0],
        description_embedding=[1.0, 0.0],
        active_embedding_model="",
    )
    router = _router(strategy="hybrid", embedding_model="model-a", confidence_threshold=0.0)
    result = router._score_collections(q, [col_empty_model])
    assert result[0][1] is None  # unscored


def test_weight_zero_equals_centroid() -> None:
    """description_weight=0.0 → hybrid scores identical to centroid strategy."""
    q = [1.0, 0.0]
    col_a = _meta_with_desc_emb("col-a", centroid=[1.0, 0.0], description_embedding=[0.0, 1.0])
    col_b = _meta_with_desc_emb("col-b", centroid=[0.6, 0.8], description_embedding=[1.0, 0.0])

    router_centroid = _router(strategy="centroid", confidence_threshold=0.0)
    router_hybrid_w0 = _router(strategy="hybrid", description_weight=0.0, confidence_threshold=0.0)

    centroid_result = {m.name: s for m, s in router_centroid._score_collections(q, [col_a, col_b])}
    hybrid_result = {m.name: s for m, s in router_hybrid_w0._score_collections(q, [col_a, col_b])}

    for name in centroid_result:
        assert centroid_result[name] is not None
        assert hybrid_result[name] is not None
        assert abs(centroid_result[name] - hybrid_result[name]) < 1e-9  # type: ignore[operator]


def test_weight_one_pure_description_embedding() -> None:
    """description_weight=1.0 → score = cos(q, description_embedding) when present."""
    import math

    q = [1.0, 0.0]
    desc = [0.6, 0.8]
    col_a = _meta_with_desc_emb("col-a", centroid=[1.0, 0.0], description_embedding=desc)
    router = _router(strategy="hybrid", description_weight=1.0, confidence_threshold=0.0)
    result = router._score_collections(q, [col_a])
    score = result[0][1]
    expected = 0.6 / math.sqrt(0.6**2 + 0.8**2)  # cos([1,0], [0.6,0.8])
    assert score is not None and abs(score - expected) < 1e-9


def test_confidence_gate_uses_blended_score() -> None:
    """Confidence gate fires based on blended max score when using hybrid strategy."""
    q = [1.0, 0.0]
    # centroid barely above 0.0 threshold alone, but blend with description should pass threshold
    # col-a: centroid cosine = 0.1 (low), description cosine = 1.0
    # w=0.9: 0.1*0.1 + 0.9*1.0 = 0.91 → above threshold 0.5
    col_a = _meta_with_desc_emb("col-a", centroid=[0.1, 0.995], description_embedding=[1.0, 0.0])
    router = _router(strategy="hybrid", description_weight=0.9, confidence_threshold=0.5)
    result = router.rank(q, [col_a])
    assert len(result) == 1  # blended score passes the gate


def test_hybrid_gate_fires_when_all_description_embeddings_none_and_centroids_below_threshold() -> None:
    """Hybrid, all description_embedding=None, all centroid cosines below threshold → rank returns []."""
    q = [1.0, 0.0]
    col_a = _meta_with_desc_emb("col-a", centroid=[0.0, 1.0], description_embedding=None)
    col_b = _meta_with_desc_emb("col-b", centroid=[0.1, 0.995], description_embedding=None)
    router = _router(strategy="hybrid", confidence_threshold=0.5)
    result = router.rank(q, [col_a, col_b])
    # Both fall back to centroid; both below threshold 0.5
    assert result == []


def test_hybrid_does_not_spuriously_bypass_at_default_threshold() -> None:
    """Hybrid with threshold=0.30, centroid max ≥ 0.30 → hybrid max also ≥ 0.30."""
    q = [1.0, 0.0]
    # centroid cosine = 0.8 → well above 0.30; blend should also be above 0.30
    col_a = _meta_with_desc_emb("col-a", centroid=[0.8, 0.6], description_embedding=[0.5, 0.5])
    router = _router(strategy="hybrid", confidence_threshold=0.30)
    result = router.rank(q, [col_a])
    assert len(result) == 1  # not spuriously bypassed
