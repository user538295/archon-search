"""BE-24 (C5, S24, S25) — the ingest asks for a description and goes on; it never generates one itself.

``SearchPipeline._finalize_batch_ingest`` refreshes the collection's counts first, then asks the injected lifecycle
(``GenerationKind.INGEST``), which reads the row, applies the 20% rule and runs the model call in a background task the
ingest never awaits. A pipeline with no lifecycle (eval, installer, CLI, a disabled ``[description]``) asks nothing.
The ingest's own row write (``last_indexed``) is a guarded one: it cannot overwrite a description written meanwhile.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._types import IngestResult
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.description_generator import DescriptionGenerator
from archon_search.description_lifecycle import (
    GENERATION_TASK_NAME_PREFIX,
    DescriptionLifecycle,
    GenerationKind,
    GenerationResult,
    RequestOutcome,
)
from archon_search.description_write import DescriptionWriteOutcome
from archon_search.pipeline import SearchPipeline
from archon_search.store import ChunkIngestResult, StoreBusyError
from tests._description_stubs import (
    SETTLE_TIMEOUT_S,
    ScriptedGenerator,
    fake_lifecycle,
    recorded_lifecycle,
)
from tests._parked_lock import until_parked

from .conftest import make_pipeline

_NAMESPACE = "default"
_BASELINE_DOC_COUNT = 10
_WAIT_TIMEOUT_S = SETTLE_TIMEOUT_S
_OLD_TEXT = "The description that was there before."
_DOC_BODY = "# Doc {index}\n\nContent number {index}.\n" * 5


def _write_docs(directory: Path, indexes: range) -> None:
    for index in indexes:
        (directory / f"doc{index:02d}.md").write_text(_DOC_BODY.format(index=index))


async def _ingest_one(pipeline: SearchPipeline, directory: Path) -> list[IngestResult]:
    _write_docs(directory, range(1))
    return await pipeline.ingest_directory(directory, "my-col", embedder=pipeline._global_embedder)


def _mock_store(existing_meta: CollectionMeta | None = None) -> MagicMock:
    store = MagicMock()
    store.assert_collection_access = AsyncMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=1, needs_recompute=False))
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=existing_meta)
    store.touch_last_indexed = AsyncMock()
    store.write_description = AsyncMock(return_value=DescriptionWriteOutcome.WRITTEN)
    store.update_collection_meta = AsyncMock()
    store._config = SearchConfig()
    return store


class TestWhatTheIngestAsks:
    @pytest.mark.asyncio
    async def test_batch_ingest_requests_one_ingest_kind_generation_for_its_collection(self, tmp_path: Path) -> None:
        lifecycle = fake_lifecycle()
        pipeline = make_pipeline(_mock_store(), description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(3))

        await pipeline.ingest_directory(tmp_path, "my-col", namespace="tenantA", embedder=pipeline._global_embedder)

        lifecycle.request_generation.assert_awaited_once_with("my-col", "tenantA", GenerationKind.INGEST)

    @pytest.mark.asyncio
    async def test_failed_batch_requests_nothing(self, tmp_path: Path) -> None:
        lifecycle = fake_lifecycle()
        store = _mock_store()
        store.delete_document = AsyncMock(side_effect=StoreBusyError(timeout_s=0.1))
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(2))

        results = await pipeline.ingest_directory(tmp_path, "my-col", embedder=pipeline._global_embedder)

        assert {r.status for r in results} == {"error"}
        lifecycle.request_generation.assert_not_awaited()
        store.touch_last_indexed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_documents_mode_requests_the_same_generation(self) -> None:
        lifecycle = fake_lifecycle()
        pipeline = make_pipeline(_mock_store(), description_lifecycle=lifecycle)

        await pipeline.ingest_documents(
            [{"text": "# Hello\n\nInline text.\n" * 5, "source_path": "inline/a.md"}],
            "my-col",
            embedder=pipeline._global_embedder,
        )

        lifecycle.request_generation.assert_awaited_once_with("my-col", _NAMESPACE, GenerationKind.INGEST)

    @pytest.mark.asyncio
    async def test_ingest_does_not_touch_the_description_or_read_a_sample(self, tmp_path: Path) -> None:
        """Only the lifecycle's task reads the sample and writes the text; the ingest writes ``last_indexed`` alone."""
        lifecycle = fake_lifecycle()
        store = _mock_store()
        store.list_chunk_ids = AsyncMock(return_value=["chunk"])
        store.read_excerpts = AsyncMock(return_value=["text"])
        store.list_chunks_raw = AsyncMock()
        store.write_description = AsyncMock()
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(2))

        await pipeline.ingest_directory(tmp_path, "my-col", embedder=pipeline._global_embedder)

        for untouched in (store.list_chunk_ids, store.read_excerpts, store.list_chunks_raw, store.write_description):
            untouched.assert_not_awaited()
        store.update_collection_meta.assert_not_awaited()
        store.touch_last_indexed.assert_awaited_once()
        assert store.touch_last_indexed.await_args.args[0] == "my-col"
        assert store.touch_last_indexed.await_args.kwargs["namespace"] == _NAMESPACE

    @pytest.mark.asyncio
    async def test_recompute_runs_before_generation_is_requested(self, tmp_path: Path) -> None:
        """The lifecycle reads the row itself, so the counts must be right by the time it is asked."""
        events: list[str] = []
        lifecycle = fake_lifecycle()
        lifecycle.request_generation = AsyncMock(side_effect=lambda *a: events.append("request"))
        store = _mock_store()
        store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=1, needs_recompute=True))
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        pipeline.recompute_collection_meta = AsyncMock(side_effect=lambda *a, **k: events.append("recompute"))  # type: ignore[method-assign]
        _write_docs(tmp_path, range(1))

        await pipeline.ingest_directory(tmp_path, "my-col", embedder=pipeline._global_embedder)

        assert events == ["recompute", "request"]

    @pytest.mark.asyncio
    async def test_a_lifecycle_that_cannot_read_the_row_never_fails_the_ingest(self, tmp_path: Path) -> None:
        """``request_generation`` answers ``unreadable`` instead of raising when the row cannot be read (C5)."""
        broken = _mock_store()
        broken.get_collection_meta = AsyncMock(side_effect=OSError("disk gone"))
        lifecycle, tickets = recorded_lifecycle(broken, ScriptedGenerator("unused"))
        pipeline = make_pipeline(_mock_store(), description_lifecycle=lifecycle)

        results = await _ingest_one(pipeline, tmp_path)

        assert [r.status for r in results] == ["ok"]
        assert tickets.outcomes == [RequestOutcome.UNREADABLE]
        assert not lifecycle.is_pending("my-col")

    @pytest.mark.asyncio
    async def test_a_batch_with_one_failed_file_still_requests_generation(self, tmp_path: Path) -> None:
        lifecycle = fake_lifecycle()
        store = _mock_store()
        attempts: list[int] = []

        async def busy_on_the_first_delete(*args: object, **kwargs: object) -> int:
            attempts.append(1)
            if len(attempts) == 1:
                raise StoreBusyError(timeout_s=0.1)
            return 0

        store.delete_document = AsyncMock(side_effect=busy_on_the_first_delete)
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(2))

        results = await pipeline.ingest_directory(tmp_path, "my-col", embedder=pipeline._global_embedder)

        assert sorted(r.status for r in results) == ["error", "ok"]
        lifecycle.request_generation.assert_awaited_once_with("my-col", _NAMESPACE, GenerationKind.INGEST)

    def test_the_force_flag_and_the_generator_parameter_are_gone(self) -> None:
        for method in (SearchPipeline.__init__, SearchPipeline.ingest_directory, SearchPipeline._finalize_batch_ingest):
            parameters = inspect.signature(method).parameters
            assert "force_regenerate_description" not in parameters
            assert "description_generator" not in parameters
        assert "description_lifecycle" in inspect.signature(SearchPipeline.__init__).parameters


