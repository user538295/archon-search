"""Tests for MaintenanceLoop orphan cleanup policy (BE-6).

Plan: Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md Task BE-6

TDD: tests written first, then _run_orphan_cleanup implementation in
archon_search/jobs/maintenance_loop.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.config import MaintenanceConfig
from archon_search.jobs.maintenance_loop import MaintenanceLoop


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


async def _async_iter(items: list) -> Any:
    """Return an async generator that yields items from a list."""
    for item in items:
        yield item


def _make_chunk(source_path: str, doc_id: str = "abc", chunk_id: str = "abc-001") -> dict:
    return {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "some text",
        "vector": [0.1, 0.2, 0.3, 0.4],
        "source_path": source_path,
        "indexed_at": "2025-01-01T00:00:00Z",
        "file_type": "txt",
        "language": "en",
        "metadata": "{}",
        "acl": None,
        "custom_score": None,
        "ingested_by": "test",
        "updated_at": "2025-01-01T00:00:00Z",
    }


def _make_loop(
    tmp_path: Path,
    *,
    interval_hours: int = 0,
    fts_optimize: bool = False,
    orphan_cleanup: bool = True,
    failed_ingest_retry: bool = False,
    retry_max_attempts: int = 3,
    retry_max_age_hours: int = 72,
    exclude: list[str] | None = None,
    search_store: Any = None,
) -> MaintenanceLoop:
    cfg = MaintenanceConfig(
        interval_hours=interval_hours,
        fts_optimize=fts_optimize,
        orphan_cleanup=orphan_cleanup,
        failed_ingest_retry=failed_ingest_retry,
        retry_max_attempts=retry_max_attempts,
        retry_max_age_hours=retry_max_age_hours,
        exclude=exclude or [],
    )
    js = MagicMock()
    ss = search_store if search_store is not None else MagicMock()
    loop = MaintenanceLoop(job_store=js, search_store=ss, config=cfg, data_dir=tmp_path)
    # Set an empty _current_health dict so policies can update it.
    loop._current_health = {  # type: ignore[attr-defined]
        "fts_optimized_at": None,
        "orphans_removed_last_run": 0,
        "last_retry_at": None,
        "last_error": None,
        "meta_chunk_count": 0,
    }
    return loop


# ---------------------------------------------------------------------------
# BE-6: orphan cleanup policy tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_cleanup_removes_deleted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S8: chunk whose source_path no longer exists → delete_by_source_path called once; count=1."""
    # Create then delete the file so Path.exists() returns False
    orphan_file = tmp_path / "gone.txt"
    orphan_file.write_text("temporary")
    orphan_file.unlink()

    chunk = _make_chunk(str(orphan_file), doc_id="doc1a", chunk_id="doc1a-000001")

    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock(return_value=_async_iter([chunk]))
    ss.delete_by_source_path = AsyncMock(return_value=1)
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock()

    loop = _make_loop(tmp_path, search_store=ss)

    await loop._run_orphan_cleanup("docs", "default")

    ss.delete_by_source_path.assert_called_once_with(
        "docs", str(orphan_file), namespace="default", skip_fts_optimize=True
    )
    assert loop._current_health["orphans_removed_last_run"] == 1


@pytest.mark.asyncio
async def test_orphan_cleanup_no_orphans(
    tmp_path: Path,
) -> None:
    """S9: all source files still exist → delete_by_source_path never called; count=0."""
    existing_file = tmp_path / "present.txt"
    existing_file.write_text("content")

    chunk = _make_chunk(str(existing_file))

    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock(return_value=_async_iter([chunk]))
    ss.delete_by_source_path = AsyncMock()
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock()

    loop = _make_loop(tmp_path, search_store=ss)
    await loop._run_orphan_cleanup("docs", "default")

    ss.delete_by_source_path.assert_not_called()
    assert loop._current_health["orphans_removed_last_run"] == 0


