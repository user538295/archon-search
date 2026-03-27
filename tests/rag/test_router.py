"""Tests for MultiCollectionRouter (FEAT-022 Task 2.1)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon.rag.collection_meta import CollectionMeta
from archon.rag.router import MultiCollectionRouter


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
        rag_url="http://localhost:9999",
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
    assert "<rag_collections>" in result
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
