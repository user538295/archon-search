"""Tests for archon_search.observability — Task 1.1 (B1)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from archon_search.observability import (
    StageRecorder,
    _stage_recorder,
    bind_stage_recorder,
    correlation_id,
    new_correlation_id,
    paused_stage_recorder,
    record_stage,
    sanitize_request_id,
)


# ---------------------------------------------------------------------------
# StageRecorder
# ---------------------------------------------------------------------------


def test_stage_recorder_single_stage() -> None:
    recorder = StageRecorder()
    recorder.record("embed", 5.0)
    timings = recorder.stage_timings_ms
    assert set(timings.keys()) == {"embed"}
    assert timings["embed"] >= 0


def test_stage_recorder_multiple_stages() -> None:
    recorder = StageRecorder()
    recorder.record("embed", 1.0)
    recorder.record("vector", 2.0)
    recorder.record("fuse", 3.0)
    assert {"embed", "vector", "fuse"} == set(recorder.stage_timings_ms.keys())


def test_stage_recorder_repeated_stage_logs_debug(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    recorder = StageRecorder()
    with caplog.at_level(logging.DEBUG, logger="archon_search"):
        recorder.record("embed", 10.0)
        recorder.record("embed", 20.0)

    assert recorder.stage_timings_ms["embed"] == 20.0  # last-write-wins
    assert recorder.stage_sums_ms["embed"] == 30.0
    assert any("embed" in r.message for r in caplog.records if r.levelno == logging.DEBUG)


def test_stage_sums_ms_returns_sum_across_recordings() -> None:
    recorder = StageRecorder()
    recorder.record("embed", 10.0)
    recorder.record("embed", 20.0)
    recorder.record("embed", 30.0)
    assert recorder.stage_sums_ms["embed"] == 60.0
    assert recorder.stage_timings_ms["embed"] == 30.0  # last-write-wins


# ---------------------------------------------------------------------------
# record_stage
# ---------------------------------------------------------------------------


def test_record_stage_noop_when_unbound() -> None:
    """record_stage is a pure no-op when no StageRecorder is bound."""
    assert _stage_recorder.get() is None
    with record_stage("embed"):
        pass  # must not raise
    assert _stage_recorder.get() is None


def test_record_stage_records_timing_when_bound() -> None:
    with bind_stage_recorder() as recorder:
        with record_stage("embed"):
            pass
    assert "embed" in recorder.stage_timings_ms
    assert recorder.stage_timings_ms["embed"] >= 0


def test_record_stage_records_in_finally_on_raise() -> None:
    """Timing is recorded even if the block raises."""
    with bind_stage_recorder() as recorder:
        with pytest.raises(ValueError):
            with record_stage("embed"):
                raise ValueError("boom")
    assert "embed" in recorder.stage_timings_ms


# ---------------------------------------------------------------------------
# bind_stage_recorder — token reset / nesting
# ---------------------------------------------------------------------------


def test_bind_stage_recorder_token_reset_nested() -> None:
    """Inner bind restores outer recorder on exit (token-based reset)."""
    with bind_stage_recorder() as outer:
        with bind_stage_recorder() as inner:
            assert _stage_recorder.get() is inner
        # inner exited — outer should be restored
        assert _stage_recorder.get() is outer
    # both exited — should be None
    assert _stage_recorder.get() is None


# ---------------------------------------------------------------------------
# paused_stage_recorder / record_total
# ---------------------------------------------------------------------------

_PAUSED_SLEEP_SECONDS = 0.05
_PAUSED_SLEEP_MS = _PAUSED_SLEEP_SECONDS * 1000.0


def test_paused_stage_recorder_records_no_stage_inside_the_block() -> None:
    """A paused block (HyDE's LLM call) adds no stage — not even its embed — to the recorder."""
    with bind_stage_recorder() as recorder:
        with paused_stage_recorder():
            assert _stage_recorder.get() is None
            with record_stage("embed"):
                pass
        assert _stage_recorder.get() is recorder
    assert "embed" not in recorder.stage_timings_ms


def test_record_total_excludes_paused_wall_time() -> None:
    """``total`` covers the stage window minus the paused block, as if it ran outside it."""
    import time

    with bind_stage_recorder() as recorder:
        t0 = time.perf_counter()
        with paused_stage_recorder():
            time.sleep(_PAUSED_SLEEP_SECONDS)
        recorder.record_total(t0)
    assert 0 <= recorder.stage_timings_ms["total"] < _PAUSED_SLEEP_MS


def test_record_total_without_a_pause_is_the_whole_window() -> None:
    import time

    with bind_stage_recorder() as recorder:
        t0 = time.perf_counter()
        time.sleep(_PAUSED_SLEEP_SECONDS)
        recorder.record_total(t0)
    assert recorder.stage_timings_ms["total"] >= _PAUSED_SLEEP_MS


def test_record_total_ignores_a_pause_that_ended_before_its_window() -> None:
    """C2-I-6: only the paused time inside [started_at, now] comes off ``total``."""
    import time

    with bind_stage_recorder() as recorder:
        with paused_stage_recorder():
            time.sleep(_PAUSED_SLEEP_SECONDS)
        t0 = time.perf_counter()
        time.sleep(_PAUSED_SLEEP_SECONDS)
        recorder.record_total(t0)
    assert recorder.stage_timings_ms["total"] >= _PAUSED_SLEEP_MS


def test_record_total_subtracts_only_the_part_of_a_pause_inside_its_window() -> None:
    """A pause straddling ``started_at`` is subtracted from that instant on, never below zero."""
    with bind_stage_recorder() as recorder:
        recorder.record_pause(started_at=10.0, ended_at=12.0)
        with patch("archon_search.observability.time.perf_counter", return_value=15.0):
            recorder.record_total(11.0)
    assert recorder.stage_timings_ms["total"] == pytest.approx(3000.0)


def test_paused_stage_recorder_noop_when_unbound() -> None:
    with paused_stage_recorder():
        assert _stage_recorder.get() is None
    assert _stage_recorder.get() is None


# ---------------------------------------------------------------------------
# correlation_id ContextVar
# ---------------------------------------------------------------------------


def test_correlation_id_default_is_none() -> None:
    assert correlation_id.get() is None


def test_correlation_id_can_be_set_and_reset() -> None:
    token = correlation_id.set("test-id-123")
    assert correlation_id.get() == "test-id-123"
    correlation_id.reset(token)
    assert correlation_id.get() is None


# ---------------------------------------------------------------------------
# new_correlation_id
# ---------------------------------------------------------------------------


def test_new_correlation_id_format() -> None:
    cid = new_correlation_id()
    assert len(cid) == 32
    assert all(c in "0123456789abcdef" for c in cid)


def test_new_correlation_id_unique() -> None:
    assert new_correlation_id() != new_correlation_id()


# ---------------------------------------------------------------------------
# sanitize_request_id
# ---------------------------------------------------------------------------


def test_sanitize_request_id_valid() -> None:
    value = "abc123ABC._-"
    assert sanitize_request_id(value) == value


def test_sanitize_request_id_valid_max_length() -> None:
    value = "a" * 128
    assert sanitize_request_id(value) == value


def test_sanitize_request_id_rejects_newline() -> None:
    assert sanitize_request_id("abc\ndef") is None


def test_sanitize_request_id_rejects_too_long() -> None:
    assert sanitize_request_id("a" * 129) is None


def test_sanitize_request_id_none_input() -> None:
    assert sanitize_request_id(None) is None


def test_sanitize_request_id_empty_string() -> None:
    assert sanitize_request_id("") is None


def test_sanitize_request_id_rejects_special_chars() -> None:
    assert sanitize_request_id("abc!def") is None
