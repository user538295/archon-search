"""Every path that changes a collection's documents asks for a description, through the one finalize routine (S17, S18).

A directory or a documents batch already did (``test_ingest_description_request.py``). Here: a single-file ingest, the
incremental sync and watcher path, and a document delete. The lifecycle's own 20% rule decides whether a request does
anything; the pipeline's job is to ask once, after the row's counts are final, and never to wait for the model.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from archon_search._types import IngestResult
from archon_search.collection_meta import CollectionMeta
from archon_search.description_lifecycle import (
    GenerationKind,
    GenerationResult,
    RequestOutcome,
)
from archon_search.pipeline import SearchPipeline
from archon_search.progress import IndexingStateStore
from archon_search.store import ChunkIngestResult, StoreBusyError
from archon_search.sync import SearchCollectionSync, path_to_collection_name
from tests._description_stubs import (
    ScriptedGenerator,
    fake_lifecycle,
    recorded_lifecycle,
)
from tests.pipeline.test_ingest_description_request import (
    _BASELINE_DOC_COUNT,
    _WAIT_TIMEOUT_S,
    _mock_store,
    _write_docs,
)

from .conftest import make_pipeline

_NAMESPACE = "default"
_COLLECTION = "my-col"


class TestSingleFileIngest:
    @pytest.mark.asyncio
    async def test_requests_one_ingest_kind_generation(self, tmp_path: Path) -> None:
        lifecycle = fake_lifecycle()
        pipeline = make_pipeline(_mock_store(), description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(1))

        result = await pipeline.ingest_file(
            tmp_path / "doc00.md", _COLLECTION, namespace="tenantA", embedder=pipeline._global_embedder
        )

        assert result.status == "ok"
        lifecycle.request_generation.assert_awaited_once_with(_COLLECTION, "tenantA", GenerationKind.INGEST)

    @pytest.mark.asyncio
    async def test_recompute_runs_once_and_before_the_request(self, tmp_path: Path) -> None:
        events: list[str] = []
        lifecycle = fake_lifecycle()
        lifecycle.request_generation = AsyncMock(side_effect=lambda *a: events.append("request"))
        store = _mock_store()
        store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=1, needs_recompute=True))
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        pipeline.recompute_collection_meta = AsyncMock(side_effect=lambda *a, **k: events.append("recompute"))  # type: ignore[method-assign]
        _write_docs(tmp_path, range(1))

        await pipeline.ingest_file(tmp_path / "doc00.md", _COLLECTION, embedder=pipeline._global_embedder)

        assert events == ["recompute", "request"]

    @pytest.mark.asyncio
    async def test_a_failed_file_requests_nothing(self, tmp_path: Path) -> None:
        lifecycle = fake_lifecycle()
        pipeline = make_pipeline(_mock_store(), description_lifecycle=lifecycle)

        result = await pipeline.ingest_file(
            tmp_path / "missing.md", _COLLECTION, embedder=pipeline._global_embedder
        )

        assert result.status == "error"
        lifecycle.request_generation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_batch_member_leaves_the_request_to_its_batch(self, tmp_path: Path) -> None:
        lifecycle = fake_lifecycle()
        pipeline = make_pipeline(_mock_store(), description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(1))

        await pipeline.ingest_file(
            tmp_path / "doc00.md", _COLLECTION, embedder=pipeline._global_embedder, finalize=False
        )

        lifecycle.request_generation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_directory_still_asks_exactly_once(self, tmp_path: Path) -> None:
        lifecycle = fake_lifecycle()
        pipeline = make_pipeline(_mock_store(), description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(3))

        await pipeline.ingest_directory(tmp_path, _COLLECTION, embedder=pipeline._global_embedder)

        lifecycle.request_generation.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_without_a_lifecycle_nothing_extra_is_written(self, tmp_path: Path) -> None:
        """S19: the lifecycle is None when descriptions are off; the single-file path stays exactly as it was."""
        store = _mock_store()
        pipeline = make_pipeline(store)
        _write_docs(tmp_path, range(1))

        result = await pipeline.ingest_file(tmp_path / "doc00.md", _COLLECTION, embedder=pipeline._global_embedder)

        assert result.status == "ok"
        store.touch_last_indexed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_sync_style_call_recomputes_before_the_request(self, tmp_path: Path) -> None:
        """Without the inline recompute of ``rebuild_fts=True`` the shared routine does it, and before the request."""
        events: list[str] = []
        lifecycle = fake_lifecycle()
        lifecycle.request_generation = AsyncMock(side_effect=lambda *a: events.append("request"))
        store = _mock_store()
        store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=1, needs_recompute=True))
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        pipeline.recompute_collection_meta = AsyncMock(side_effect=lambda *a, **k: events.append("recompute"))  # type: ignore[method-assign]
        _write_docs(tmp_path, range(1))

        await pipeline.ingest_file(
            tmp_path / "doc00.md", _COLLECTION, embedder=pipeline._global_embedder, rebuild_fts=False
        )

        assert events == ["recompute", "request"]

    @pytest.mark.asyncio
    async def test_a_store_that_stays_busy_after_the_delete_requests_nothing(self, tmp_path: Path) -> None:
        lifecycle = fake_lifecycle()
        store = _mock_store()
        store.ingest_chunks = AsyncMock(side_effect=StoreBusyError(timeout_s=0.1))
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(1))

        result = await pipeline.ingest_file(tmp_path / "doc00.md", _COLLECTION, embedder=pipeline._global_embedder)

        assert result.status == "error"
        lifecycle.request_generation.assert_not_awaited()
        store.touch_last_indexed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_finalize_never_fails_the_ingest(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        lifecycle = fake_lifecycle()
        store = _mock_store()
        store.touch_last_indexed = AsyncMock(side_effect=OSError("disk gone"))
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(1))

        with caplog.at_level(logging.WARNING, logger="archon_search.pipeline"):
            result = await pipeline.ingest_file(tmp_path / "doc00.md", _COLLECTION, embedder=pipeline._global_embedder)

        assert result.status == "ok"
        store.touch_last_indexed.assert_awaited_once()
        assert any("Could not settle collection" in record.getMessage() for record in caplog.records)


class TestDocumentDelete:
    @pytest.mark.asyncio
    async def test_a_delete_requests_one_ingest_kind_generation(self) -> None:
        lifecycle = fake_lifecycle()
        store = _mock_store(CollectionMeta(name=_COLLECTION, doc_count=5, chunk_count=5))
        store.delete_document = AsyncMock(return_value=2)
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)

        deleted = await pipeline.delete_document("a" * 64, _COLLECTION, namespace="tenantA")

        assert deleted == 2
        lifecycle.request_generation.assert_awaited_once_with(_COLLECTION, "tenantA", GenerationKind.INGEST)

    @pytest.mark.asyncio
    async def test_a_delete_that_removed_nothing_requests_nothing(self) -> None:
        lifecycle = fake_lifecycle()
        store = _mock_store(CollectionMeta(name=_COLLECTION, doc_count=5, chunk_count=5))
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)

        assert await pipeline.delete_document("a" * 64, _COLLECTION) == 0

        lifecycle.request_generation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_flagged_row_is_recomputed_before_the_request(self) -> None:
        events: list[str] = []
        lifecycle = fake_lifecycle()
        lifecycle.request_generation = AsyncMock(side_effect=lambda *a: events.append("request"))
        store = _mock_store(CollectionMeta(name=_COLLECTION, doc_count=5, chunk_count=5, needs_recompute=True))
        store.delete_document = AsyncMock(return_value=1)
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        pipeline.recompute_collection_meta = AsyncMock(side_effect=lambda *a, **k: events.append("recompute"))  # type: ignore[method-assign]

        await pipeline.delete_document("a" * 64, _COLLECTION)

        assert events == ["recompute", "request"]

    @pytest.mark.asyncio
    async def test_an_unflagged_row_is_not_recomputed(self) -> None:
        lifecycle = fake_lifecycle()
        store = _mock_store(CollectionMeta(name=_COLLECTION, doc_count=5, chunk_count=5))
        store.delete_document = AsyncMock(return_value=1)
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        pipeline.recompute_collection_meta = AsyncMock()  # type: ignore[method-assign]

        await pipeline.delete_document("a" * 64, _COLLECTION)

        pipeline.recompute_collection_meta.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_a_lifecycle_a_delete_reads_no_extra_row(self) -> None:
        store = _mock_store(CollectionMeta(name=_COLLECTION, doc_count=5, chunk_count=5))
        store.delete_document = AsyncMock(return_value=1)
        pipeline = make_pipeline(store)

        await pipeline.delete_document("a" * 64, _COLLECTION)

        assert store.get_collection_meta.await_count == 1  # the existence check only
        store.touch_last_indexed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expiry_pruning_recomputes_before_it_asks(self) -> None:
        """Expiry removes chunks without touching the row: ``counts_stale`` forces the recompute (the row may say
        nothing is due) without a row read."""
        events: list[str] = []
        lifecycle = fake_lifecycle()
        lifecycle.request_generation = AsyncMock(side_effect=lambda *a: events.append("request"))
        store = _mock_store(CollectionMeta(name=_COLLECTION, doc_count=5, chunk_count=5))
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        pipeline.recompute_collection_meta = AsyncMock(side_effect=lambda *a, **k: events.append("recompute"))  # type: ignore[method-assign]

        await pipeline.finalize_after_removal(_COLLECTION, _NAMESPACE, True)

        assert events == ["recompute", "request"]
        assert pipeline.recompute_collection_meta.await_args.kwargs["force"] is True
        store.get_collection_meta.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_finalize_never_fails_the_delete(self, caplog: pytest.LogCaptureFixture) -> None:
        """The chunks are gone already: a store failure while settling the row is logged, the count still comes back."""
        lifecycle = fake_lifecycle()
        store = _mock_store(CollectionMeta(name=_COLLECTION, doc_count=5, chunk_count=5))
        store.delete_document = AsyncMock(return_value=2)
        store.touch_last_indexed = AsyncMock(side_effect=OSError("disk gone"))
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)

        with caplog.at_level(logging.WARNING, logger="archon_search.pipeline"):
            assert await pipeline.delete_document("a" * 64, _COLLECTION) == 2

        store.touch_last_indexed.assert_awaited_once()
        lifecycle.request_generation.assert_not_awaited()
        assert any("Could not settle collection" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("row", [OSError("disk gone"), None], ids=["unreadable-row", "row-gone"])
    async def test_an_unreadable_or_missing_row_after_a_removal_requests_nothing_and_raises_nothing(
        self, row: OSError | None
    ) -> None:
        lifecycle = fake_lifecycle()
        store = _mock_store()
        store.get_collection_meta = AsyncMock(side_effect=row) if row else AsyncMock(return_value=None)
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        pipeline.recompute_collection_meta = AsyncMock()  # type: ignore[method-assign]

        await pipeline.finalize_after_removal(_COLLECTION, _NAMESPACE)

        pipeline.recompute_collection_meta.assert_not_awaited()
        if row is None:
            lifecycle.request_generation.assert_awaited_once()
        else:
            lifecycle.request_generation.assert_not_awaited()


@pytest.mark.integration
class TestAgainstARealStore:
    @pytest.mark.asyncio
    async def test_first_single_file_returns_before_the_provider_finishes(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        release = asyncio.Event()
        generator = ScriptedGenerator("A single file collection.", gate=release)
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(1))
        try:
            result = await asyncio.wait_for(
                pipeline.ingest_file(tmp_path / "doc00.md", col_name, embedder=pipeline._global_embedder),
                timeout=_WAIT_TIMEOUT_S,
            )

            assert result.status == "ok"
            await asyncio.wait_for(generator.started.wait(), timeout=_WAIT_TIMEOUT_S)
            assert tickets.outcomes == [RequestOutcome.ACCEPTED]
            assert not tickets.tickets[0].settled.done()
            release.set()
            assert await tickets.generation_results() == [GenerationResult.GENERATED]
            meta = await connected_store.get_collection_meta(col_name)
            assert meta is not None
            assert (meta.description, meta.description_source, meta.description_status) == (
                "A single file collection.",
                "generated",
                "ok",
            )
            assert meta.described_at_doc_count == 1
        finally:
            release.set()
            await lifecycle.drain()

    @pytest.mark.asyncio
    async def test_single_file_ingests_follow_the_twenty_percent_rule(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        """Ten described documents: the 11th is +10% (no request effect), the 12th is +20% (regenerates)."""
        _write_docs(tmp_path, range(_BASELINE_DOC_COUNT + 2))
        first, first_tickets = recorded_lifecycle(connected_store, ScriptedGenerator("First text."))
        pipeline = make_pipeline(connected_store, description_lifecycle=first)
        for index in range(_BASELINE_DOC_COUNT):
            await pipeline.ingest_file(
                tmp_path / f"doc{index:02d}.md", col_name, embedder=pipeline._global_embedder, finalize=False
            )
        await pipeline.finalize_collection_change(col_name, _NAMESPACE, recompute=True)
        assert await first_tickets.generation_results() == [GenerationResult.GENERATED]

        generator = ScriptedGenerator("Second text.")
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)
        for index in (_BASELINE_DOC_COUNT, _BASELINE_DOC_COUNT + 1):
            await pipeline.ingest_file(
                tmp_path / f"doc{index:02d}.md", col_name, embedder=pipeline._global_embedder
            )
        await tickets.settle()

        assert tickets.outcomes == [RequestOutcome.NOT_REQUIRED, RequestOutcome.ACCEPTED]
        assert generator.calls == [(col_name, _NAMESPACE)]
        meta = await connected_store.get_collection_meta(col_name)
        assert meta is not None
        assert (meta.doc_count, meta.description, meta.described_at_doc_count) == (
            _BASELINE_DOC_COUNT + 2,
            "Second text.",
            _BASELINE_DOC_COUNT + 2,
        )

    @pytest.mark.asyncio
    async def test_reingesting_the_only_file_is_judged_on_recomputed_counts(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        """A single-file re-ingest of the only document empties the incremental counts (0 chunks) until the recompute.
        The collection was never described (its first generation failed, so there is no baseline): asked on the stale
        row the rule sees no chunks and refuses; asked after the recompute it sees one document and generates."""
        _write_docs(tmp_path, range(1))
        first, first_tickets = recorded_lifecycle(connected_store, ScriptedGenerator(None))
        pipeline = make_pipeline(connected_store, description_lifecycle=first)
        await pipeline.ingest_file(tmp_path / "doc00.md", col_name, embedder=pipeline._global_embedder)
        assert await first_tickets.generation_results() == [GenerationResult.FAILED]

        generator = ScriptedGenerator("Second text.")
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)
        await pipeline.ingest_file(
            tmp_path / "doc00.md", col_name, embedder=pipeline._global_embedder, rebuild_fts=False
        )
        await tickets.settle()

        meta = await connected_store.get_collection_meta(col_name)
        assert meta is not None
        assert (meta.doc_count, meta.needs_recompute) == (1, False)
        assert tickets.outcomes == [RequestOutcome.ACCEPTED]
        assert meta.description == "Second text."

    @pytest.mark.asyncio
    async def test_two_single_file_ingests_while_the_first_run_is_parked_make_one_description(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        """S24 on the single-file path: the second request settles ``alreadyRunning`` and one description results."""
        release = asyncio.Event()
        generator = ScriptedGenerator("One description.", gate=release)
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(2))
        try:
            await pipeline.ingest_file(tmp_path / "doc00.md", col_name, embedder=pipeline._global_embedder)
            await asyncio.wait_for(generator.started.wait(), timeout=_WAIT_TIMEOUT_S)
            await pipeline.ingest_file(tmp_path / "doc01.md", col_name, embedder=pipeline._global_embedder)

            assert tickets.outcomes == [RequestOutcome.ACCEPTED, RequestOutcome.ALREADY_RUNNING]
            release.set()
            assert await tickets.generation_results() == [GenerationResult.GENERATED, GenerationResult.ALREADY_RUNNING]
            assert generator.calls == [(col_name, _NAMESPACE)]
        finally:
            release.set()
            await lifecycle.drain()

    @pytest.mark.asyncio
    async def test_a_manual_description_stays_silent_on_a_single_file(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        await connected_store.update_collection_meta(
            CollectionMeta(
                name=col_name, description="Written by hand.", description_source="manual", description_status="ok"
            )
        )
        generator = ScriptedGenerator("Must not be written.")
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(1))

        await pipeline.ingest_file(tmp_path / "doc00.md", col_name, embedder=pipeline._global_embedder)

        assert tickets.outcomes == [RequestOutcome.REFUSED_MANUAL]
        assert generator.calls == []
        meta = await connected_store.get_collection_meta(col_name)
        assert meta is not None and meta.description == "Written by hand."

    @pytest.mark.asyncio
    async def test_without_a_lifecycle_a_single_file_settles_nothing(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        pipeline = make_pipeline(connected_store)
        _write_docs(tmp_path, range(1))

        with patch.object(SearchPipeline, "finalize_collection_change", AsyncMock(side_effect=AssertionError)) as final:
            result = await pipeline.ingest_file(tmp_path / "doc00.md", col_name, embedder=pipeline._global_embedder)

        assert result.status == "ok"
        final.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("removed", "requested"),
        [pytest.param(1, False, id="shrank-by-10-percent"), pytest.param(2, True, id="shrank-by-20-percent")],
    )
    async def test_deleting_documents_follows_the_twenty_percent_rule(
        self, connected_store, col_name, tmp_path: Path, removed: int, requested: bool
    ) -> None:
        _write_docs(tmp_path, range(_BASELINE_DOC_COUNT))
        first, first_tickets = recorded_lifecycle(connected_store, ScriptedGenerator("First text."))
        pipeline = make_pipeline(connected_store, description_lifecycle=first)
        initial = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)
        assert await first_tickets.generation_results() == [GenerationResult.GENERATED]

        generator = ScriptedGenerator("Second text.")
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)
        for result in initial[:removed]:
            assert await pipeline.delete_document(result.doc_id, col_name) > 0
        await tickets.settle()

        meta = await connected_store.get_collection_meta(col_name)
        assert meta is not None
        assert meta.doc_count == _BASELINE_DOC_COUNT - removed
        assert bool(generator.calls) is requested
        assert meta.description == ("Second text." if requested else "First text.")

    @pytest.mark.asyncio
    async def test_deleting_a_document_of_a_manual_collection_stays_silent(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        _write_docs(tmp_path, range(2))
        plain = make_pipeline(connected_store)
        initial = await plain.ingest_directory(tmp_path, col_name, embedder=plain._global_embedder)
        meta = await connected_store.get_collection_meta(col_name)
        assert meta is not None
        await connected_store.update_collection_meta(
            CollectionMeta(
                name=col_name,
                doc_count=meta.doc_count,
                chunk_count=meta.chunk_count,
                description="By hand.",
                description_source="manual",
                description_status="ok",
            )
        )
        generator = ScriptedGenerator("Must not be written.")
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)

        await pipeline.delete_document(initial[0].doc_id, col_name)

        assert tickets.outcomes == [RequestOutcome.REFUSED_MANUAL]
        assert generator.calls == []


@pytest.mark.integration
class TestIncrementalSync:
    @staticmethod
    def _syncer(store, lifecycle, tmp_path: Path) -> SearchCollectionSync:  # type: ignore[no-untyped-def]
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        return SearchCollectionSync(pipeline, state_store=IndexingStateStore(tmp_path / "state"))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("added", "removed", "requested"),
        [
            pytest.param(2, 0, True, id="grew-by-20-percent"),
            pytest.param(1, 0, False, id="grew-by-10-percent"),
            pytest.param(0, 2, True, id="shrank-by-20-percent"),
            pytest.param(0, 1, False, id="shrank-by-10-percent"),
        ],
    )
    async def test_a_sync_that_changes_files_follows_the_twenty_percent_rule(
        self, connected_store, col_name, tmp_path: Path, added: int, removed: int, requested: bool
    ) -> None:
        source = tmp_path / col_name
        source.mkdir()
        _write_docs(source, range(_BASELINE_DOC_COUNT))
        first, first_tickets = recorded_lifecycle(connected_store, ScriptedGenerator("First text."))
        syncer = self._syncer(connected_store, first, tmp_path)
        assert (await syncer.sync([str(source)])).errors == []
        assert await first_tickets.generation_results() == [GenerationResult.GENERATED]

        for gone in sorted(source.glob("*.md"))[:removed]:
            gone.unlink()
        _write_docs(source, range(_BASELINE_DOC_COUNT, _BASELINE_DOC_COUNT + added))
        generator = ScriptedGenerator("Second text.")
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        second = self._syncer(connected_store, lifecycle, tmp_path)
        result = await second.sync([str(source)])
        await tickets.settle()

        assert result.errors == [] and result.updated == [path_to_collection_name(str(source))]
        assert tickets.outcomes == [RequestOutcome.ACCEPTED if requested else RequestOutcome.NOT_REQUIRED]
        assert bool(generator.calls) is requested
        meta = await connected_store.get_collection_meta(path_to_collection_name(str(source)))
        assert meta is not None
        expected = _BASELINE_DOC_COUNT + added - removed
        assert meta.doc_count == expected
        assert meta.described_at_doc_count == (expected if requested else _BASELINE_DOC_COUNT)

    @pytest.mark.asyncio
    async def test_a_sync_that_changes_nothing_requests_nothing(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        source = tmp_path / col_name
        source.mkdir()
        _write_docs(source, range(3))
        first, first_tickets = recorded_lifecycle(connected_store, ScriptedGenerator("First text."))
        await self._syncer(connected_store, first, tmp_path).sync([str(source)])
        await first_tickets.settle()

        lifecycle, tickets = recorded_lifecycle(connected_store, ScriptedGenerator("Unused."))
        await self._syncer(connected_store, lifecycle, tmp_path).sync([str(source)])

        assert tickets.outcomes == []

    @pytest.mark.asyncio
    async def test_watch_triggered_sync_requests_after_the_change(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        source = tmp_path / col_name
        source.mkdir()
        _write_docs(source, range(1))
        first, first_tickets = recorded_lifecycle(connected_store, ScriptedGenerator("First text."))
        await self._syncer(connected_store, first, tmp_path).sync([str(source)])
        assert await first_tickets.generation_results() == [GenerationResult.GENERATED]

        _write_docs(source, range(1, 2))
        generator = ScriptedGenerator("Second text.")
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        await self._syncer(connected_store, lifecycle, tmp_path).sync_collection(
            path_to_collection_name(str(source)), source
        )
        await tickets.settle()

        assert tickets.outcomes == [RequestOutcome.ACCEPTED]
        meta = await connected_store.get_collection_meta(path_to_collection_name(str(source)))
        assert meta is not None and meta.description == "Second text." and meta.doc_count == 2

    @pytest.mark.asyncio
    async def test_a_sync_whose_files_all_fail_requests_nothing(self, connected_store, col_name, tmp_path: Path) -> None:
        source = tmp_path / col_name
        source.mkdir()
        _write_docs(source, range(3))
        first, first_tickets = recorded_lifecycle(connected_store, ScriptedGenerator("First text."))
        await self._syncer(connected_store, first, tmp_path).sync([str(source)])
        await first_tickets.settle()

        _write_docs(source, range(3, 6))
        lifecycle, tickets = recorded_lifecycle(connected_store, ScriptedGenerator("Unused."))
        syncer = self._syncer(connected_store, lifecycle, tmp_path)
        failed = IngestResult(doc_id="", chunks_created=0, status="error", error="parse failed")
        ingest = AsyncMock(return_value=failed)
        with patch.object(syncer._pipeline, "ingest_file", ingest):
            await syncer.sync([str(source)])

        assert ingest.await_count == 3  # the sync did see the three new files
        assert tickets.outcomes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("touch_existing", "recompute"),
        [pytest.param(True, True, id="re-ingest-of-the-only-file-leaves-the-row-flagged"),
         pytest.param(False, False, id="a-new-file-keeps-the-counts")],
    )
    async def test_the_sync_recomputes_a_flagged_row_before_it_asks(
        self, connected_store, col_name, tmp_path: Path, touch_existing: bool, recompute: bool
    ) -> None:
        source = tmp_path / col_name
        source.mkdir()
        _write_docs(source, range(1))
        first, first_tickets = recorded_lifecycle(connected_store, ScriptedGenerator("First text."))
        await self._syncer(connected_store, first, tmp_path).sync([str(source)])
        await first_tickets.settle()

        if touch_existing:
            only = next(source.glob("*.md"))
            only.write_text(only.read_text() + "Changed content.\n")
            os.utime(only, (time.time() + 5, time.time() + 5))
        else:
            _write_docs(source, range(1, 2))
        lifecycle, tickets = recorded_lifecycle(connected_store, ScriptedGenerator("Second text."))
        syncer = self._syncer(connected_store, lifecycle, tmp_path)
        finalize = AsyncMock(wraps=syncer._pipeline.finalize_collection_change)
        with patch.object(syncer._pipeline, "finalize_collection_change", finalize):
            await syncer.sync([str(source)])
        await tickets.settle()

        finalize.assert_awaited_once()
        assert finalize.await_args.kwargs["recompute"] is recompute
        meta = await connected_store.get_collection_meta(path_to_collection_name(str(source)))
        assert meta is not None
        assert (meta.needs_recompute, meta.doc_count) == (False, 1 if touch_existing else 2)
