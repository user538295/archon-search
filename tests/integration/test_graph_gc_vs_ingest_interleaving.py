"""M3: graph GC never removes the graph rows of an ingest that is writing them.

An ingest persists a document's chunks, then its graph nodes and edges, then its mentions, taking the collection
lock only around each store call. Graph GC snapshots the live chunk ids, prunes the mentions of chunks outside the
snapshot, then deletes every node with no mention. Interleaved without a common lock it deletes the nodes an ingest
has written but not yet mentioned, or the mentions of chunks that appeared after its snapshot, and the loss is
silent. Real LanceDB stores and the real pipeline; each interleaving is forced with events.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.chunker import DocumentChunker
from archon_search.config import GraphConfig, MaintenanceConfig
from archon_search.embedder import Embedder
from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    EntityType,
    GraphExtractionResult,
    GraphMention,
    GraphNode,
    make_stable_entity_id,
)
from archon_search.jobs.maintenance_loop import MaintenanceLoop
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline
from archon_search.reranker import Reranker
from archon_search.store import SearchStore

pytestmark = pytest.mark.integration

_COLLECTION = "gc_race"
_NAMESPACE = "default"
_SETTLE_SECONDS = 0.2
_JOIN_SECONDS = 30.0


class _EmbedderBackend:
    model_name = "mock-embedder"
    is_warm = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]


class _RerankerBackend:
    is_warm = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


def _extractor(entity_name: str) -> MagicMock:
    """An extractor that finds one concept, *entity_name*, in the first chunk of whatever document it is given."""

    async def extract(chunk_inputs: list[Any], doc_id: str, collection: str) -> GraphExtractionResult:
        node = GraphNode(
            id=make_stable_entity_id(EntityType.concept.value, entity_name.lower()),
            entity_name=entity_name,
            entity_type=EntityType.concept,
            source_doc_id=doc_id,
            collection_name=collection,
            entity_subtype=None,
        )
        mention = GraphMention(entity_id=node.id, chunk_id=chunk_inputs[0].chunk_id, doc_id=doc_id)
        return GraphExtractionResult(nodes=[node], edges=[], mentions=[mention], warnings=[], fatal_error=None)

    stub = MagicMock()
    stub.extract = AsyncMock(side_effect=extract)
    return stub


class _PausingGraphStore:
    """Delegates to a real ``GraphStore``; the first call of one named method, once it ran, waits to be resumed."""

    def __init__(self, inner: GraphStore, pause_after: str) -> None:
        self._inner = inner
        self._pause_after = pause_after
        self.paused = asyncio.Event()
        self.resume = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if name != self._pause_after:
            return attribute

        async def paused_call(*args: Any, **kwargs: Any) -> Any:
            result = await attribute(*args, **kwargs)
            if not self.paused.is_set():
                self.paused.set()
                await self.resume.wait()
            return result

        return paused_call


@pytest.fixture
async def stores(tmp_path: Path) -> AsyncIterator[tuple[SearchStore, GraphStore]]:
    search = SearchStore(str(tmp_path / "search"))
    await search.connect()
    graph = GraphStore(str(tmp_path / "search"))
    await graph.connect()
    yield search, graph
    await search.disconnect()
    await graph.disconnect()


def _pipeline(search: SearchStore, graph: Any, entity_name: str) -> SearchPipeline:
    return SearchPipeline(
        store=search,
        embedder=Embedder(_EmbedderBackend()),
        reranker=Reranker(_RerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        graph_extractor=_extractor(entity_name),
        graph_store=graph,
        graph_config=GraphConfig(enabled=True, backend_threshold_edges=10_000),
    )


def _gc_loop(tmp_path: Path, search: SearchStore, graph: Any) -> MaintenanceLoop:
    loop = MaintenanceLoop(
        dispatch_retry=lambda _job: None,
        job_store=MagicMock(),
        search_store=search,
        config=MaintenanceConfig(interval_hours=0),
        data_dir=tmp_path,
    )
    loop._graph_store = graph
    loop._spawn_rebuild_task = MagicMock()  # type: ignore[method-assign]
    return loop


def _write_doc(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.md"
    path.write_text(f"# {name}\n\nText about {name}.\n", encoding="utf-8")
    return path


async def _mentioned_entities(graph: GraphStore) -> set[str]:
    table = await graph._require_db().open_table(graph._mentions_table_name(_COLLECTION, _NAMESPACE))
    return set((await table.query().select(["entity_id"]).to_arrow())["entity_id"].to_pylist())


async def _ingest_first_document(tmp_path: Path, search: SearchStore, graph: GraphStore) -> None:
    """Give the collection one fully ingested document, so its mentions table is not empty (GC skips an empty one)."""
    result = await _pipeline(search, graph, "Alpha").ingest_file(
        _write_doc(tmp_path, "doc-a"), _COLLECTION, embedder=Embedder(_EmbedderBackend())
    )
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_gc_does_not_delete_the_nodes_an_ingest_wrote_but_has_not_yet_mentioned(
    tmp_path: Path, stores: tuple[SearchStore, GraphStore]
) -> None:
    search, graph = stores
    await _ingest_first_document(tmp_path, search, graph)
    paused_graph = _PausingGraphStore(graph, pause_after="write_graph")
    second = _pipeline(search, paused_graph, "Beta")

    ingest = asyncio.create_task(
        second.ingest_file(_write_doc(tmp_path, "doc-b"), _COLLECTION, embedder=Embedder(_EmbedderBackend()))
    )
    await asyncio.wait_for(paused_graph.paused.wait(), _JOIN_SECONDS)  # Beta's node is written, its mention is not
    gc = asyncio.create_task(_gc_loop(tmp_path, search, graph)._run_graph_gc(_COLLECTION, _NAMESPACE))
    await asyncio.sleep(_SETTLE_SECONDS)
    paused_graph.resume.set()
    result = await asyncio.wait_for(ingest, _JOIN_SECONDS)
    await asyncio.wait_for(gc, _JOIN_SECONDS)

    assert result.status == "ok"
    assert await graph.node_count(_COLLECTION, _NAMESPACE) == 2
    assert len(await _mentioned_entities(graph)) == 2


@pytest.mark.asyncio
async def test_an_ingest_that_starts_during_gc_keeps_its_graph_rows(
    tmp_path: Path, stores: tuple[SearchStore, GraphStore]
) -> None:
    """GC has snapshotted the live chunks; a document ingested now is not in the snapshot and must not be pruned."""
    search, graph = stores
    await _ingest_first_document(tmp_path, search, graph)
    paused_graph = _PausingGraphStore(graph, pause_after="count_stale_mentions")
    gc = asyncio.create_task(_gc_loop(tmp_path, search, paused_graph)._run_graph_gc(_COLLECTION, _NAMESPACE))
    await asyncio.wait_for(paused_graph.paused.wait(), _JOIN_SECONDS)  # the snapshot is taken, nothing pruned yet

    ingest = asyncio.create_task(
        _pipeline(search, graph, "Beta").ingest_file(
            _write_doc(tmp_path, "doc-b"), _COLLECTION, embedder=Embedder(_EmbedderBackend())
        )
    )
    await asyncio.sleep(_SETTLE_SECONDS)
    paused_graph.resume.set()
    await asyncio.wait_for(gc, _JOIN_SECONDS)
    result = await asyncio.wait_for(ingest, _JOIN_SECONDS)

    assert result.status == "ok"
    assert await graph.node_count(_COLLECTION, _NAMESPACE) == 2
    assert len(await _mentioned_entities(graph)) == 2
