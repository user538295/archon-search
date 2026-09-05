"""Integration tests for BE-12: GraphExtractor degrades cleanly through the real
SearchPipeline + GraphStore + SearchStore wiring on the three auxiliary-failure
modes ProseExtractionBackend can raise for — a missing/unloadable model
artifact (S14), a mid-batch inference() raise (S17), and a WAITER timeout on
another caller's in-flight load (S46). All three must: persist chunks, keep
code-symbol nodes, mark the extraction result degraded, and surface exactly
one sanitized warning — never fail the ingest.

Only the ``ProseExtractionBackend`` boundary is faked (same seam as
``tests/_graph_engine_stub.py``); ``GraphExtractor``, ``GraphStore`` and
``SearchStore`` are the real classes, so this exercises BE-12's actual
degrade wiring end to end.

The fake backend is the shared ``_FakeBackend`` from ``tests/test_graph_extractor.py``
(also reused by the bug030 graph-degradation latch ingest test) —
not an ad-hoc per-file class — so this stays the one fixture for the seam.

Stale-row deletion on a degraded re-ingest is proven via ``GraphStore.edge_count``,
the same proxy ``test_reingest_under_degradation_leaves_no_orphan_prose_edges``
(the bug030 latch-ingest test) uses — NOT via the mentions table:
``delete_mentions_by_doc`` runs unconditionally on every ingest (pipeline.py),
degraded or not, so a mention-based assertion cannot distinguish a genuinely
degraded re-ingest from a non-degraded one. Edges, by contrast, are only
rewritten by ``delete_graph_by_doc`` + a fresh ``write_graph`` — on a degraded
re-ingest ``write_graph`` never runs for the prose contribution, so the
document's prose edges must drop to zero. The seeding "working" backend
returns 2+ DISTINCT entities per chunk (not one repeated entity) so the
co-occurrence loop (``graph_extractor.py``'s ``itertools.combinations`` over a
chunk's entities) actually produces edges to prove got deleted.

A real ``.py`` ingest tags EVERY chunk with a tree-sitter-derived
``_symbol_type`` (module-level code included — see
``archon_search/code_enricher.py``), so there is no way to get both a
code-symbol chunk and a plain-prose chunk out of one real parse. `CodeEnricher.
enrich_chunk` is patched to alternate — first chunk keeps its real
code-symbol metadata, the rest degrade to plain prose metadata — so the
document genuinely exercises both the C3 code-symbol path and the prose
extraction path in the same ingest, same as production documents that mix
functions and prose. The patch keys its "first chunk" bookkeeping on the
``scope_table`` object's identity (a fresh one per ``ingest_file`` call —
``pipeline.py``'s ``enricher.prepare(...)``) rather than a single counter
shared across the whole test function, so EVERY ``ingest_file`` call in a
test — including a degraded re-ingest — gets the same mixed code+prose split,
not just the first.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import GraphConfig
from archon_search.graph_store import GraphStore
from archon_search.graph_types import EntityType
from archon_search.prose_extraction_backend import ProseExtractionLoadTimeoutError
from tests.test_graph_extractor import ChunkExtraction, _entity, _FakeBackend

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Shared fixture wiring — the shared _FakeBackend, not ad-hoc per-file fakes.
# ---------------------------------------------------------------------------


def _working_backend() -> _FakeBackend:
    """A backend whose inference() returns 2 DISTINCT entities per chunk, so
    the co-occurrence loop actually produces an edge (a single repeated
    entity across chunks would not — co-occurrence is per-chunk only)."""
    return _FakeBackend(
        default=ChunkExtraction(
            entities=[
                _entity("StubPerson", EntityType.person.value),
                _entity("StubConcept", EntityType.concept.value),
            ]
        )
    )


def _patch_backend(monkeypatch: pytest.MonkeyPatch, backend: object) -> None:
    monkeypatch.setattr(
        "archon_search.graph_extractor.ProseExtractionBackend",
        lambda *args, **kwargs: backend,
    )


def _patch_mixed_code_and_prose_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the first chunk of EVERY ingested file (not just the first
    ``ingest_file`` call in the test) to keep its real code-symbol metadata,
    and every subsequent chunk of that same ingest to degrade to plain prose
    (no ``_symbol_type``) — see module docstring for why this is needed to
    get both chunk kinds out of one real ``.py`` parse, and why the
    bookkeeping must be keyed per ingest rather than globally.
    """
    from archon_search.code_enricher import CodeEnricher

    real_enrich_chunk = CodeEnricher.enrich_chunk
    call_counts: dict[int, int] = {}

    def _fake_enrich_chunk(self, record, scope_table):
        key = id(scope_table)  # fresh scope_table per ingest_file call
        call_counts[key] = call_counts.get(key, 0) + 1
        if call_counts[key] == 1:
            return real_enrich_chunk(self, record, scope_table)
        return {}

    monkeypatch.setattr(CodeEnricher, "enrich_chunk", _fake_enrich_chunk)


