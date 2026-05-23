"""Tests for MultiCollectionRouter ."""
from __future__ import annotations

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
) -> MultiCollectionRouter:
    return MultiCollectionRouter(
        search_url="http://localhost:9999",
        embedder=embedder or _make_embedder(),
        shortlist_size=shortlist_size,
        confidence_threshold=confidence_threshold,
        embedding_model=embedding_model,
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
# rank_with_scores() tests (Task 2.2)
# ---------------------------------------------------------------------------


def test_rank_with_scores_returns_all_collections() -> None:
    router = _router(shortlist_size=2, confidence_threshold=0.9)
    collections = [
        _meta("col-a", centroid=[1.0, 0.0]),
        _meta("col-b", centroid=[0.0, 1.0]),
        _meta("col-c", centroid=[0.7, 0.7]),
    ]
    # rank() would cap at shortlist_size=2; rank_with_scores must return all 3
    result = router.rank_with_scores([1.0, 0.0], collections)
    assert len(result) == 3


def test_rank_with_scores_bypasses_confidence_gate() -> None:
    router = _router(shortlist_size=5, confidence_threshold=0.99)
    collections = [
        _meta("col-a", centroid=[0.7, 0.7]),  # sim ≈ 0.707 < 0.99
        _meta("col-b", centroid=[0.6, 0.8]),
    ]
    # rank() would return [] due to gate; rank_with_scores must still return entries
    result = router.rank_with_scores([1.0, 0.0], collections)
    assert len(result) == 2
    assert all(score is not None for _, score in result)


def test_rank_with_scores_handles_mismatched_model() -> None:
    router = _router(shortlist_size=5, confidence_threshold=0.0, embedding_model="model-a")
    collections = [
        _meta("col-good", centroid=[1.0, 0.0], embedding_model="model-a"),
        _meta("col-wrong", centroid=[1.0, 0.0], embedding_model="model-b"),
    ]
    result = router.rank_with_scores([1.0, 0.0], collections)
    assert len(result) == 2
    # col-good scored; col-wrong unscored (None)
    names = [m.name for m, _ in result]
    scores = [s for _, s in result]
    assert names[0] == "col-good"
    assert scores[0] is not None
    assert names[1] == "col-wrong"
    assert scores[1] is None


def test_rank_with_scores_handles_none_centroid() -> None:
    router = _router(shortlist_size=5, confidence_threshold=0.0)
    collections = [
        _meta("col-a", centroid=[1.0, 0.0]),
        _meta("no-centroid", centroid=None),
    ]
    result = router.rank_with_scores([1.0, 0.0], collections)
    assert len(result) == 2
    assert result[0][0].name == "col-a"
    assert result[0][1] is not None
    assert result[1][0].name == "no-centroid"
    assert result[1][1] is None


def test_rank_with_scores_alphabetical_tie_break() -> None:
    """When two collections have identical scores, alphabetical ordering applies."""
    router = _router(shortlist_size=5, confidence_threshold=0.0)
    # Identical centroids → identical cosine similarity
    collections = [
        _meta("col-z", centroid=[1.0, 0.0]),
        _meta("col-a", centroid=[1.0, 0.0]),
        _meta("col-m", centroid=[1.0, 0.0]),
    ]
    result = router.rank_with_scores([1.0, 0.0], collections)
    names = [m.name for m, _ in result]
    assert names == ["col-a", "col-m", "col-z"]


def test_rank_with_scores_does_not_truncate_to_shortlist() -> None:
    router = _router(shortlist_size=1, confidence_threshold=0.0)
    collections = [
        _meta("col-a", centroid=[1.0, 0.0]),
        _meta("col-b", centroid=[0.0, 1.0]),
        _meta("col-c", centroid=[0.5, 0.5]),
    ]
    # rank() would cap at 1; rank_with_scores must return all 3
    result = router.rank_with_scores([1.0, 0.0], collections)
    assert len(result) == 3


def test_rank_uses_alpha_tie_break() -> None:
    """rank() must also use alphabetical tie-break (inherited from _score_collections)."""
    router = _router(shortlist_size=10, confidence_threshold=0.0)
    collections = [
        _meta("col-z", centroid=[1.0, 0.0]),
        _meta("col-a", centroid=[1.0, 0.0]),
    ]
    result = router.rank([1.0, 0.0], collections)
    assert result[0].name == "col-a"
    assert result[1].name == "col-z"


def test_rank_preserves_confidence_gate_after_refactor() -> None:
    router = _router(shortlist_size=5, confidence_threshold=0.9)
    collections = [
        _meta("col-a", centroid=[0.7, 0.7]),  # sim ≈ 0.707 < 0.9
    ]
    result = router.rank([1.0, 0.0], collections)
    assert result == []


def test_rank_preserves_shortlist_truncation_after_refactor() -> None:
    router = _router(shortlist_size=2, confidence_threshold=0.0)
    collections = [
        _meta("col-a", centroid=[1.0, 0.0]),
        _meta("col-b", centroid=[0.9, 0.1]),
        _meta("col-c", centroid=[0.8, 0.2]),
    ]
    result = router.rank([1.0, 0.0], collections)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Task 2.1 — invalidate() and initial_metadata tests
# ---------------------------------------------------------------------------


def test_invalidate_clears_cached_metadata() -> None:
    """invalidate() sets _cached_metadata to None."""
    router = _router()
    router._cached_metadata = [_meta("col-a")]
    router.invalidate()
    assert router._cached_metadata is None


def test_invalidate_is_idempotent() -> None:
    """Calling invalidate() twice when cache is already None must not raise."""
    router = _router()
    router._cached_metadata = None
    router.invalidate()  # first call — no-op
    router.invalidate()  # second call — still no-op
    assert router._cached_metadata is None


def test_initial_metadata_populates_cache() -> None:
    """Constructor with initial_metadata sets _cached_metadata without fetch."""
    meta = [_meta("col-a"), _meta("col-b")]
    router = MultiCollectionRouter(
        search_url="http://localhost:9999",
        embedder=_make_embedder(),
        shortlist_size=5,
        confidence_threshold=0.5,
        embedding_model="model-a",
        initial_metadata=meta,
    )
    assert router._cached_metadata is not None
    assert len(router._cached_metadata) == 2
    assert router._cached_metadata[0].name == "col-a"


def test_initial_metadata_none_leaves_cache_empty() -> None:
    """Constructor with initial_metadata=None leaves _cached_metadata as None."""
    router = MultiCollectionRouter(
        search_url="http://localhost:9999",
        embedder=_make_embedder(),
        shortlist_size=5,
        confidence_threshold=0.5,
        embedding_model="model-a",
        initial_metadata=None,
    )
    assert router._cached_metadata is None


def test_initial_metadata_empty_list_marks_cache_populated() -> None:
    """initial_metadata=[] sets cache to [] (distinct from None — cache is populated)."""
    router = MultiCollectionRouter(
        search_url="http://localhost:9999",
        embedder=_make_embedder(),
        shortlist_size=5,
        confidence_threshold=0.5,
        embedding_model="model-a",
        initial_metadata=[],
    )
    assert router._cached_metadata == []
    assert router._cached_metadata is not None


def test_initial_metadata_is_copied() -> None:
    """Mutating the original list after construction must not affect _cached_metadata."""
    original = [_meta("col-a")]
    router = MultiCollectionRouter(
        search_url="http://localhost:9999",
        embedder=_make_embedder(),
        shortlist_size=5,
        confidence_threshold=0.5,
        embedding_model="model-a",
        initial_metadata=original,
    )
    original.append(_meta("col-b"))
    assert len(router._cached_metadata) == 1


@pytest.mark.asyncio
async def test_select_uses_initial_metadata_without_http() -> None:
    """Router with initial_metadata uses cache without making any HTTP call."""
    embedder = _make_embedder(vector=[1.0, 0.0])
    meta = [_meta("col-a", centroid=[1.0, 0.0])]
    router = MultiCollectionRouter(
        search_url="http://localhost:9999",
        embedder=embedder,
        shortlist_size=5,
        confidence_threshold=0.0,
        embedding_model="model-a",
        initial_metadata=meta,
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        result = await router.select("test query")
        mock_client_cls.assert_not_called()

    assert len(result) == 1
    assert result[0].name == "col-a"


# ---------------------------------------------------------------------------
# Task 2.2 — No direct _cached_metadata write outside router.py
# ---------------------------------------------------------------------------


def test_eval_runner_no_direct_cached_metadata_write() -> None:
    """No file in archon_search/ (except router.py) may directly assign to _cached_metadata."""
    import re
    from pathlib import Path

    archon_search = Path(__file__).parent.parent / "archon_search"
    pattern = re.compile(r'_cached_metadata\s*=')

    violations = []
    for py_file in archon_search.rglob("*.py"):
        if py_file.name == "router.py":
            continue
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                violations.append(f"{py_file.relative_to(archon_search)}:{lineno}: {line.strip()}")

    assert not violations, (
        "Direct _cached_metadata assignment found outside router.py:\n"
        + "\n".join(violations)
    )