@pytest.mark.asyncio
async def test_orphan_cleanup_skips_url_source_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """S10: source_path starting with http:// or https:// → Path.exists() never called; chunk not deleted."""
    http_chunk = _make_chunk("http://example.com/doc.pdf")
    https_chunk = _make_chunk("https://example.com/other.pdf")

    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock(return_value=_async_iter([http_chunk, https_chunk]))
    ss.delete_by_source_path = AsyncMock()
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock()

    loop = _make_loop(tmp_path, search_store=ss)

    with patch("archon_search.jobs.maintenance_loop.Path") as mock_path_cls:
        with caplog.at_level(logging.DEBUG, logger="archon_search.jobs.maintenance_loop"):
            await loop._run_orphan_cleanup("docs", "default")

    # Path must never have been instantiated with URL strings (spec: Path.exists() never called).
    called_with_urls = [
        call for call in mock_path_cls.call_args_list
        if call.args and (
            str(call.args[0]).startswith("http://") or str(call.args[0]).startswith("https://")
        )
    ]
    assert called_with_urls == [], f"Path was called with URL source paths: {called_with_urls}"
    # Stronger: Path was never instantiated at all (URLs filtered before Phase 2)
    mock_path_cls.assert_not_called()
    ss.delete_by_source_path.assert_not_called()
    # DEBUG must be logged for each URL
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("http" in r.message.lower() or "url" in r.message.lower() for r in debug_records)


@pytest.mark.asyncio
async def test_orphan_cleanup_elapsed_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """S11: orphan scan takes > 60 s → WARNING logged with elapsed time."""
    orphan_file = tmp_path / "old_gone.txt"
    # File does not exist.

    chunk = _make_chunk(str(orphan_file))

    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock(return_value=_async_iter([chunk]))
    ss.delete_by_source_path = AsyncMock(return_value=1)
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock()

    loop = _make_loop(tmp_path, search_store=ss)

    # Monkeypatch time.monotonic so that elapsed time appears > 60 s.
    _ELAPSED_LIMIT_S = 60.0
    call_count = 0

    def _fake_monotonic() -> float:
        nonlocal call_count
        call_count += 1
        # First call (start): 0.0; subsequent calls: 65.0 (simulates > 60 s elapsed)
        return 0.0 if call_count == 1 else 65.0

    import archon_search.jobs.maintenance_loop as ml_mod

    with (
        caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"),
        patch.object(ml_mod, "time") as mock_time,
    ):
        mock_time.monotonic = _fake_monotonic
        await loop._run_orphan_cleanup("docs", "default")

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("60" in r.message or "elapsed" in r.message.lower() for r in warning_records), (
        f"Expected elapsed WARNING; got: {[r.message for r in warning_records]}"
    )


@pytest.mark.asyncio
async def test_orphan_cleanup_multi_chunk_multi_docid_single_source_path(
    tmp_path: Path,
) -> None:
    """S12: three chunks with two distinct doc_ids, same source_path →
    delete_by_source_path called exactly once; all chunks removed."""
    orphan_file = tmp_path / "shared.txt"
    # File does not exist.

    # Three chunks, two doc_ids, same source_path.
    chunk_a = _make_chunk(str(orphan_file), doc_id="doc-aaa", chunk_id="doc-aaa-000001")
    chunk_b = _make_chunk(str(orphan_file), doc_id="doc-aaa", chunk_id="doc-aaa-000002")
    chunk_c = _make_chunk(str(orphan_file), doc_id="doc-bbb", chunk_id="doc-bbb-000001")

    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock(return_value=_async_iter([chunk_a, chunk_b, chunk_c]))
    ss.delete_by_source_path = AsyncMock(return_value=3)
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock()

    loop = _make_loop(tmp_path, search_store=ss)
    await loop._run_orphan_cleanup("docs", "default")

    # Only one call per unique source_path.
    assert ss.delete_by_source_path.call_count == 1
    ss.delete_by_source_path.assert_called_once_with(
        "docs", str(orphan_file), namespace="default", skip_fts_optimize=True
    )
    assert loop._current_health["orphans_removed_last_run"] == 1


@pytest.mark.asyncio
async def test_orphan_cleanup_disabled_by_config(tmp_path: Path) -> None:
    """When orphan_cleanup=False, list_chunks_raw is never called."""
    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock()
    ss.delete_by_source_path = AsyncMock()

    loop = _make_loop(tmp_path, search_store=ss, orphan_cleanup=False)
    await loop._run_orphan_cleanup("docs", "default")

    ss.list_chunks_raw.assert_not_called()
    ss.delete_by_source_path.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_cleanup_no_chunks_in_collection(tmp_path: Path) -> None:
    """Empty collection → Path.exists never called; delete never called; count=0."""
    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock(return_value=_async_iter([]))
    ss.delete_by_source_path = AsyncMock()
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock()

    loop = _make_loop(tmp_path, search_store=ss)

    with patch("pathlib.Path.exists") as mock_exists:
        await loop._run_orphan_cleanup("docs", "default")
        mock_exists.assert_not_called()

    ss.delete_by_source_path.assert_not_called()
    assert loop._current_health["orphans_removed_last_run"] == 0


