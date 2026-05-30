"""Tests — MCP ingest_file / ingest_directory emit stage_timings log records (B1 Task 5.2)."""
from __future__ import annotations

import logging
import sys
import time
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub fastmcp so mcp.py can be imported without the real package.
if "fastmcp" not in sys.modules:
    _fastmcp = types.ModuleType("fastmcp")
    _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fastmcp

from archon_search._types import IngestResult
from archon_search.config import SearchConfig
from archon_search.observability import _stage_recorder, bind_stage_recorder, record_stage


# ---------------------------------------------------------------------------
# FastMCP stub (mirrors test_mcp_ingest_503.py)
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, name: str) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            return func
        return decorator

    def custom_route(self, path: str, methods: list[str] | None = None) -> Any:
        def decorator(func: Any) -> Any:
            return func
        return decorator


class _FakeFastMCP:
    def __new__(cls, name: str, **kwargs: Any) -> _FakeApp:  # type: ignore[misc]
        return _FakeApp(name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(pipeline: MagicMock, config: SearchConfig | None = None) -> _FakeApp:
    if config is None:
        cfg = SearchConfig()
        cfg.observability.stage_timings_enabled = True
    else:
        cfg = config
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        return mcp_module.create_app(pipeline, "default", writer=None, config=cfg)  # type: ignore[call-arg]


def _stage_recording_ingest_file(doc_id: str = "doc1") -> Any:
    """Return an async side_effect for pipeline.ingest_file that records parse/embed/persist."""
    async def _impl(*args: Any, **kwargs: Any) -> IngestResult:
        with record_stage("parse"):
            pass
        with record_stage("embed"):
            pass
        with record_stage("persist"):
            pass
        return IngestResult(doc_id=doc_id, chunks_created=1, status="ok")
    return _impl


def _stage_recording_ingest_directory(n_files: int = 2) -> Any:
    """Return an async side_effect for pipeline.ingest_directory that records stages per file."""
    async def _impl(*args: Any, **kwargs: Any) -> list[IngestResult]:
        results = []
        for i in range(n_files):
            with record_stage("parse"):
                pass
            with record_stage("embed"):
                pass
            with record_stage("persist"):
                pass
            results.append(IngestResult(doc_id=f"doc{i}", chunks_created=1, status="ok"))
        return results
    return _impl


def _get_stage_timing_records(caplog: pytest.LogCaptureFixture) -> list:
    return [r for r in caplog.records if getattr(r, "event_type", None) == "stage_timings"]


# ---------------------------------------------------------------------------
# MCP ingest_file stage timings tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_ingest_file_emits_stage_timings(caplog: pytest.LogCaptureFixture) -> None:
    """MCP ingest_file tool emits one stage_timings log record with parse/embed/persist/total keys."""
    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock(side_effect=_stage_recording_ingest_file())

    app = _make_app(pipeline)

    with caplog.at_level(logging.INFO, logger="archon_search"):
        result = await app.tools["ingest_file"](path="/tmp/x.md", collection=None)

    assert isinstance(result, dict), f"Expected dict result, got {type(result)}"
    assert result.get("status") == "ok", f"Expected ok status, got {result}"

    records = _get_stage_timing_records(caplog)
    assert len(records) == 1, f"Expected 1 stage_timings record, got {len(records)}"
    rec = records[0]
    assert rec.endpoint == "ingest", f"endpoint should be 'ingest', got {rec.endpoint!r}"
    timings = rec.stage_timings_ms
    assert {"parse", "embed", "persist", "total"} == set(timings.keys()), (
        f"Expected {{parse, embed, persist, total}}, got {set(timings.keys())}"
    )


@pytest.mark.asyncio
async def test_mcp_ingest_file_stage_timings_values_non_negative(caplog: pytest.LogCaptureFixture) -> None:
    """All stage timing values in ingest_file log record are non-negative floats."""
    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock(side_effect=_stage_recording_ingest_file())

    app = _make_app(pipeline)

    with caplog.at_level(logging.INFO, logger="archon_search"):
        await app.tools["ingest_file"](path="/tmp/x.md", collection=None)

    records = _get_stage_timing_records(caplog)
    assert len(records) == 1
    for stage, val in records[0].stage_timings_ms.items():
        assert val >= 0.0, f"stage {stage!r} timing {val!r} is negative"


@pytest.mark.asyncio
async def test_mcp_ingest_file_stage_timings_disabled_no_log(caplog: pytest.LogCaptureFixture) -> None:
    """stage_timings_enabled=False → no stage_timings log record from ingest_file."""
    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock(side_effect=_stage_recording_ingest_file())

    cfg = SearchConfig()
    cfg.observability.stage_timings_enabled = False
    app = _make_app(pipeline, config=cfg)

    with caplog.at_level(logging.DEBUG, logger="archon_search"):
        await app.tools["ingest_file"](path="/tmp/x.md", collection=None)

    records = _get_stage_timing_records(caplog)
    assert len(records) == 0, f"Expected 0 stage_timings records when disabled, got {len(records)}"


# ---------------------------------------------------------------------------
# MCP ingest_directory aggregated stage sums tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_ingest_directory_emits_aggregated_stage_sums(caplog: pytest.LogCaptureFixture) -> None:
    """MCP ingest_directory uses stage_sums_ms (not stage_timings_ms) for log record.

    Three files are ingested with known fixed per-file durations; the log record's
    stage values must equal the SUM (not the last value). If last-write-wins were
    used, all values would equal one file's contribution instead of three.
    """
    async def _ingest_directory_with_known_timings(path: Any, collection: Any, **kwargs: Any) -> list[IngestResult]:
        recorder = _stage_recorder.get()
        assert recorder is not None, "_stage_recorder not bound — bind_stage_recorder() not active"
        results = []
        for i in range(3):
            recorder.record("parse", 10.0)   # 10ms each → 30ms total
            recorder.record("embed", 20.0)   # 20ms each → 60ms total
            recorder.record("persist", 5.0)  # 5ms each  → 15ms total
            results.append(IngestResult(doc_id=f"doc{i}", chunks_created=1, status="ok"))
        return results

    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(side_effect=_ingest_directory_with_known_timings)

    app = _make_app(pipeline)

    with caplog.at_level(logging.INFO, logger="archon_search"):
        result = await app.tools["ingest_directory"](path="/tmp/dir", collection=None)

    assert isinstance(result, list), f"Expected list result, got {type(result)}"

    records = _get_stage_timing_records(caplog)
    assert len(records) == 1, f"Expected 1 stage_timings record, got {len(records)}"
    rec = records[0]
    assert rec.endpoint == "ingest", f"endpoint should be 'ingest', got {rec.endpoint!r}"
    timings = rec.stage_timings_ms
    assert "total" in timings, f"'total' key missing: {set(timings.keys())}"

    # Verify sums (not last-write-wins): 3 files × known per-file values
    assert timings["parse"] == pytest.approx(30.0), (
        f"Expected parse sum 30.0 (3×10), got {timings['parse']!r} — last-write-wins would give 10.0"
    )
    assert timings["embed"] == pytest.approx(60.0), (
        f"Expected embed sum 60.0 (3×20), got {timings['embed']!r} — last-write-wins would give 20.0"
    )
    assert timings["persist"] == pytest.approx(15.0), (
        f"Expected persist sum 15.0 (3×5), got {timings['persist']!r} — last-write-wins would give 5.0"
    )
    assert timings["total"] >= 0.0, "total must be non-negative"


@pytest.mark.asyncio
async def test_mcp_ingest_directory_stage_timings_disabled_no_log(caplog: pytest.LogCaptureFixture) -> None:
    """stage_timings_enabled=False → no stage_timings log record from ingest_directory."""
    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(side_effect=_stage_recording_ingest_directory())

    cfg = SearchConfig()
    cfg.observability.stage_timings_enabled = False
    app = _make_app(pipeline, config=cfg)

    with caplog.at_level(logging.DEBUG, logger="archon_search"):
        await app.tools["ingest_directory"](path="/tmp/dir", collection=None)

    records = _get_stage_timing_records(caplog)
    assert len(records) == 0, f"Expected 0 stage_timings records when disabled, got {len(records)}"
