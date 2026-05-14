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


# ---------------------------------------------------------------------------
# Task 2.2 — oversized-entry truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writer_truncates_oversized_entry(tmp_path: Path) -> None:
    """1000 doc_ids each 50 chars long → written line ≤ 8192 bytes, truncated=true."""
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)

    big_ids = ["x" * 50 for _ in range(1000)]
    entry = TelemetryEntry(
        query_id="big",
        timestamp="2026-05-14T12:00:00Z",
        endpoint="search",
        latency_ms=1.0,
        status="ok",
        collection="default",
        result_count=len(big_ids),
        result_doc_ids=big_ids,
    )

    await writer.start()
    writer.enqueue(entry)
    await writer.drain_and_stop()

    log_file = tmp_path / "2026-05-14.jsonl"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    line_bytes = lines[0].encode("utf-8")
    assert len(line_bytes) <= 8192, f"line is {len(line_bytes)} bytes, expected ≤ 8192"
    parsed = json.loads(lines[0])
    assert parsed.get("truncated") is True


@pytest.mark.asyncio
async def test_writer_keeps_short_entry_untouched(tmp_path: Path) -> None:
    """A small entry must not have a 'truncated' key in the serialized output."""
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)

    await writer.start()
    writer.enqueue(_make_entry())
    await writer.drain_and_stop()

    log_file = tmp_path / "2026-05-14.jsonl"
    parsed = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
    assert "truncated" not in parsed


def test_truncate_to_fit_binary_search_correctness() -> None:
    """The result must be the largest prefix that still fits within the limit."""
    from archon_search.telemetry.writer import MAX_ENTRY_BYTES, TelemetryWriter

    writer = TelemetryWriter(Path("/tmp"))  # log_dir unused in this call
    big_ids = ["x" * 50 for _ in range(1000)]
    entry = TelemetryEntry(
        query_id="bs",
        timestamp="2026-05-14T12:00:00Z",
        endpoint="search",
        latency_ms=1.0,
        status="ok",
        collection="default",
        result_count=len(big_ids),
        result_doc_ids=big_ids,
    )

    result = writer._truncate_to_fit(entry, MAX_ENTRY_BYTES)

    # Must fit.
    serialized = writer._serialize(result)
    assert len(serialized) <= MAX_ENTRY_BYTES

    # Must be truncated and flagged.
    assert result.truncated is True
    assert result.result_doc_ids is not None
    kept = len(result.result_doc_ids)

    # Adding one more id must break the limit (largest-prefix property).
    if kept < len(big_ids):
        one_more = result.model_copy(
            update={
                "result_doc_ids": big_ids[: kept + 1],
                "truncated": True,
            }
        )
        assert len(writer._serialize(one_more)) > MAX_ENTRY_BYTES


def test_truncate_to_fit_raises_when_even_zero_doc_ids_too_large() -> None:
    """When common fields alone exceed the limit, ValueError must be raised."""
    from archon_search.telemetry.writer import TelemetryWriter

    writer = TelemetryWriter(Path("/tmp"))
    # A very small limit that even a minimal serialized entry will exceed.
    entry = TelemetryEntry(
        query_id="tiny",
        timestamp="2026-05-14T12:00:00Z",
        endpoint="search",
        latency_ms=1.0,
        status="ok",
        collection="default",
        result_count=0,
        result_doc_ids=[],
    )

    import pytest as _pytest

    with _pytest.raises(ValueError, match="exceeds MAX_ENTRY_BYTES"):
        writer._truncate_to_fit(entry, limit_bytes=10)


# ---------------------------------------------------------------------------
# Fix 1 — new tests for result_doc_ids=None edge cases and drain-loop coverage
# ---------------------------------------------------------------------------


def test_writer_truncate_to_fit_raises_for_none_doc_ids() -> None:
    """_truncate_to_fit raises ValueError when entry has result_doc_ids=None and is oversized."""
    from archon_search.telemetry.writer import TelemetryWriter

    writer = TelemetryWriter(Path("/tmp"))
    # A route entry has result_doc_ids=None by design.
    entry = TelemetryEntry.from_route_response(
        collections=["col1", "col2"],
        decomposer_invoked=True,
        latency_ms=5.0,
    )
    # Force a tiny limit so the entry is "oversized".
    with pytest.raises(ValueError, match="no result_doc_ids to truncate"):
        writer._truncate_to_fit(entry, limit_bytes=10)