@pytest.mark.asyncio
async def test_orphan_cleanup_post_deletion_fts_optimize_called(
    tmp_path: Path,
) -> None:
    """After deletions, optimize_fts is called under a separate lock acquisition."""
    orphan_file = tmp_path / "orphan.txt"
    # Does not exist.

    chunk = _make_chunk(str(orphan_file))

    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock(return_value=_async_iter([chunk]))
    ss.delete_by_source_path = AsyncMock(return_value=1)
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock()

    # fts_optimize=False: documents that post-orphan FTS optimize is unconditional
    # (not governed by the fts_optimize config flag, which only controls the separate
    # _run_fts_optimize policy).
    loop = _make_loop(tmp_path, search_store=ss, fts_optimize=False)
    await loop._run_orphan_cleanup("docs", "default")

    # optimize_fts must be called once after all deletions regardless of fts_optimize flag.
    ss.optimize_fts.assert_called_once_with("docs")
    # Lock must be released after optimize.
    assert not lock.locked()


@pytest.mark.asyncio
async def test_orphan_cleanup_post_deletion_fts_optimize_not_called_if_no_orphans(
    tmp_path: Path,
) -> None:
    """optimize_fts is NOT called when there are no deletions."""
    existing_file = tmp_path / "exists.txt"
    existing_file.write_text("data")

    chunk = _make_chunk(str(existing_file))

    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock(return_value=_async_iter([chunk]))
    ss.delete_by_source_path = AsyncMock()
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock()

    # fts_optimize=False: documents that post-orphan FTS optimize is unconditional;
    # when there are no orphans the optimize is still skipped (because orphan_count == 0).
    loop = _make_loop(tmp_path, search_store=ss, fts_optimize=False)
    await loop._run_orphan_cleanup("docs", "default")

    ss.delete_by_source_path.assert_not_called()
    ss.optimize_fts.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_cleanup_post_deletion_fts_lock_timeout_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If lock acquisition for post-orphan FTS optimize times out → WARNING logged; no exception."""
    orphan_file = tmp_path / "orphan_lock.txt"
    # Does not exist.

    chunk = _make_chunk(str(orphan_file))

    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock(return_value=_async_iter([chunk]))
    ss.delete_by_source_path = AsyncMock(return_value=1)
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock()

    # fts_optimize=False: documents that post-orphan FTS optimize is unconditional.
    loop = _make_loop(tmp_path, search_store=ss, fts_optimize=False)

    import archon_search.jobs.maintenance_loop as ml_mod

    with (
        caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"),
        patch.object(ml_mod, "INGEST_LOCK_TIMEOUT_S", 0.05),
    ):
        # Hold the lock externally so the method times out.
        await lock.acquire()
        try:
            await loop._run_orphan_cleanup("docs", "default")  # must not raise
        finally:
            lock.release()

    # optimize_fts must not have been called.
    ss.optimize_fts.assert_not_called()
    # WARNING logged.
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("fts" in r.message.lower() or "lock" in r.message.lower() for r in warning_records), (
        f"Expected lock-timeout WARNING; got: {[r.message for r in warning_records]}"
    )
    # Lock must be released (we already released it in finally above)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_orphan_cleanup_delete_exception_continues_loop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """C1-I-1: delete_by_source_path raises on first orphan → WARNING logged; second orphan still processed."""
    orphan_file_a = tmp_path / "orphan_a.txt"
    orphan_file_b = tmp_path / "orphan_b.txt"
    # Neither file exists.

    chunk_a = _make_chunk(str(orphan_file_a), doc_id="doc-aaa", chunk_id="doc-aaa-000001")
    chunk_b = _make_chunk(str(orphan_file_b), doc_id="doc-bbb", chunk_id="doc-bbb-000001")

    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock(return_value=_async_iter([chunk_a, chunk_b]))
    # First call raises, second succeeds.
    ss.delete_by_source_path = AsyncMock(
        side_effect=[RuntimeError("db error"), None]
    )
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock()

    loop = _make_loop(tmp_path, search_store=ss)

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_orphan_cleanup("docs", "default")  # must not raise

    # delete_by_source_path called twice (loop continued after error).
    assert ss.delete_by_source_path.call_count == 2
    # Only the successful deletion is counted.
    assert loop._current_health["orphans_removed_last_run"] == 1
    # WARNING logged for the failed deletion.
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("delete_by_source_path" in r.message or "db error" in r.message for r in warning_records), (
        f"Expected WARNING for failed deletion; got: {[r.message for r in warning_records]}"
    )


@pytest.mark.asyncio
async def test_orphan_cleanup_post_deletion_fts_index_not_found_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """C1-I-9: optimize_fts raises FTSIndexNotFoundError after orphan deletion → WARNING; no exception; lock released."""
    from archon_search.store import FTSIndexNotFoundError

    orphan_file = tmp_path / "orphan_fts_err.txt"
    # Does not exist.

    chunk = _make_chunk(str(orphan_file))

    ss = AsyncMock()
    ss.list_chunks_raw = MagicMock(return_value=_async_iter([chunk]))
    ss.delete_by_source_path = AsyncMock(return_value=1)
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock(side_effect=FTSIndexNotFoundError("no index"))

    loop = _make_loop(tmp_path, search_store=ss, fts_optimize=False)

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_orphan_cleanup("docs", "default")  # must not raise

    # WARNING must be logged.
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "fts" in r.message.lower() or "no fts index" in r.message.lower()
        for r in warning_records
    ), f"Expected FTSIndexNotFoundError WARNING; got: {[r.message for r in warning_records]}"
    # Lock must be released.
    assert not lock.locked()
    # orphans_removed_last_run still set (deletion succeeded; only FTS failed).
    assert loop._current_health["orphans_removed_last_run"] == 1


# ---------------------------------------------------------------------------
# Integration test: real store
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_orphan_cleanup_real_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration: ingest file, delete file from disk, run _run_orphan_cleanup, assert chunks gone."""
    import gc
    import os

    from tests.integration.conftest import make_real_pipeline

    # Two separate tmp dirs for two make_real_pipeline instances to avoid DB state sharing.
    store_dir = tmp_path / "store1"
    store_dir.mkdir()

    store, pipeline = await make_real_pipeline(store_dir, monkeypatch)
    try:
        col = "orphan-test"
        ns = "default"

        # Create and ingest a real file.
        doc_file = tmp_path / "target.txt"
        doc_file.write_text(
            "Orphan cleanup integration test. " * 30,
            encoding="utf-8",
        )
        await pipeline.ingest_file(
            doc_file,
            col,
            namespace=ns,
            embedder=pipeline._global_embedder,
        )

        # Verify chunks exist.
        chunks_before: list[dict] = []
        async for chunk in store.list_chunks_raw(col, ns):
            chunks_before.append(chunk)
        assert len(chunks_before) > 0, "ingest must produce at least one chunk"

        # Delete the source file so it is now an orphan.
        doc_file.unlink()
        assert not doc_file.exists()

        # Build the MaintenanceLoop with the real store.
        cfg = MaintenanceConfig(
            interval_hours=0,
            fts_optimize=False,  # skip FTS to isolate orphan logic
            orphan_cleanup=True,
            failed_ingest_retry=False,
        )
        from archon_search.jobs.maintenance_loop import MaintenanceLoop

        loop = MaintenanceLoop(
            job_store=MagicMock(),
            search_store=store,
            config=cfg,
            data_dir=tmp_path,
        )
        loop._current_health = {  # type: ignore[attr-defined]
            "fts_optimized_at": None,
            "orphans_removed_last_run": 0,
            "last_retry_at": None,
            "last_error": None,
            "meta_chunk_count": 0,
        }

        await loop._run_orphan_cleanup(col, ns)

        # Assert orphans_removed_last_run > 0.
        assert loop._current_health["orphans_removed_last_run"] > 0, (
            "orphans_removed_last_run must be incremented for the deleted file"
        )

        # Assert all chunks for that source_path are gone.
        chunks_after: list[dict] = []
        async for chunk in store.list_chunks_raw(col, ns):
            chunks_after.append(chunk)

        source_path_str = str(doc_file)
        remaining = [c for c in chunks_after if c["source_path"] == source_path_str]
        assert remaining == [], (
            f"orphan chunks must be deleted; {len(remaining)} remain"
        )
    finally:
        gc.collect()
        await store.disconnect()
