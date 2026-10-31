"""BE-21/BE-22/BE-24 (C2, C5) — an ingest's description request runs the real generator in the lifecycle's task.

The task lists the open chunk ids, draws an evenly spaced sample over all of them and reads it through the store port; a
store error is not an empty sample, and it never fails the ingest or replaces the current description (the row is marked
``failed`` and keeps its text). A pipeline without a lifecycle (eval, installer, a disabled ``[description]``) reads and
generates nothing.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.chunker import DocumentChunker
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.description_generator import DescriptionGenerator
from archon_search.description_lifecycle import GenerationResult
from archon_search.description_sample import StoreReadError
from archon_search.description_write import DescriptionWriteOutcome, FieldOp
from archon_search.embedder import Embedder, EmbedderBackend
from archon_search.llm_client.protocol import LlmCallFailure
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline
from archon_search.reranker import Reranker, RerankerBackend
from archon_search.store import ChunkIngestResult
from tests._description_stubs import (
    PIPELINE_EMBEDDING_MODEL,
    TicketLog,
    recorded_lifecycle,
)
from tests._hyde_stubs import ScriptedLlmClient

_SECRET_TEXT = "Secret document text"
_COLLECTION = "sample-col"
_OLD_DESCRIPTION = "The description that was there before."
_OPEN_CHUNK_COUNT = 500
_SAMPLE_CAP = 100
_EMBEDDING_DIM = 4
_CHUNK_SIZE = 64
_TOP_K_RETRIEVE = 10
_TOP_K_RETURN = 5


class _StubEmbedder(EmbedderBackend):
    model_name = PIPELINE_EMBEDDING_MODEL
    is_warm = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * _EMBEDDING_DIM for _ in texts]


class _StubReranker(RerankerBackend):
    is_warm = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


def _existing_meta() -> CollectionMeta:
    return CollectionMeta(
        name=_COLLECTION,
        description=_OLD_DESCRIPTION,
        description_source="generated",
        description_status="ok",
        doc_count=10,
        chunk_count=100,
        described_at_doc_count=1,
    )


def _store(meta: CollectionMeta) -> MagicMock:
    store = MagicMock()
    store.assert_collection_access = AsyncMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=3, needs_recompute=False))
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.touch_last_indexed = AsyncMock()
    store.update_collection_meta = AsyncMock()
    store.write_description = AsyncMock(return_value=DescriptionWriteOutcome.WRITTEN)
    store._config = SearchConfig()
    return store


def _pipeline(store: MagicMock, client: ScriptedLlmClient | None = None) -> tuple[SearchPipeline, TicketLog | None]:
    lifecycle = None
    tickets = None
    if client is not None:
        lifecycle, tickets = recorded_lifecycle(store, DescriptionGenerator(SearchConfig().description, client, store))
    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_StubEmbedder()),
        reranker=Reranker(_StubReranker()),
        chunker=DocumentChunker(chunk_size=_CHUNK_SIZE),
        parser=DocumentParser(),
        top_k_retrieve=_TOP_K_RETRIEVE,
        top_k_return=_TOP_K_RETURN,
        description_lifecycle=lifecycle,
    )
    return pipeline, tickets


def _excerpts_of(ids: list[str]) -> list[str]:
    return [f"excerpt-{chunk_id}" for chunk_id in ids]


@pytest.mark.asyncio
async def test_generated_description_is_written_from_an_evenly_spaced_sample_capped_at_100(tmp_path) -> None:
    """A collection with more open chunks than the cap is sampled with 100 ids spaced over all of them."""
    store = _store(_existing_meta())
    listed_ids = [f"chunk-{i:04d}" for i in range(_OPEN_CHUNK_COUNT)]
    store.list_chunk_ids = AsyncMock(return_value=listed_ids)
    store.read_excerpts = AsyncMock(side_effect=lambda collection, namespace, ids, include_restricted: _excerpts_of(ids))
    client = ScriptedLlmClient(reply="A description")
    pipeline, tickets = _pipeline(store, client)
    (tmp_path / "doc.md").write_text("# Big collection\n\n" + "Content.\n" * 20)

    await pipeline.ingest_directory(tmp_path, _COLLECTION, embedder=pipeline._global_embedder, rebuild_fts=False)
    assert tickets is not None
    assert await tickets.generation_results() == [GenerationResult.GENERATED]

    store.list_chunk_ids.assert_awaited_once_with(_COLLECTION, "default", False)
    picked = store.read_excerpts.await_args.args[2]
    assert len(picked) == _SAMPLE_CAP
    assert picked == [listed_ids[(2 * j + 1) * _OPEN_CHUNK_COUNT // (2 * _SAMPLE_CAP)] for j in range(_SAMPLE_CAP)]
    assert store.write_description.await_args.args[2].description.value == "A description"
    assert len(client.requests) == 1


@pytest.mark.parametrize("failing_call", ["list_chunk_ids", "read_excerpts"])
@pytest.mark.asyncio
async def test_sample_read_error_keeps_the_description_and_the_ingest(tmp_path, caplog, failing_call: str) -> None:
    """A store error in either read is not "no chunks": the old text stays, one WARNING, no cause text logged."""
    store = _store(_existing_meta())
    store.list_chunk_ids = AsyncMock(return_value=["chunk-0001"])
    store.read_excerpts = AsyncMock(return_value=[])
    cause = OSError(_SECRET_TEXT)
    error = StoreReadError(_COLLECTION)
    error.__cause__ = cause
    setattr(store, failing_call, AsyncMock(side_effect=error))
    client = ScriptedLlmClient(reply="A description that must not be written")
    pipeline, tickets = _pipeline(store, client)
    (tmp_path / "doc.md").write_text("# Doc\n\nSome document text.\n" * 5)

    with caplog.at_level(logging.WARNING, logger="archon_search"):
        results = await pipeline.ingest_directory(
            tmp_path, _COLLECTION, embedder=pipeline._global_embedder, rebuild_fts=False
        )
        assert tickets is not None
        assert await tickets.generation_results() == [GenerationResult.FAILED]

    assert [r.status for r in results] == ["ok"]
    assert client.requests == [], "no provider call without a sample"
    fields = store.write_description.await_args.args[2]
    assert fields.status.value == "failed" and fields.description.action is FieldOp.KEEP
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and _COLLECTION in r.getMessage()]
    assert len(warnings) == 1
    assert "OSError" in warnings[0].getMessage(), "the operator sees what kind of failure it was"
    assert _SECRET_TEXT not in caplog.text


@pytest.mark.asyncio
async def test_provider_failure_keeps_the_old_description_and_the_ingest(tmp_path) -> None:
    """A provider that fails marks the row failed and keeps its text; the ingest result is ok."""
    store = _store(_existing_meta())
    store.list_chunk_ids = AsyncMock(return_value=["chunk-0001"])
    store.read_excerpts = AsyncMock(return_value=["some text"])
    client = ScriptedLlmClient(failure=LlmCallFailure("anthropic", "no API key"))
    pipeline, tickets = _pipeline(store, client)
    (tmp_path / "doc.md").write_text("# Doc\n\nSome document text.\n" * 5)

    results = await pipeline.ingest_directory(
        tmp_path, _COLLECTION, embedder=pipeline._global_embedder, rebuild_fts=False
    )
    assert tickets is not None
    assert await tickets.generation_results() == [GenerationResult.FAILED]

    assert [r.status for r in results] == ["ok"]
    fields = store.write_description.await_args.args[2]
    assert fields.status.value == "failed" and fields.description.action is FieldOp.KEEP


@pytest.mark.asyncio
async def test_pipeline_without_a_lifecycle_reads_and_generates_nothing(tmp_path) -> None:
    store = _store(_existing_meta())
    store.list_chunk_ids = AsyncMock(return_value=["chunk-0001"])
    store.read_excerpts = AsyncMock(return_value=["some text"])
    pipeline, _ = _pipeline(store)
    (tmp_path / "doc.md").write_text("# Doc\n\nSome document text.\n" * 5)

    results = await pipeline.ingest_directory(
        tmp_path, _COLLECTION, embedder=pipeline._global_embedder, rebuild_fts=False
    )

    assert [r.status for r in results] == ["ok"]
    store.list_chunk_ids.assert_not_awaited()
    store.read_excerpts.assert_not_awaited()
    store.write_description.assert_not_awaited()
    store.touch_last_indexed.assert_awaited_once()
