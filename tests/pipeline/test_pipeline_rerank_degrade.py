"""Cold-reranker degrade path for ``SearchPipeline`` — S286/S288.

Two closely-related contracts live here:

* ``warmup_models(reranker_timeout=...)`` — the bounded warm-up whose bool return
  value is what every transport threads through as ``rerank=``.
* ``rerank=False`` on ``search_many`` / ``search`` — the degraded ranking that
  bool selects: no cross-encoder pass, ``reranker_score`` left ``None``, and a
  deterministic ``(-rrf_score, chunk_id)`` ordering.

Both were previously exercised only end-to-end, so a regression in either showed
up as a wire-level symptom several layers away from its cause.
"""
from __future__ import annotations

import time

import pytest

from archon_search.embedder import Embedder
from archon_search.reranker import Reranker, RerankerWarmupTimeout

from .conftest import make_pipeline


class _WarmingEmbedderBackend:
    """Like ``MockEmbedderBackend`` but actually reports warmth after a build.

    ``tests/pipeline/conftest.MockEmbedderBackend`` pins ``is_warm = False``
    forever, which is fine for retrieval tests but makes every warm-up assertion
    vacuous.
    """

    model_name: str = "mock-embedder"

    def __init__(self) -> None:
        self.is_warm = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.is_warm = True
        return [[0.1] * 4 for _ in texts]


class _WarmingRerankerBackend:
    """Reranker backend that reports warmth once its first build has run."""

    def __init__(self) -> None:
        self.is_warm = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.is_warm = True
        return [0.5] * len(pairs)


def _make_warming_pipeline(store):  # type: ignore[no-untyped-def]
    """``make_pipeline`` with backends whose ``is_warm`` tracks reality."""
    pipeline = make_pipeline(store)
    pipeline._global_embedder = Embedder(_WarmingEmbedderBackend())  # type: ignore[arg-type]
    pipeline._reranker = Reranker(_WarmingRerankerBackend())  # type: ignore[arg-type]
    return pipeline


# ---------------------------------------------------------------------------
# C2-T-6 — warmup_models(reranker_timeout=...)
# ---------------------------------------------------------------------------


class _NeverWarmsBackend:
    """Reranker backend whose build outlives any bounded wait."""

    is_warm: bool = False

    def __init__(self) -> None:
        self.calls = 0

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.calls += 1
        time.sleep(2.0)
        return [0.5] * len(pairs)


@pytest.mark.asyncio
async def test_warmup_models_returns_false_when_reranker_stays_cold(connected_store) -> None:
    """A bounded warm-up that expires returns False — the caller must degrade."""
    pipeline = make_pipeline(connected_store)
    backend = _NeverWarmsBackend()
    pipeline._reranker = Reranker(backend)  # type: ignore[arg-type]

    assert await pipeline.warmup_models(reranker_timeout=0.01) is False
    assert pipeline.reranker_is_warm is False


@pytest.mark.asyncio
async def test_warmup_models_returns_true_when_reranker_warms(connected_store) -> None:
    """A warm cross-encoder returns True — the caller keeps full reranking."""
    pipeline = _make_warming_pipeline(connected_store)

    assert await pipeline.warmup_models(reranker_timeout=5.0) is True
    assert pipeline.reranker_is_warm is True


@pytest.mark.asyncio
async def test_warmup_models_swallows_a_raising_reranker(connected_store) -> None:
    """A failing warm-up never propagates — it returns the backend's warmth.

    A search can still answer without the cross-encoder, so a transient build
    failure must degrade the ranking, not fail the request.
    """

    class _RaisingBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            raise RuntimeError("cross-encoder build exploded")

    pipeline = make_pipeline(connected_store)
    pipeline._reranker = Reranker(_RaisingBackend())  # type: ignore[arg-type]

    assert await pipeline.warmup_models(reranker_timeout=5.0) is False


