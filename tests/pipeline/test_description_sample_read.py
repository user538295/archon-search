"""BE-21 (C2) — the ingest path reads its description sample through the store port.

``list_chunk_ids`` lists the open chunk ids, ``read_excerpts`` reads the picked ones; a store error is not an empty
sample, and it never fails the ingest or replaces the current description. The evenly spaced draw and the fit to the
model's budget belong to the generator rewrite (BE-22); until then the pipeline picks the first ``MAX_SAMPLE_CHUNKS`` ids.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.chunker import DocumentChunker
from archon_search.config import SearchConfig
from archon_search.description_generator import MAX_SAMPLE_CHUNKS
from archon_search.embedder import Embedder, EmbedderBackend
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline
from archon_search.reranker import Reranker, RerankerBackend
from archon_search.store import ChunkIngestResult, StoreReadError

_SECRET_TEXT = "Secret document text"
_COLLECTION = "sample-col"
_OLD_DESCRIPTION = "The description that was there before."
_OPEN_CHUNK_COUNT = 500
_EMBEDDING_DIM = 4
_CHUNK_SIZE = 64
_TOP_K_RETRIEVE = 10
_TOP_K_RETURN = 5


class _StubEmbedder(EmbedderBackend):
    model_name = "mock"
    is_warm = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * _EMBEDDING_DIM for _ in texts]


class _StubReranker(RerankerBackend):
    is_warm = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


def _existing_meta() -> MagicMock:
    meta = MagicMock()
    meta.description = _OLD_DESCRIPTION
    meta.described_at_doc_count = 1
    meta.last_described = datetime(2026, 1, 1, tzinfo=UTC)
    meta.doc_count = 10
    meta.chunk_count = 100
    meta.default_ttl_seconds = None
    return meta


def _store(*, existing_meta: MagicMock | None = None) -> MagicMock:
    store = MagicMock()
    store.assert_collection_access = AsyncMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=3, needs_recompute=False))
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=existing_meta)
    store.update_description = AsyncMock()
    store.update_collection_meta = AsyncMock()
    store._config = SearchConfig()
    return store


def _pipeline(store: MagicMock) -> SearchPipeline:
    return SearchPipeline(
        store=store,
        embedder=Embedder(_StubEmbedder()),
        reranker=Reranker(_StubReranker()),
        chunker=DocumentChunker(chunk_size=_CHUNK_SIZE),
        parser=DocumentParser(),
        top_k_retrieve=_TOP_K_RETRIEVE,
        top_k_return=_TOP_K_RETURN,
    )


def test_max_sample_chunks_constant_equals_100() -> None:
    assert MAX_SAMPLE_CHUNKS == 100


@pytest.mark.asyncio
async def test_description_sample_is_capped_at_max_sample_chunks(tmp_path) -> None:
    """A collection with more open chunks than the cap is sampled with the first 100 ids, in listing order."""
    store = _store()
    listed_ids = [f"chunk-{i:04d}" for i in range(_OPEN_CHUNK_COUNT)]
    texts = [f"excerpt-{i:04d}" for i in range(MAX_SAMPLE_CHUNKS)]
    store.list_chunk_ids = AsyncMock(return_value=listed_ids)
    store.read_excerpts = AsyncMock(return_value=texts)
    pipeline = _pipeline(store)
    (tmp_path / "doc.md").write_text("# Big collection\n\n" + "Content.\n" * 20)

    with patch("archon_search.pipeline.generate_description", return_value="A description") as mock_gen, \
         patch("archon_search.pipeline._should_regenerate", return_value=True):
        await pipeline.ingest_directory(tmp_path, _COLLECTION, embedder=pipeline._global_embedder, rebuild_fts=False)

    store.list_chunk_ids.assert_awaited_once_with(_COLLECTION, "default")
    store.read_excerpts.assert_awaited_once_with(_COLLECTION, "default", listed_ids[:MAX_SAMPLE_CHUNKS])
    mock_gen.assert_awaited_once()
    assert mock_gen.call_args[0][0] == texts


@pytest.mark.parametrize("failing_call", ["list_chunk_ids", "read_excerpts"])
@pytest.mark.asyncio
async def test_sample_read_error_keeps_the_description_and_the_ingest(tmp_path, caplog, failing_call: str) -> None:
    """A store error in either read is not "no chunks": the old description stays, one WARNING, no cause text logged."""
    store = _store(existing_meta=_existing_meta())
    store.list_chunk_ids = AsyncMock(return_value=["chunk-0001"])
    store.read_excerpts = AsyncMock(return_value=[])
    cause = OSError(_SECRET_TEXT)
    error = StoreReadError(_COLLECTION)
    error.__cause__ = cause
    setattr(store, failing_call, AsyncMock(side_effect=error))
    pipeline = _pipeline(store)
    (tmp_path / "doc.md").write_text("# Doc\n\nSome document text.\n" * 5)

    with patch("archon_search.pipeline.generate_description", return_value=None) as mock_gen, \
         patch("archon_search.pipeline._should_regenerate", return_value=True), \
         caplog.at_level(logging.WARNING, logger="archon_search.pipeline"):
        results = await pipeline.ingest_directory(
            tmp_path, _COLLECTION, embedder=pipeline._global_embedder, rebuild_fts=False
        )

    assert [r.status for r in results] == ["ok"]
    assert mock_gen.call_args[0][0] == [], "the generator is handed no excerpts"
    assert store.update_description.await_args.args[1] == _OLD_DESCRIPTION
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and _COLLECTION in r.getMessage()]
    assert len(warnings) == 1
    assert "OSError" in warnings[0].getMessage(), "the operator sees what kind of failure it was"
    assert _SECRET_TEXT not in warnings[0].getMessage()
    assert _SECRET_TEXT not in caplog.text
