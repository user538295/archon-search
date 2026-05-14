"""Tests for TelemetryWriter — enqueue + drain loop (FEAT-039b Task 2.1)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon_search.telemetry.entry import TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter


def _make_entry(query_id: str = "abc123") -> TelemetryEntry:
    return TelemetryEntry(
        query_id=query_id,
        timestamp="2026-05-14T12:00:00Z",
        endpoint="search",
        latency_ms=12.5,
        status="ok",
        collection="default",
        result_count=1,
        result_doc_ids=["doc-1"],
    )


@pytest.mark.asyncio
async def test_writer_enqueues_and_writes_one_line_per_entry(tmp_path: Path) -> None:
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)
    await writer.start()
    entry = _make_entry()
    writer.enqueue(entry)
    await writer.drain_and_stop()

    log_file = tmp_path / "2026-05-14.jsonl"
    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["query_id"] == "abc123"
    assert parsed["endpoint"] == "search"


@pytest.mark.asyncio
async def test_writer_appends_to_existing_file(tmp_path: Path) -> None:
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    log_file = tmp_path / "2026-05-14.jsonl"
    log_file.write_text('{"pre": "existing"}\n', encoding="utf-8")

    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)
    await writer.start()
    writer.enqueue(_make_entry("q1"))
    writer.enqueue(_make_entry("q2"))
    await writer.drain_and_stop()

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"pre": "existing"}
    assert json.loads(lines[1])["query_id"] == "q1"
    assert json.loads(lines[2])["query_id"] == "q2"


@pytest.mark.asyncio
async def test_writer_rolls_over_at_utc_midnight(tmp_path: Path) -> None:
    times = [
        datetime(2026, 5, 14, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 5, 15, 0, 0, 1, tzinfo=UTC),
    ]
    idx = {"i": 0}

    def fake_clock() -> datetime:
        t = times[min(idx["i"], len(times) - 1)]
        idx["i"] += 1
        return t

    writer = TelemetryWriter(tmp_path, clock=fake_clock)
    await writer.start()
    writer.enqueue(_make_entry("before"))
    writer.enqueue(_make_entry("after"))
    await writer.drain_and_stop()

    file_before = tmp_path / "2026-05-14.jsonl"
    file_after = tmp_path / "2026-05-15.jsonl"
    assert file_before.exists()
    assert file_after.exists()
    assert json.loads(file_before.read_text().splitlines()[0])["query_id"] == "before"
    assert json.loads(file_after.read_text().splitlines()[0])["query_id"] == "after"


@pytest.mark.asyncio
async def test_writer_drops_oldest_on_full_queue(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, queue_size=2, clock=lambda: fixed)
    caplog.set_level(logging.WARNING, logger="archon.search")
    # Don't start the drain task yet — fill the queue first.
    writer.enqueue(_make_entry("q1"))
    writer.enqueue(_make_entry("q2"))
    writer.enqueue(_make_entry("q3"))  # drops q1

    drop_warnings = [
        r for r in caplog.records if "dropped" in r.getMessage().lower()
    ]
    assert len(drop_warnings) >= 1

    await writer.start()
    await writer.drain_and_stop()

    log_file = tmp_path / "2026-05-14.jsonl"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    ids = [json.loads(line)["query_id"] for line in lines]
    assert ids == ["q2", "q3"]


@pytest.mark.asyncio
async def test_writer_drop_balances_task_done_so_drain_does_not_hang(
    tmp_path: Path,
) -> None:
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(
        tmp_path, queue_size=2, drain_timeout_s=1.0, clock=lambda: fixed
    )
    writer.enqueue(_make_entry("q1"))
    writer.enqueue(_make_entry("q2"))
    writer.enqueue(_make_entry("q3"))  # triggers drop+task_done

    await writer.start()
    # If task_done is not balanced, join() will hang and time out.
    start = asyncio.get_event_loop().time()
    await writer.drain_and_stop()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 1.0, f"drain hung ({elapsed:.2f}s) — task_done not balanced"


@pytest.mark.asyncio
async def test_writer_dropped_count_warning_rate_limited(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, queue_size=2, clock=lambda: fixed)
    writer.enqueue(_make_entry("q1"))
    writer.enqueue(_make_entry("q2"))

    caplog.set_level(logging.WARNING, logger="archon.search")
    for i in range(100):
        writer.enqueue(_make_entry(f"drop-{i}"))

    drop_warnings = [
        r for r in caplog.records if "dropped" in r.getMessage().lower()
    ]
    assert len(drop_warnings) == 1, (
        f"expected exactly 1 rate-limited drop warning, got {len(drop_warnings)}"
    )

    await writer.start()
    await writer.drain_and_stop()


@pytest.mark.asyncio
async def test_writer_swallows_oserror_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)

    real_open = Path.open
    calls = {"n": 0}

    def flaky_open(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk-full-simulation")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)

    caplog.set_level(logging.WARNING, logger="archon.search")
    await writer.start()
    writer.enqueue(_make_entry("fail-once"))
    writer.enqueue(_make_entry("ok"))
    await writer.drain_and_stop()

    # Restore Path.open before reading back, so read_text doesn't trip the fault.
    monkeypatch.setattr(Path, "open", real_open)

    log_file = tmp_path / "2026-05-14.jsonl"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    # The first entry was dropped (OSError), but the second must have been written.
    assert "ok" in content
    assert "fail-once" not in content


@pytest.mark.asyncio
async def test_writer_crash_on_unexpected_exception_is_observable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, drain_timeout_s=0.5, clock=lambda: fixed)

    def bad_serialize(_entry: TelemetryEntry) -> bytes:
        raise TypeError("boom")

    monkeypatch.setattr(writer, "_serialize", bad_serialize)

    await writer.start()
    writer.enqueue(_make_entry())

    # Wait for the task to crash.
    assert writer._task is not None
    try:
        await asyncio.wait_for(writer._task, timeout=1.0)
    except TypeError:
        pass
    assert writer._task.done()
    assert isinstance(writer._task.exception(), TypeError)

    # Subsequent enqueue must be silent (no exception raised).
    writer.enqueue(_make_entry("post-crash"))


@pytest.mark.asyncio
async def test_writer_enqueue_after_stop_is_silent_drop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)
    await writer.start()
    await writer.drain_and_stop()

    caplog.set_level(logging.WARNING, logger="archon.search")
    for i in range(5):
        writer.enqueue(_make_entry(f"after-stop-{i}"))

    after_stop_warnings = [
        r for r in caplog.records if "after stop" in r.getMessage().lower()
    ]
    # Rate-limited: exactly 1 warning even after multiple enqueues.
    assert len(after_stop_warnings) == 1


@pytest.mark.asyncio
async def test_writer_drain_on_shutdown_flushes_pending(tmp_path: Path) -> None:
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)
    await writer.start()
    for i in range(5):
        writer.enqueue(_make_entry(f"q{i}"))
    await writer.drain_and_stop()

    log_file = tmp_path / "2026-05-14.jsonl"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5


@pytest.mark.asyncio
async def test_writer_drain_respects_bounded_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="archon.search")
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, drain_timeout_s=0.5, clock=lambda: fixed)

    # Wedge the writer's _run loop by making _append await forever.
    # Use an awaitable wedge so cancel can propagate.
    async def slow_append(when: datetime, payload: bytes) -> None:
        await asyncio.sleep(60)

    # The drain loop calls self._append synchronously; replace _append with a
    # method whose body awaits via asyncio.run_coroutine ... simpler: replace
    # the whole _run with one that blocks awaitably.
    async def wedged_run() -> None:
        while True:
            entry = await writer._queue.get()
            try:
                await asyncio.sleep(60)
            finally:
                writer._queue.task_done()

    monkeypatch.setattr(writer, "_run", wedged_run)

    await writer.start()
    writer.enqueue(_make_entry())

    start = asyncio.get_event_loop().time()
    await writer.drain_and_stop()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed <= 0.5 + 0.5, f"drain exceeded bounded timeout: {elapsed:.2f}s"

    timeout_warnings = [
        r for r in caplog.records if "drain timed out" in r.getMessage()
    ]
    assert len(timeout_warnings) == 1
    assert "unfinished" in timeout_warnings[0].getMessage()


@pytest.mark.asyncio
async def test_writer_drain_is_idempotent(tmp_path: Path) -> None:
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)
    await writer.start()
    writer.enqueue(_make_entry())
    await writer.drain_and_stop()
    # Second call must not raise.
    await writer.drain_and_stop()
    await writer.drain_and_stop()