@pytest.mark.asyncio
async def test_writer_handles_route_entry_exceeding_limit_as_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """ValueError from _truncate_to_fit is caught in drain loop; writer stays alive."""
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)

    route_entry = TelemetryEntry.from_route_response(
        collections=["col1"],
        decomposer_invoked=False,
        latency_ms=3.0,
    )

    caplog.set_level(logging.WARNING, logger="archon.search")

    # Patch _truncate_to_fit to always raise for this specific oversized call.
    real_truncate = writer._truncate_to_fit

    def truncate_raises(entry: TelemetryEntry, limit_bytes: int = 8192) -> TelemetryEntry:
        if entry.result_doc_ids is None:
            raise ValueError("entry exceeds MAX_ENTRY_BYTES and has no result_doc_ids to truncate")
        return real_truncate(entry, limit_bytes)

    writer._truncate_to_fit = truncate_raises  # type: ignore[method-assign]

    await writer.start()
    writer.enqueue(route_entry)
    # Enqueue a normal entry afterwards to prove the drain loop is still alive.
    writer.enqueue(_make_entry("after-route"))
    await writer.drain_and_stop()

    # The normal entry must have been written.
    log_file = tmp_path / "2026-05-14.jsonl"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "after-route" in content

    # A warning about the failed write must have been emitted.
    write_warnings = [r for r in caplog.records if "write failed" in r.getMessage().lower()]
    assert len(write_warnings) >= 1


@pytest.mark.asyncio
async def test_writer_valueerror_from_truncation_caught_in_drain_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Monkeypatched _truncate_to_fit raising ValueError: drain completes, warning logged, writer survives."""
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)

    monkeypatch.setattr(
        writer,
        "_truncate_to_fit",
        lambda entry, limit_bytes=8192: (_ for _ in ()).throw(ValueError("synthetic error")),
    )

    caplog.set_level(logging.WARNING, logger="archon.search")

    await writer.start()
    writer.enqueue(_make_entry("raises"))
    await writer.drain_and_stop()

    # 1. Drain task completed without crashing (drain_and_stop didn't raise).
    assert writer._task is None  # task was cleaned up normally

    # 2. A WARNING was logged.
    warn_records = [r for r in caplog.records if r.levelno == logging.WARNING and "archon.search" in r.name]
    assert len(warn_records) >= 1

    # 3. Writer can process a subsequent entry (start again to prove not broken).
    await writer.start()
    writer.enqueue(_make_entry("after-error"))
    # Remove the monkeypatch for the second round.
    monkeypatch.setattr(writer, "_truncate_to_fit", TelemetryWriter._truncate_to_fit.__get__(writer))
    await writer.drain_and_stop()


@pytest.mark.asyncio
async def test_writer_write_error_warning_is_rate_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When _truncate_to_fit always raises, 10 enqueued entries produce only 1 WARNING."""
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)

    monkeypatch.setattr(
        writer,
        "_truncate_to_fit",
        lambda entry, limit_bytes=8192: (_ for _ in ()).throw(ValueError("always fails")),
    )

    caplog.set_level(logging.WARNING, logger="archon.search")

    await writer.start()
    for i in range(10):
        writer.enqueue(_make_entry(f"q{i}"))
    await writer.drain_and_stop()

    write_warnings = [
        r for r in caplog.records
        if "write failed" in r.getMessage().lower()
    ]
    assert len(write_warnings) == 1, (
        f"expected exactly 1 rate-limited warning, got {len(write_warnings)}"
    )


def test_writer_keeps_error_entry_untouched() -> None:
    """An error entry that fits within the limit is returned as-is (no truncated field)."""
    from archon_search.telemetry.writer import MAX_ENTRY_BYTES, TelemetryWriter

    writer = TelemetryWriter(Path("/tmp"))
    entry = TelemetryEntry.from_error(
        endpoint="search",
        status="timeout",
        error_kind="timeout",
        latency_ms=30000.0,
    )

    result = writer._truncate_to_fit(entry, MAX_ENTRY_BYTES)

    # Must be the same object (returned unchanged).
    assert result is entry
    # Must not have a truncated field set.
    assert result.truncated is None


@pytest.mark.asyncio
async def test_writer_keeps_short_entry_result_doc_ids_intact(tmp_path: Path) -> None:
    """A short entry must pass through _truncate_to_fit with result_doc_ids preserved."""
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    writer = TelemetryWriter(tmp_path, clock=lambda: fixed)

    original_ids = ["doc-1", "doc-2", "doc-3"]
    entry = TelemetryEntry(
        query_id="short",
        timestamp="2026-05-14T12:00:00Z",
        endpoint="search",
        latency_ms=5.0,
        status="ok",
        collection="default",
        result_count=len(original_ids),
        result_doc_ids=original_ids,
    )

    await writer.start()
    writer.enqueue(entry)
    await writer.drain_and_stop()

    log_file = tmp_path / "2026-05-14.jsonl"
    parsed = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
    assert "truncated" not in parsed
    assert parsed["result_doc_ids"] == original_ids