class TestManualDescription:
    @pytest.mark.asyncio
    async def test_manual_source_collection_is_not_marked_pending(self, tmp_path: Path) -> None:
        """The rule does not enqueue for a manual description: no pending overlay, no model call, nothing written. The
        baseline is unset, so only the manual check keeps the ingest rule from generating."""
        manual = CollectionMeta(
            name="my-col",
            description=_OLD_TEXT,
            description_source="manual",
            description_status="ok",
            doc_count=1,
            chunk_count=3,
            described_at_doc_count=None,
            last_described=datetime(2026, 1, 1, tzinfo=UTC),
        )
        store = _mock_store(existing_meta=manual)
        generator = ScriptedGenerator("Generated text that must not be written")
        lifecycle, tickets = recorded_lifecycle(store, generator)
        pipeline = make_pipeline(store, description_lifecycle=lifecycle)

        results = await _ingest_one(pipeline, tmp_path)

        assert [r.status for r in results] == ["ok"]
        assert tickets.outcomes == [RequestOutcome.REFUSED_MANUAL]
        assert not lifecycle.is_pending("my-col")
        assert generator.calls == []
        store.write_description.assert_not_awaited()
        assert lifecycle.overlay_status("my-col", "ok") == "ok"


@pytest.mark.integration
class TestAgainstARealStore:
    @pytest.mark.asyncio
    async def test_ingest_returns_before_the_provider_finishes(self, connected_store, col_name, tmp_path: Path) -> None:
        """With a parked provider the ingest result is already returned: the row is pending, not described."""
        release = asyncio.Event()
        generator = ScriptedGenerator("A fine collection.", gate=release)
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(3))
        try:
            results = await asyncio.wait_for(
                pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder),
                timeout=_WAIT_TIMEOUT_S,
            )

            assert [r.status for r in results] == ["ok", "ok", "ok"]
            await asyncio.wait_for(generator.started.wait(), timeout=_WAIT_TIMEOUT_S)
            assert tickets.outcomes == [RequestOutcome.ACCEPTED]
            assert not tickets.tickets[0].settled.done(), "the model call has not returned, yet the ingest has"
            assert lifecycle.is_pending(col_name)
            parked = await connected_store.get_collection_meta(col_name)
            assert parked is not None and parked.description is None and parked.last_indexed is not None

            release.set()
            assert await asyncio.wait_for(tickets.generation_results(), timeout=_WAIT_TIMEOUT_S) == [
                GenerationResult.GENERATED
            ]
            described = await connected_store.get_collection_meta(col_name)
            assert described is not None
            assert described.description == "A fine collection."
            assert described.described_at_doc_count == 3
            assert described.description_source == "generated"
            assert not lifecycle.is_pending(col_name)
        finally:
            release.set()
            await lifecycle.drain()

    @pytest.mark.asyncio
    async def test_ingest_without_lifecycle_requests_no_generation(self, connected_store, col_name, tmp_path: Path) -> None:
        """Eval and installer pipelines have no lifecycle: nothing is requested, generated or built, and no description
        or status appears. Every entry into generation is armed to fail the test if it is reached."""
        pipeline = make_pipeline(connected_store)
        _write_docs(tmp_path, range(3))

        with (
            patch.object(DescriptionLifecycle, "request_generation", AsyncMock(side_effect=AssertionError)) as request,
            patch.object(DescriptionGenerator, "generate", AsyncMock(side_effect=AssertionError)) as generate,
            patch("archon_search.server.llm_composition.build_llm_client", side_effect=AssertionError) as build_client,
        ):
            results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)

        assert [r.status for r in results] == ["ok", "ok", "ok"]
        for entry in (request, generate, build_client):
            entry.assert_not_called()
        assert pipeline._description_lifecycle is None
        assert not [t for t in asyncio.all_tasks() if t.get_name().startswith(GENERATION_TASK_NAME_PREFIX)]
        meta = await connected_store.get_collection_meta(col_name)
        assert meta is not None
        assert meta.description is None and meta.description_status is None and meta.last_described is None
        assert meta.doc_count == 3 and meta.last_indexed is not None

    @pytest.mark.asyncio
    async def test_a_second_ingest_while_the_first_run_is_parked_does_not_start_another(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        """S24: single-flight at the pipeline level. The second batch's request settles ``alreadyRunning`` at once and
        the one parked run writes one description."""
        release = asyncio.Event()
        generator = ScriptedGenerator("One description.", gate=release)
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(3))
        try:
            await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)
            await asyncio.wait_for(generator.started.wait(), timeout=_WAIT_TIMEOUT_S)
            await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)

            assert tickets.outcomes == [RequestOutcome.ACCEPTED, RequestOutcome.ALREADY_RUNNING]
            release.set()
            results = await tickets.generation_results()

            assert results == [GenerationResult.GENERATED, GenerationResult.ALREADY_RUNNING]
            assert generator.calls == [(col_name, _NAMESPACE)]
            described = await connected_store.get_collection_meta(col_name)
            assert described is not None and described.description == "One description."
        finally:
            release.set()
            await lifecycle.drain()

    @pytest.mark.asyncio
    async def test_provider_failure_keeps_the_old_text_and_marks_the_row_failed(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        _write_docs(tmp_path, range(_BASELINE_DOC_COUNT))
        first, first_tickets = recorded_lifecycle(connected_store, ScriptedGenerator("Original description."))
        pipeline = make_pipeline(connected_store, description_lifecycle=first)
        await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)
        await first_tickets.settle()

        _write_docs(tmp_path, range(_BASELINE_DOC_COUNT, _BASELINE_DOC_COUNT + 3))
        failing = ScriptedGenerator(None)
        second, second_tickets = recorded_lifecycle(connected_store, failing)
        pipeline = make_pipeline(connected_store, description_lifecycle=second)
        await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)

        assert await second_tickets.generation_results() == [GenerationResult.FAILED]
        meta = await connected_store.get_collection_meta(col_name)
        assert meta is not None
        assert meta.description == "Original description."
        assert meta.description_status == "failed"
        assert meta.described_at_doc_count == _BASELINE_DOC_COUNT

    @pytest.mark.asyncio
    async def test_failed_first_generation_leaves_no_description_and_no_baseline(
        self, connected_store, col_name, tmp_path: Path
    ) -> None:
        lifecycle, tickets = recorded_lifecycle(connected_store, ScriptedGenerator(None))
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)
        _write_docs(tmp_path, range(1))

        await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)

        assert await tickets.generation_results() == [GenerationResult.FAILED]
        meta = await connected_store.get_collection_meta(col_name)
        assert meta is not None
        assert meta.description is None
        assert meta.described_at_doc_count is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("added", "removed", "requested"),
        [
            pytest.param(2, 0, True, id="grew-by-20-percent"),
            pytest.param(1, 0, False, id="grew-by-10-percent"),
            pytest.param(0, 3, True, id="shrank-by-30-percent"),
            pytest.param(0, 1, False, id="shrank-by-10-percent"),
            pytest.param(0, 0, False, id="unchanged"),
        ],
    )
    async def test_reingest_requests_generation_only_for_a_change_of_twenty_percent(
        self, connected_store, col_name, tmp_path: Path, added: int, removed: int, requested: bool
    ) -> None:
        """S24/S25 on a REAL store: a re-ingest deletes and re-adds documents, which zeroes the incremental centroid
        sum, so the row's counts are only right after the recompute; the rule must see the recomputed counts."""
        _write_docs(tmp_path, range(_BASELINE_DOC_COUNT))
        first, first_tickets = recorded_lifecycle(connected_store, ScriptedGenerator("First text."))
        pipeline = make_pipeline(connected_store, description_lifecycle=first)
        initial = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)
        assert await first_tickets.generation_results() == [GenerationResult.GENERATED]
        baseline = await connected_store.get_collection_meta(col_name)
        assert baseline is not None and baseline.described_at_doc_count == _BASELINE_DOC_COUNT

        for gone, result in zip(sorted(tmp_path.glob("*.md"))[:removed], initial[:removed], strict=True):
            gone.unlink()
            await connected_store.delete_document(col_name, result.doc_id)
        _write_docs(tmp_path, range(_BASELINE_DOC_COUNT, _BASELINE_DOC_COUNT + added))

        generator = ScriptedGenerator("Second text.")
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)
        await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)
        await tickets.settle()

        expected_docs = _BASELINE_DOC_COUNT + added - removed
        meta = await connected_store.get_collection_meta(col_name)
        assert meta is not None
        assert meta.doc_count == expected_docs
        assert tickets.outcomes == [RequestOutcome.ACCEPTED if requested else RequestOutcome.NOT_REQUIRED]
        assert bool(generator.calls) is requested
        assert meta.description == ("Second text." if requested else "First text.")
        assert meta.described_at_doc_count == (expected_docs if requested else _BASELINE_DOC_COUNT)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("added", "requested"), [(1, True), (0, False)], ids=["doubled", "unchanged"])
    async def test_reingest_of_a_one_document_collection_is_judged_on_its_recomputed_counts(
        self, connected_store, col_name, tmp_path: Path, added: int, requested: bool
    ) -> None:
        """The BE-22 observation: re-ingesting the only document empties the incremental counts (the delete takes the
        collection to zero chunks), so the row reads 0 documents and 0 chunks until the recompute. Asked before it, the
        20% rule saw an empty collection and never fired; asked after it, it sees one document become two."""
        _write_docs(tmp_path, range(1))
        first, first_tickets = recorded_lifecycle(connected_store, ScriptedGenerator("First text."))
        pipeline = make_pipeline(connected_store, description_lifecycle=first)
        await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)
        assert await first_tickets.generation_results() == [GenerationResult.GENERATED]

        _write_docs(tmp_path, range(1, 1 + added))
        generator = ScriptedGenerator("Second text.")
        lifecycle, tickets = recorded_lifecycle(connected_store, generator)
        pipeline = make_pipeline(connected_store, description_lifecycle=lifecycle)
        await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)
        await tickets.settle()

        meta = await connected_store.get_collection_meta(col_name)
        assert meta is not None
        assert (meta.doc_count, meta.needs_recompute) == (1 + added, False)
        assert tickets.outcomes == [RequestOutcome.ACCEPTED if requested else RequestOutcome.NOT_REQUIRED]
        assert bool(generator.calls) is requested
        assert meta.described_at_doc_count == (1 + added if requested else 1)

    @pytest.mark.asyncio
    async def test_last_indexed_write_cannot_overwrite_a_description_written_meanwhile(
        self, connected_store, col_name
    ) -> None:
        """The ingest's row write re-reads under the lock: a description that lands while it waits for the lock
        survives, and the stamp still lands."""
        await connected_store.update_collection_meta(CollectionMeta(name=col_name, doc_count=2, chunk_count=2))
        before = await connected_store.get_collection_meta(col_name, _NAMESPACE)
        assert before is not None
        stamp = datetime(2026, 7, 8, 9, 10, 11, tzinfo=UTC)
        lock = connected_store.lock_for(col_name)
        await lock.acquire()
        touch = asyncio.create_task(connected_store.touch_last_indexed(col_name, stamp, namespace=_NAMESPACE))
        try:
            await until_parked(lock)  # the stamp has read nothing yet: it waits for the lock the writer holds
            written = replace(
                before, description="Written meanwhile.", description_source="generated", description_status="ok"
            )
            await connected_store._do_write_meta_unlocked(connected_store._require_connected(), col_name, written)
        finally:
            lock.release()
        await asyncio.wait_for(touch, timeout=_WAIT_TIMEOUT_S)

        meta = await connected_store.get_collection_meta(col_name, _NAMESPACE)
        assert meta is not None
        assert meta.description == "Written meanwhile."
        assert (meta.description_source, meta.description_status) == ("generated", "ok")
        assert meta.last_indexed == stamp
        assert meta.doc_count == 2

    @pytest.mark.asyncio
    async def test_last_indexed_write_in_another_namespace_leaves_the_default_row_alone(
        self, connected_store, col_name
    ) -> None:
        stamp = datetime(2026, 7, 8, 9, 10, 11, tzinfo=UTC)
        await connected_store.update_collection_meta(CollectionMeta(name=col_name, namespace="tenantA", doc_count=1))

        await connected_store.touch_last_indexed(col_name, stamp, namespace="tenantA")
        await connected_store.touch_last_indexed(col_name, stamp, namespace="tenantB")

        owned = await connected_store.get_collection_meta(col_name, "tenantA")
        assert owned is not None and owned.last_indexed == stamp
        assert await connected_store.get_collection_meta(col_name, "tenantB") is None

    @pytest.mark.asyncio
    async def test_last_indexed_write_for_a_missing_collection_writes_nothing(self, connected_store, col_name) -> None:
        await connected_store.touch_last_indexed(col_name, datetime.now(UTC), namespace=_NAMESPACE)

        assert await connected_store.get_collection_meta(col_name) is None