async def _make_pipeline_with_real_graph_extractor(tmp_path: Path):
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.graph_extractor import GraphExtractor
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    db_path = str(tmp_path / "search")
    store = SearchStore(db_path)
    await store.connect()

    graph_store = GraphStore(db_path)
    await graph_store.connect()

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs):
            return [0.5] * len(pairs)

    graph_config = GraphConfig(enabled=True, backend_threshold_edges=10_000)
    graph_extractor = GraphExtractor(graph_config)

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=2),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        graph_extractor=graph_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
    )
    return store, graph_store, pipeline


_PY_SOURCE = (
    "def parse_config():\n"
    "    return 1\n"
    "\n\n"
    "def handle_request():\n"
    "    return 2\n"
)

_NAMESPACE = "default"


@pytest.mark.asyncio
async def test_missing_artifact_still_persists_chunks_and_code_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S14: a missing/unloadable prose model artifact degrades the ingest —
    it still succeeds, chunks persist, code-symbol nodes are written, the
    result is degraded, and exactly the pinned sanitized warning is returned.
    """
    from archon_search.graph_extractor import _BACKEND_UNAVAILABLE_DETAIL

    broken_backend = _FakeBackend(
        load_error=RuntimeError("model artifact not found on disk")
    )
    _patch_backend(monkeypatch, broken_backend)
    _patch_mixed_code_and_prose_chunks(monkeypatch)

    store, graph_store, pipeline = await _make_pipeline_with_real_graph_extractor(tmp_path)
    try:
        collection = "be12-missing-artifact"
        doc_file = tmp_path / "sample.py"
        doc_file.write_text(_PY_SOURCE)

        result = await pipeline.ingest_file(doc_file, collection, embedder=pipeline._global_embedder)

        assert result.status == "ok", f"ingest must still succeed: {result.error}"
        assert result.chunks_created > 0, "chunks must persist despite the degrade"
        assert result.warnings == [_BACKEND_UNAVAILABLE_DETAIL]

        nodes = await graph_store.get_all_nodes(collection, ns=_NAMESPACE)
        code_symbol_nodes = [n for n in nodes if n.entity_type == EntityType.code_symbol]
        assert code_symbol_nodes, "code-symbol node(s) must survive the degrade"

        # S14 requires GraphExtractionResult.degraded is True (pipeline.py
        # keys stale-prose-row deletion on it). The observable proxy is
        # GraphStore.edge_count — see module docstring for why mentions are
        # NOT a valid proxy (delete_mentions_by_doc runs unconditionally).
        pipeline._graph_extractor._backend = _working_backend()
        first = await pipeline.ingest_file(doc_file, collection, embedder=pipeline._global_embedder)
        assert first.status == "ok"
        edges_before = await graph_store.edge_count(collection, ns=_NAMESPACE)
        assert edges_before > 0, "the working-backend pass must produce prose edges to orphan"

        pipeline._graph_extractor._backend = _FakeBackend(
            load_error=RuntimeError("model artifact not found on disk")
        )
        second = await pipeline.ingest_file(doc_file, collection, embedder=pipeline._global_embedder)
        assert second.status == "ok"
        assert second.warnings == [_BACKEND_UNAVAILABLE_DETAIL]

        edges_after = await graph_store.edge_count(collection, ns=_NAMESPACE)
        assert edges_after == 0, (
            "prose edges from the prior non-degraded ingest survived a "
            f"degraded re-ingest; got {edges_after} edge(s), was {edges_before}"
        )
    finally:
        await store.disconnect()
        await graph_store.disconnect()


@pytest.mark.asyncio
async def test_mid_batch_raise_returns_the_pinned_sanitized_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S17: an inference() call that raises mid-batch degrades the ingest —
    chunks persist, the result is degraded, and the returned warning is
    asserted EQUAL to the pinned sanitized constant (not merely absent of
    the raw exception text).
    """
    from archon_search.graph_extractor import _INFERENCE_FAILED_DETAIL

    broken_backend = _FakeBackend(
        infer_error=RuntimeError("boom: forward pass raised mid-batch")
    )
    _patch_backend(monkeypatch, broken_backend)
    _patch_mixed_code_and_prose_chunks(monkeypatch)

    store, graph_store, pipeline = await _make_pipeline_with_real_graph_extractor(tmp_path)
    try:
        collection = "be12-mid-batch-raise"
        doc_file = tmp_path / "sample.py"
        doc_file.write_text(_PY_SOURCE)

        result = await pipeline.ingest_file(doc_file, collection, embedder=pipeline._global_embedder)

        assert result.status == "ok", f"ingest must still succeed: {result.error}"
        assert result.chunks_created > 0, "chunks must persist despite the degrade"
        assert result.warnings == [_INFERENCE_FAILED_DETAIL]
        for warning in result.warnings:
            assert "boom" not in warning, "the raw exception text must never reach the wire"

        # S17 requires GraphExtractionResult.degraded is True (pipeline.py
        # keys stale-prose-row deletion on it) — same edge_count proxy as
        # the S14 test above (see module docstring for why mentions are not
        # a valid proxy).
        pipeline._graph_extractor._backend = _working_backend()
        first = await pipeline.ingest_file(doc_file, collection, embedder=pipeline._global_embedder)
        assert first.status == "ok"
        edges_before = await graph_store.edge_count(collection, ns=_NAMESPACE)
        assert edges_before > 0, "the working-backend pass must produce prose edges to orphan"

        pipeline._graph_extractor._backend = _FakeBackend(
            infer_error=RuntimeError("boom: forward pass raised mid-batch")
        )
        second = await pipeline.ingest_file(doc_file, collection, embedder=pipeline._global_embedder)
        assert second.status == "ok"
        assert second.warnings == [_INFERENCE_FAILED_DETAIL]

        edges_after = await graph_store.edge_count(collection, ns=_NAMESPACE)
        assert edges_after == 0, (
            "prose edges from the prior non-degraded ingest survived a "
            f"degraded re-ingest; got {edges_after} edge(s), was {edges_before}"
        )
    finally:
        await store.disconnect()
        await graph_store.disconnect()


