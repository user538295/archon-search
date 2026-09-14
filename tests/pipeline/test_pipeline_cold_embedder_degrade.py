"""Cold-embedder degrade path for ``SearchPipeline`` — S290.

The reranker equivalent lives in ``test_pipeline_rerank_degrade.py`` (S286/S288).
Two contracts here:

* ``warmup_models(embedder_timeout=...)`` — the embedder leg is bounded too, so a
  cold ONNX build cannot hold a request open past a client's read timeout.
* ``fts_only=True`` on ``search`` / ``search_many`` — the degraded ranking a caller
  selects when that bounded wait expires: the query is never embedded and the
  results come from the FTS leg alone.
"""
from __future__ import annotations

import logging
import time

import pytest

from archon_search.embedder import Embedder, EmbedderWarmupTimeout

from .conftest import make_pipeline


class _NeverWarmsEmbedderBackend:
    """Embedder backend whose ONNX build outlives any bounded wait."""

    model_name: str = "never-warms-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        time.sleep(2.0)
        return [[0.1] * 4 for _ in texts]


class _AssertNeverCalledEmbedderBackend:
    """Fails loudly if the query is embedded despite ``fts_only=True``."""

    # Same model name as ``MockEmbedderBackend``: ``search_many`` partitions
    # collections by embedding model and would otherwise exclude the corpus.
    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("fts_only=True must skip embedding entirely")


async def _ingest_corpus(pipeline, collection: str, tmp_path) -> None:
    for i in range(3):
        doc = tmp_path / f"cold_{i}.md"
        doc.write_text(f"# Doc {i}\n\nsearchable degraded ranking content number {i}.\n" * 6)
        await pipeline.ingest_file(doc, collection, embedder=pipeline._global_embedder)


# ---------------------------------------------------------------------------
# warmup_models(embedder_timeout=...)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warmup_models_gives_up_on_a_cold_embedder_within_its_bound(
    connected_store,
) -> None:
    """The bounded embedder wait expires quickly instead of parking the caller."""
    pipeline = make_pipeline(connected_store)
    pipeline._global_embedder = Embedder(_NeverWarmsEmbedderBackend())  # type: ignore[arg-type]
    pipeline._reranker = None

    started = time.monotonic()
    await pipeline.warmup_models(embedder_timeout=0.01)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"the embedder leg was not bounded: waited {elapsed:.2f}s"
    assert pipeline.embedder_is_warm is False


@pytest.mark.asyncio
async def test_warmup_models_without_an_embedder_timeout_stays_unbounded(
    connected_store,
) -> None:
    """The lifespan/ingest callers pass no bound and still get a fully warm model."""
    pipeline = make_pipeline(connected_store)
    pipeline._reranker = None

    await pipeline.warmup_models()

    # The warm-up ran to completion: the dimension is only cached by a real embed.
    assert pipeline._global_embedder.embedding_dim == 4


@pytest.mark.asyncio
async def test_warmup_models_reports_a_caller_give_up_distinctly(
    connected_store, caplog
) -> None:
    """The bounded wait expiring is *this caller* giving up — the build runs on."""
    pipeline = make_pipeline(connected_store)
    pipeline._global_embedder = Embedder(_NeverWarmsEmbedderBackend())  # type: ignore[arg-type]
    pipeline._reranker = None

    with caplog.at_level(logging.WARNING, logger="archon_search.pipeline"):
        await pipeline.warmup_models(embedder_timeout=0.01)

    assert "still cold after waiting 0.01s" in caplog.text
    assert "the build continues in the background" in caplog.text


