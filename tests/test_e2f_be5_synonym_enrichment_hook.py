"""Unit and integration tests for E2f BE-5: post-ingest synonym enrichment hook.

Tests:
- test_synonym_enrichment_debounce_no_duplicate_job: call schedule_synonym_enrichment twice
  while first task is in-flight; assert pending=True and asyncio.create_task called once.
- test_maintenance_loop_drains_communities_pending_rebuild: after write_graph(), (ns, col) is
  in _communities_pending_rebuild; a maintenance pass calls _spawn_rebuild_task and removes key.
- test_pipeline_calls_on_synonym_edges_written_after_write_graph: pipeline calls
  on_synonym_edges_written(collection, ns) after write_graph() when callback is not None.
- test_synonym_enrichment_gated_by_enrichment_auto_false: when enrichment_auto=False,
  no enrichment task is spawned after ingest.
- test_synonym_enrichment_failure_does_not_propagate: exception in SynonymDetector.detect()
  is caught, WARNING logged, ingest result returned normally.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

pytestmark = pytest.mark.xdist_group("maintenance_loop")


# ---------------------------------------------------------------------------
# Helpers — minimal SearchPipeline with graph components
# ---------------------------------------------------------------------------

def _make_pipeline(
    store,
    *,
    graph_extractor=None,
    graph_store=None,
    graph_config=None,
    on_synonym_edges_written=None,
):
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs):
            return [0.5] * len(pairs)

    p = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        graph_extractor=graph_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
    )
    p.on_synonym_edges_written = on_synonym_edges_written
    return p


def _make_maintenance_loop(tmp_path, graph_store=None, graph_config=None):
    """Create a MaintenanceLoop with minimal config for testing."""
    from archon_search.jobs.maintenance_loop import MaintenanceLoop
    from archon_search.config import MaintenanceConfig

    job_store = MagicMock()
    job_store.list.return_value = []
    search_store = MagicMock()
    search_store.list_collections = AsyncMock(return_value=[])

    config = MaintenanceConfig()
    config.interval_hours = 0
    config.fts_optimize = False
    config.orphan_cleanup = False
    config.prune_expired_chunks = False
    config.failed_ingest_retry = False

    loop = MaintenanceLoop(
        job_store=job_store,
        search_store=search_store,
        config=config,
        data_dir=tmp_path,
        graph_store=graph_store,
        graph_config=graph_config,
    )
    return loop


# ---------------------------------------------------------------------------
# Unit test S12: debounce — no duplicate enrichment tasks
# ---------------------------------------------------------------------------

def test_synonym_enrichment_debounce_no_duplicate_job(tmp_path):
    """Call schedule_synonym_enrichment twice while first task is in-flight.

    Asserts:
    - asyncio.create_task called exactly once (no duplicate task).
    - _synonym_state[(ns, col)].pending == True after second call.
    """
    loop = _make_maintenance_loop(tmp_path)

    col = "docs"
    ns = "default"

    # We need an event loop to create real tasks for the in-flight check
    async def _run():
        # Create a never-completing coroutine to simulate in-flight task
        sentinel = asyncio.Event()

        async def _never_complete():
            await sentinel.wait()

        with patch("asyncio.create_task", wraps=asyncio.create_task) as mock_create_task:
            # First call — should spawn a task
            loop.schedule_synonym_enrichment(col, ns)
            assert mock_create_task.call_count == 1

            # Second call while in-flight — should set pending, not spawn
            loop.schedule_synonym_enrichment(col, ns)
            assert mock_create_task.call_count == 1, (
                "create_task must NOT be called twice when task is in-flight"
            )

        # Verify pending flag is set
        key = (ns, col)
        assert key in loop._synonym_state
        assert loop._synonym_state[key].pending is True

        # Cancel the in-flight task to avoid ResourceWarning
        state = loop._synonym_state[key]
        state.task.cancel()
        try:
            await state.task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Unit test S3/S4: _communities_pending_rebuild draining
# ---------------------------------------------------------------------------

def test_maintenance_loop_drains_communities_pending_rebuild(tmp_path):
    """schedule_synonym_enrichment adds (ns, col) to _communities_pending_rebuild after
    write_graph() succeeds. A maintenance pass calls _spawn_rebuild_task(ns, col) and
    removes the key from the set.

    Verifies S3/S4 community rebuild trigger path.
    """
    loop = _make_maintenance_loop(tmp_path)

    col = "docs"
    ns = "default"

    # Directly seed _communities_pending_rebuild as the producer would after write_graph()
    loop._communities_pending_rebuild.add((ns, col))
    assert (ns, col) in loop._communities_pending_rebuild

    # Stub _spawn_rebuild_task to avoid real asyncio.create_task
    spawned_calls: list[tuple[str, str]] = []

    def _stub_spawn(namespace, collection):
        spawned_calls.append((namespace, collection))

    loop._spawn_rebuild_task = _stub_spawn

    # Simulate the maintenance pass draining _communities_pending_rebuild
    loop._drain_communities_pending_rebuild()

    # Key must be removed from the set
    assert (ns, col) not in loop._communities_pending_rebuild, (
        "_communities_pending_rebuild must be cleared after drain"
    )

    # _spawn_rebuild_task must have been called with (ns, col) — namespace first, collection second
    assert spawned_calls == [(ns, col)], (
        f"Expected _spawn_rebuild_task({ns!r}, {col!r}), got {spawned_calls}"
    )


# ---------------------------------------------------------------------------
# Unit test: pipeline calls on_synonym_edges_written after write_graph()
# ---------------------------------------------------------------------------

def test_pipeline_calls_on_synonym_edges_written_after_write_graph(tmp_path):
    """pipeline.py calls on_synonym_edges_written(collection, ns) after write_graph()
    when the callback is set and graph extraction succeeds.
    """
    import asyncio
    from pathlib import Path

    from archon_search.config import GraphConfig
    from archon_search.graph_types import GraphExtractionResult

    col = "docs"
    ns = "default"

    # Stub graph_extractor, graph_store, graph_config
    graph_config = GraphConfig()
    graph_config.enabled = True
    graph_config.enrichment_auto = True

    extraction_result = GraphExtractionResult(nodes=[], edges=[], mentions=[])
    graph_extractor = MagicMock()
    graph_extractor.extract = AsyncMock(return_value=extraction_result)

    graph_store = MagicMock()
    graph_store.ensure_graph_tables = AsyncMock()
    graph_store.write_graph = AsyncMock()
    graph_store.delete_mentions_by_doc = AsyncMock()
    graph_store.write_mentions = AsyncMock()
    graph_store.edge_count = AsyncMock(return_value=0)

    # Track callback calls
    callback_calls: list[tuple[str, str]] = []

    def _on_synonym_edges_written(collection, namespace):
        callback_calls.append((collection, namespace))

    chunk_ingest_result = MagicMock()
    chunk_ingest_result.chunks_ingested = 1
    chunk_ingest_result.needs_recompute = False  # avoids recompute_collection_meta call

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.lock_for = MagicMock(return_value=asyncio.Lock())
    store.ingest_chunks = AsyncMock(return_value=chunk_ingest_result)
    store.rebuild_fts_index = AsyncMock()
    store.optimize_fts = AsyncMock()
    store.supports_incremental_fts_delete = False
    store.delete_document = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="en")

    pipeline = _make_pipeline(
        store,
        graph_extractor=graph_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
        on_synonym_edges_written=_on_synonym_edges_written,
    )

    # Create a sample text file to ingest
    doc_file = tmp_path / "test.md"
    doc_file.write_text("# Test\nSome content about AuthService and TokenValidator.\n")

    from archon_search.embedder import Embedder

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    embedder = Embedder(_MockEmbedderBackend())

    async def _run():
        result = await pipeline.ingest_file(
            doc_file,
            collection=col,
            namespace=ns,
            embedder=embedder,
        )
        return result

    result = asyncio.run(_run())

    assert result.status == "ok", f"Ingest failed: {result}"
    assert callback_calls == [(col, ns)], (
        f"Expected on_synonym_edges_written({col!r}, {ns!r}), got {callback_calls}"
    )


# ---------------------------------------------------------------------------
# Unit test S15: enrichment_auto=False gates the enrichment callback
# ---------------------------------------------------------------------------

def test_synonym_enrichment_gated_by_enrichment_auto_false(tmp_path):
    """When enrichment_auto=False, on_synonym_edges_written is NOT called after ingest."""
    import asyncio
    from pathlib import Path

    from archon_search.config import GraphConfig
    from archon_search.graph_types import GraphExtractionResult

    col = "docs"
    ns = "default"

    graph_config = GraphConfig()
    graph_config.enabled = True
    graph_config.enrichment_auto = False  # disabled

    extraction_result = GraphExtractionResult(nodes=[], edges=[], mentions=[])
    graph_extractor = MagicMock()
    graph_extractor.extract = AsyncMock(return_value=extraction_result)

    graph_store = MagicMock()
    graph_store.ensure_graph_tables = AsyncMock()
    graph_store.write_graph = AsyncMock()
    graph_store.delete_mentions_by_doc = AsyncMock()
    graph_store.write_mentions = AsyncMock()
    graph_store.edge_count = AsyncMock(return_value=0)

    callback_calls: list[tuple[str, str]] = []

    def _on_synonym_edges_written(collection, namespace):
        callback_calls.append((collection, namespace))

    chunk_ingest_result = MagicMock()
    chunk_ingest_result.chunks_ingested = 1
    chunk_ingest_result.needs_recompute = False

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.lock_for = MagicMock(return_value=asyncio.Lock())
    store.ingest_chunks = AsyncMock(return_value=chunk_ingest_result)
    store.rebuild_fts_index = AsyncMock()
    store.optimize_fts = AsyncMock()
    store.supports_incremental_fts_delete = False
    store.delete_document = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="en")

    pipeline = _make_pipeline(
        store,
        graph_extractor=graph_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
        on_synonym_edges_written=_on_synonym_edges_written,
    )

    doc_file = tmp_path / "test.md"
    doc_file.write_text("# Test\nSome content.\n")

    from archon_search.embedder import Embedder

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    embedder = Embedder(_MockEmbedderBackend())

    async def _run():
        return await pipeline.ingest_file(
            doc_file,
            collection=col,
            namespace=ns,
            embedder=embedder,
        )

    result = asyncio.run(_run())

    assert result.status == "ok", f"Ingest failed: {result}"
    assert callback_calls == [], (
        "on_synonym_edges_written must NOT be called when enrichment_auto=False"
    )


# ---------------------------------------------------------------------------
# Unit test: synonym enrichment failure does not propagate
# ---------------------------------------------------------------------------

def test_synonym_enrichment_failure_does_not_propagate(tmp_path):
    """Exception in the synonym enrichment path is caught, WARNING logged, result returned ok."""
    import asyncio
    import logging
    from pathlib import Path

    from archon_search.config import GraphConfig
    from archon_search.graph_types import GraphExtractionResult

    col = "docs"
    ns = "default"

    graph_config = GraphConfig()
    graph_config.enabled = True
    graph_config.enrichment_auto = True

    extraction_result = GraphExtractionResult(nodes=[], edges=[], mentions=[])
    graph_extractor = MagicMock()
    graph_extractor.extract = AsyncMock(return_value=extraction_result)

    graph_store = MagicMock()
    graph_store.ensure_graph_tables = AsyncMock()
    graph_store.write_graph = AsyncMock()
    graph_store.delete_mentions_by_doc = AsyncMock()
    graph_store.write_mentions = AsyncMock()
    graph_store.edge_count = AsyncMock(return_value=0)

    # Callback that raises to simulate enrichment failure
    def _failing_callback(collection, namespace):
        raise RuntimeError("Simulated enrichment failure")

    chunk_ingest_result = MagicMock()
    chunk_ingest_result.chunks_ingested = 1
    chunk_ingest_result.needs_recompute = False

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.lock_for = MagicMock(return_value=asyncio.Lock())
    store.ingest_chunks = AsyncMock(return_value=chunk_ingest_result)
    store.rebuild_fts_index = AsyncMock()
    store.optimize_fts = AsyncMock()
    store.supports_incremental_fts_delete = False
    store.delete_document = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="en")

    pipeline = _make_pipeline(
        store,
        graph_extractor=graph_extractor,
        graph_store=graph_store,
        graph_config=graph_config,
        on_synonym_edges_written=_failing_callback,
    )

    doc_file = tmp_path / "test.md"
    doc_file.write_text("# Test\nSome content.\n")

    from archon_search.embedder import Embedder

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    embedder = Embedder(_MockEmbedderBackend())

    warning_records: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                warning_records.append(record.getMessage())

    handler = _CapturingHandler()
    logging.getLogger("archon_search.pipeline").addHandler(handler)
    try:
        async def _run():
            return await pipeline.ingest_file(
                doc_file,
                collection=col,
                namespace=ns,
                embedder=embedder,
            )

        result = asyncio.run(_run())
    finally:
        logging.getLogger("archon_search.pipeline").removeHandler(handler)

    assert result.status == "ok", (
        "Ingest must succeed even when synonym enrichment callback raises"
    )
    assert any("synonym" in msg.lower() or "enrichment" in msg.lower() for msg in warning_records), (
        f"Expected WARNING about synonym enrichment failure; got: {warning_records}"
    )