@pytest.mark.asyncio
async def test_wait_timeout_degrades_and_deletes_stale_prose_rows_on_reingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S46: a WAITER that times out on another caller's in-flight backend
    load must degrade this ingest end to end — status stays "ok", chunks
    persist, and the pinned `_LOAD_WAIT_TIMEOUT_DETAIL` warning is returned
    — proving S46's "ingest still succeeds, no 503" through the full
    pipeline, not merely that the backend raises the right exception type.

    The observable proxy for `GraphExtractionResult.degraded` (not itself
    wire-facing) is `GraphStore.edge_count` — see module docstring for why
    mentions are not a valid proxy here.
    """
    from archon_search.graph_extractor import _LOAD_WAIT_TIMEOUT_DETAIL

    _patch_backend(monkeypatch, _working_backend())
    _patch_mixed_code_and_prose_chunks(monkeypatch)

    store, graph_store, pipeline = await _make_pipeline_with_real_graph_extractor(tmp_path)
    try:
        collection = "be12-wait-timeout"
        doc_file = tmp_path / "sample.py"
        doc_file.write_text(_PY_SOURCE)

        first = await pipeline.ingest_file(doc_file, collection, embedder=pipeline._global_embedder)
        assert first.status == "ok"
        edges_before = await graph_store.edge_count(collection, ns=_NAMESPACE)
        assert edges_before > 0, "the working-backend pass must produce prose edges to orphan"

        pipeline._graph_extractor._backend = _FakeBackend(
            load_error=ProseExtractionLoadTimeoutError(
                "timed out waiting for another caller's in-flight load"
            )
        )
        result = await pipeline.ingest_file(doc_file, collection, embedder=pipeline._global_embedder)

        assert result.status == "ok", f"ingest must still succeed: {result.error}"
        assert result.chunks_created > 0, "chunks must persist despite the degrade"
        assert result.warnings == [_LOAD_WAIT_TIMEOUT_DETAIL]

        edges_after = await graph_store.edge_count(collection, ns=_NAMESPACE)
        assert edges_after == 0, (
            "prose edges from the prior non-degraded ingest survived a "
            f"degraded re-ingest; got {edges_after} edge(s), was {edges_before}"
        )
    finally:
        await store.disconnect()
        await graph_store.disconnect()