@pytest.mark.asyncio
async def test_warmup_models_reports_the_builds_own_ceiling_distinctly(
    connected_store, caplog, monkeypatch
) -> None:
    """C1-I-2: ``EmbedderWarmupTimeout`` is the *build* blowing its own limit.

    That is a different outcome from this caller giving up, and the old shared
    ``except asyncio.TimeoutError`` reported it as the latter — promising a
    background build that had in fact already been abandoned.
    """

    class _TimeoutBackend:
        model_name: str = "timeout-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            raise AssertionError("must not be reached")

    async def _raise_timeout() -> None:
        raise EmbedderWarmupTimeout("build blew its own ceiling")

    pipeline = make_pipeline(connected_store)
    embedder = Embedder(_TimeoutBackend())  # type: ignore[arg-type]
    monkeypatch.setattr(embedder, "warmup", _raise_timeout)
    pipeline._global_embedder = embedder
    pipeline._reranker = None

    with caplog.at_level(logging.WARNING, logger="archon_search.pipeline"):
        await pipeline.warmup_models(embedder_timeout=0.5)

    assert "exceeded its own" in caplog.text
    assert "the build continues in the background" not in caplog.text


@pytest.mark.asyncio
async def test_warmup_models_never_reports_waiting_none_seconds(
    connected_store, caplog
) -> None:
    """An unbounded wait cannot time out, so it must never claim it waited ``None``s.

    Since 3.11 ``asyncio.TimeoutError`` **is** the builtin ``TimeoutError``, so a
    socket ``ETIMEDOUT`` from a model download lands in the same handler on the
    unbounded lifespan/ingest path, where ``embedder_timeout`` is ``None``.
    """

    class _EtimedoutBackend:
        model_name: str = "etimedout-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            raise TimeoutError("ETIMEDOUT fetching model weights")

    pipeline = make_pipeline(connected_store)
    pipeline._global_embedder = Embedder(_EtimedoutBackend())  # type: ignore[arg-type]
    pipeline._reranker = None

    with caplog.at_level(logging.WARNING, logger="archon_search.pipeline"):
        await pipeline.warmup_models()

    assert "Nones" not in caplog.text
    assert "embedder failed" in caplog.text


# ---------------------------------------------------------------------------
# fts_only=True degrade paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_fts_only_returns_results_without_embedding(
    connected_store, col_name, tmp_path
) -> None:
    """``search(fts_only=True)`` answers from the FTS leg without a query vector."""
    pipeline = make_pipeline(connected_store)
    await _ingest_corpus(pipeline, col_name, tmp_path)

    result = await pipeline.search(
        "searchable degraded ranking content",
        col_name,
        embedder=Embedder(_AssertNeverCalledEmbedderBackend()),  # type: ignore[arg-type]
        fts_only=True,
    )

    assert result.results, "the degraded path must still return results"


@pytest.mark.asyncio
async def test_search_many_fts_only_returns_results_without_embedding(
    connected_store, col_name, tmp_path
) -> None:
    """``search_many(fts_only=True)`` fans out without embedding the query."""
    pipeline = make_pipeline(connected_store)
    await _ingest_corpus(pipeline, col_name, tmp_path)
    pipeline._global_embedder = Embedder(_AssertNeverCalledEmbedderBackend())  # type: ignore[arg-type]

    result = await pipeline.search_many(
        "searchable degraded ranking content", [col_name], fts_only=True
    )

    assert result.results, "the degraded fan-out must still return results"


@pytest.mark.asyncio
async def test_fts_only_is_ignored_when_the_caller_supplies_a_vector(
    connected_store, col_name, tmp_path
) -> None:
    """An already-computed vector removes the need to embed, so it cancels the degrade.

    The query text matches nothing in the FTS index, so every result can only have
    come from the vector leg the degrade would otherwise have skipped.
    """
    pipeline = make_pipeline(connected_store)
    await _ingest_corpus(pipeline, col_name, tmp_path)

    result = await pipeline.search(
        "zzqqxx unmatchable",
        col_name,
        embedder=Embedder(_AssertNeverCalledEmbedderBackend()),  # type: ignore[arg-type]
        query_vector=[0.1] * 4,
        fts_only=True,
    )

    assert result.results, "the caller-supplied vector must still drive the vector leg"
