"""Regression guards: async ``SearchStore`` methods must not do blocking FS I/O on the loop.

Invariant guarded here — three ``async def`` methods in ``archon_search/store.py``
must offload their blocking syscall (``asyncio.to_thread``) instead of running it
inline on the event loop thread:

- ``connect``               -> ``self._db_path.mkdir(parents=True, exist_ok=True)``
- ``reindex_metadata``      -> ``Path(source_path).stat()``, memoized to one call
  per unique ``source_path``
- ``delete_by_source_path`` -> ``Path(source_path).resolve()``

Technique (identical for all three): the blocking ``pathlib`` method is
monkeypatched with a wrapper that simulates a slow disk via ``time.sleep`` and,
while sleeping, samples a heartbeat counter incremented by a concurrently
scheduled task. If the syscall runs inline on the loop thread the heartbeat
cannot tick (delta == 0); once it is offloaded the loop is free and the heartbeat
ticks during the call (delta > 0).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from archon_search._types import ChunkRecord
from archon_search.store import SearchStore

_DIM = 4
# Long enough that the heartbeat ticks tens of times if the loop is free, short
# enough not to burn wall-clock across the suite.
_SLOW_IO_SECONDS = 0.05
_HEARTBEAT_SECONDS = 0.001
# Chunks sharing one source_path — proves reindex_metadata stats the file once.
_SHARED_SOURCE_ROWS = 3


class _LoopProbe:
    """Counts event-loop ticks and samples them from inside the blocking call."""

    def __init__(self) -> None:
        self.ticks = 0
        self.calls = 0
        self.ticks_during_call: int | None = None

    async def heartbeat(self) -> None:
        while True:
            self.ticks += 1
            await asyncio.sleep(_HEARTBEAT_SECONDS)

    def sample_around_blocking_sleep(self) -> None:
        """Simulate a slow syscall and record how many loop ticks happened meanwhile."""
        self.calls += 1
        before = self.ticks
        time.sleep(_SLOW_IO_SECONDS)
        self.ticks_during_call = self.ticks - before


async def _run_with_heartbeat(probe: _LoopProbe, coro) -> Any:
    hb = asyncio.create_task(probe.heartbeat())
    await asyncio.sleep(0)  # let the heartbeat reach its first await
    try:
        return await coro
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb


def _assert_loop_was_free(probe: _LoopProbe, method: str) -> None:
    assert probe.calls >= 1, f"the patched Path.{method} was never called"
    assert probe.ticks_during_call is not None
    assert probe.ticks_during_call > 0, (
        f"event loop was blocked for the whole of Path.{method}: the heartbeat "
        f"coroutine ticked {probe.ticks_during_call} times during the "
        f"{_SLOW_IO_SECONDS}s call. Offload it (asyncio.to_thread)."
    )


def _chunks(source_path: str, count: int) -> list[ChunkRecord]:
    """``count`` chunks of one document, all sharing *source_path*."""
    # 64 hex chars: the store validates chunk_id against ``^[a-f0-9]{64}-\d{6}$``.
    did = uuid.uuid4().hex + uuid.uuid4().hex
    return [
        ChunkRecord(
            doc_id=did,
            chunk_id=f"{did}-{i:06d}",
            text=f"hello world for blocking-io probe {i}",
            vector=[0.0] * _DIM,
            source_path=source_path,
            indexed_at=datetime.now(timezone.utc).isoformat(),
            ingested_by="cli",
        )
        for i in range(count)
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_connect_mkdir_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db"
    store = SearchStore(db_path)
    probe = _LoopProbe()
    real_mkdir = Path.mkdir

    def slow_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == db_path and probe.calls == 0:
            probe.sample_around_blocking_sleep()
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", slow_mkdir)
    try:
        await _run_with_heartbeat(probe, store.connect())
    finally:
        monkeypatch.setattr(Path, "mkdir", real_mkdir)
        await store.disconnect()

    _assert_loop_was_free(probe, "mkdir")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_metadata_stat_does_not_block_the_event_loop(
    connected_store: SearchStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    col = f"blockio_stat_{uuid.uuid4().hex[:8]}"
    source = tmp_path / "doc.md"
    source.write_text("hello world", encoding="utf-8")

    probe = _LoopProbe()
    real_stat = Path.stat
    source_stats = 0

    def slow_stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal source_stats
        if self == source:
            source_stats += 1
            if probe.calls == 0:
                probe.sample_around_blocking_sleep()
        return real_stat(self, *args, **kwargs)

    await connected_store.ensure_collection(col, _DIM)
    try:
        await connected_store.ingest_chunks(col, _chunks(str(source), _SHARED_SOURCE_ROWS))

        monkeypatch.setattr(Path, "stat", slow_stat)
        result = await _run_with_heartbeat(
            probe, connected_store.reindex_metadata(col, dry_run=True)
        )

        assert result.processed == _SHARED_SOURCE_ROWS
        assert result.warnings == []
        assert source_stats == 1, (
            f"expected the per-path stat cache to collapse {_SHARED_SOURCE_ROWS} rows "
            f"to a single Path.stat call, got {source_stats}"
        )
        _assert_loop_was_free(probe, "stat")
    finally:
        await connected_store.drop_collection(col)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_metadata_warns_once_per_row_for_missing_source(
    connected_store: SearchStore, tmp_path: Path
) -> None:
    """The offloaded ``stat``'s OSError still becomes one warning per ROW."""
    col = f"blockio_missing_{uuid.uuid4().hex[:8]}"
    missing = tmp_path / "gone.md"

    await connected_store.ensure_collection(col, _DIM)
    try:
        await connected_store.ingest_chunks(col, _chunks(str(missing), _SHARED_SOURCE_ROWS))

        result = await connected_store.reindex_metadata(col, dry_run=True)

        assert result.processed == _SHARED_SOURCE_ROWS
        assert result.warnings == [f"missing-source: {missing}"] * _SHARED_SOURCE_ROWS
    finally:
        await connected_store.drop_collection(col)


@pytest.mark.asyncio
async def test_delete_by_source_path_resolve_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SearchStore(tmp_path / "db")
    source = tmp_path / "doc.md"
    source.write_text("hello world", encoding="utf-8")
    captured: dict[str, Any] = {}

    async def fake_delete_document(collection: str, doc_id: str, **kwargs: Any) -> int:
        captured["collection"] = collection
        captured["doc_id"] = doc_id
        return 0

    monkeypatch.setattr(store, "delete_document", fake_delete_document)

    probe = _LoopProbe()
    real_resolve = Path.resolve

    def slow_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self == source and probe.calls == 0:
            probe.sample_around_blocking_sleep()
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", slow_resolve)
    await _run_with_heartbeat(probe, store.delete_by_source_path("docs", str(source)))

    assert captured["collection"] == "docs"
    assert captured["doc_id"] == hashlib.sha256(str(real_resolve(source)).encode()).hexdigest()
    _assert_loop_was_free(probe, "resolve")