@pytest.mark.asyncio
async def test_warmup_models_swallows_reranker_warmup_timeout(connected_store, monkeypatch) -> None:
    """``RerankerWarmupTimeout`` (the build's own ceiling) is swallowed too."""

    class _TimeoutBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            raise AssertionError("must not be reached")

    async def _raise_timeout() -> None:
        raise RerankerWarmupTimeout("build blew its own ceiling")

    pipeline = make_pipeline(connected_store)
    reranker = Reranker(_TimeoutBackend())  # type: ignore[arg-type]
    monkeypatch.setattr(reranker, "warmup", _raise_timeout)
    pipeline._reranker = reranker

    assert await pipeline.warmup_models(reranker_timeout=5.0) is False


@pytest.mark.asyncio
async def test_warmup_models_warms_the_global_embedder_when_none_is_passed(
    connected_store,
) -> None:
    """C3-P-1 regression guard: ``embedder=None`` must warm the *global* embedder.

    ``search_many`` embeds the query through ``self._global_embedder``, so the
    fan-out call sites pass no embedder. An earlier revision skipped the embedder
    leg entirely in that case, leaving the whole cold ONNX build inside the
    fan-out budget — the exact S184 failure the warm-up exists to prevent.
    """
    pipeline = _make_warming_pipeline(connected_store)
    assert pipeline.embedder_is_warm is False

    await pipeline.warmup_models(reranker_timeout=5.0)

    assert pipeline.embedder_is_warm is True


# ---------------------------------------------------------------------------
# C2-T-5 — rerank=False degrade paths
# ---------------------------------------------------------------------------


class _AssertNeverCalledRerankerBackend:
    """Fails loudly if the cross-encoder runs despite ``rerank=False``."""

    is_warm: bool = True

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        raise AssertionError("rerank=False must skip the cross-encoder entirely")


async def _ingest_corpus(pipeline, collection: str, tmp_path) -> None:
    for i in range(6):
        doc = tmp_path / f"degrade_{i}.md"
        doc.write_text(f"# Doc {i}\n\nsearchable degraded ranking content number {i}.\n" * 6)
        await pipeline.ingest_file(doc, collection, embedder=pipeline._global_embedder)


def _assert_degraded(results) -> None:
    assert results, "the degraded path must still return results"
    assert all(r.reranker_score is None for r in results), (
        "rerank=False must leave reranker_score unset; got "
        f"{[r.reranker_score for r in results]}"
    )
    # With no reranker pass, SearchResult.score carries the fused RRF score.
    keys = [(-r.score, r.chunk_id) for r in results]
    assert keys == sorted(keys), f"degraded results are not RRF-ordered: {keys}"


@pytest.mark.asyncio
async def test_search_many_rerank_false_returns_rrf_ordering(
    connected_store, col_name, tmp_path
) -> None:
    """``search_many(rerank=False)`` skips the cross-encoder and fuses by RRF."""
    pipeline = make_pipeline(connected_store)
    await _ingest_corpus(pipeline, col_name, tmp_path)
    pipeline._reranker = Reranker(_AssertNeverCalledRerankerBackend())  # type: ignore[arg-type]

    result = await pipeline.search_many("searchable degraded", [col_name], rerank=False)

    _assert_degraded(result.results)


@pytest.mark.asyncio
async def test_search_graph_mode_rerank_false_returns_rrf_ordering(
    connected_store, col_name, tmp_path
) -> None:
    """``search(graph_mode=..., rerank=False)`` threads the flag through the graph dispatch."""
    pipeline = make_pipeline(connected_store)
    await _ingest_corpus(pipeline, col_name, tmp_path)
    pipeline._reranker = Reranker(_AssertNeverCalledRerankerBackend())  # type: ignore[arg-type]

    result = await pipeline.search(
        "searchable degraded",
        col_name,
        embedder=pipeline._global_embedder,
        graph_mode="global",
        rerank=False,
    )

    _assert_degraded(result.results)


@pytest.mark.asyncio
async def test_search_rerank_true_still_populates_reranker_score(
    connected_store, col_name, tmp_path
) -> None:
    """Control: with ``rerank=True`` the cross-encoder still runs.

    Without this the degrade assertions above would also pass against a build
    that had simply stopped reranking altogether.
    """
    pipeline = make_pipeline(connected_store)
    await _ingest_corpus(pipeline, col_name, tmp_path)

    result = await pipeline.search(
        "searchable degraded", col_name, embedder=pipeline._global_embedder
    )

    assert result.results
    assert all(r.reranker_score is not None for r in result.results)
