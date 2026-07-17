"""Tests — CLI ingest emits stage_timings log records (B1 Task 5.2).

Note: `collection add` stage_timings tests were removed in FE-4 — `collection add` is now
an httpx proxy; stage timings are emitted server-side, not in the CLI.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from archon_search._types import IngestResult
from archon_search.config import SearchConfig
from archon_search.observability import record_stage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_pipeline() -> MagicMock:
    """Build a mock pipeline whose ingest_directory records parse/embed/persist stages."""
    pipeline = MagicMock()
    pipeline.store = MagicMock()
    pipeline.store.connect = AsyncMock()
    pipeline.store.disconnect = AsyncMock()

    async def _ingest_directory(*args: object, **kwargs: object) -> list[IngestResult]:
        with record_stage("parse"):
            pass
        with record_stage("embed"):
            pass
        with record_stage("persist"):
            pass
        return [IngestResult(doc_id="doc1", chunks_created=1, status="ok")]

    pipeline.ingest_directory = AsyncMock(side_effect=_ingest_directory)
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)
    return pipeline


def _get_stage_timing_records(caplog: pytest.LogCaptureFixture) -> list:
    return [r for r in caplog.records if getattr(r, "event_type", None) == "stage_timings"]


# ---------------------------------------------------------------------------
# CLI ingest command
# ---------------------------------------------------------------------------


def test_cli_ingest_emits_stage_timings_log_record(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI `ingest` command emits one stage_timings log record with correlation_id."""
    from archon_search.cli.ingest import ingest

    mock_pipeline = _make_mock_pipeline()
    monkeypatch.setattr("archon_search.cli.ingest.create_pipeline", lambda cfg: mock_pipeline)
    monkeypatch.setattr("archon_search.cli.ingest.load_config", lambda p: SearchConfig())

    runner = CliRunner()
    with caplog.at_level(logging.INFO, logger="archon_search"):
        result = runner.invoke(ingest, ["--path", str(tmp_path), "--collection", "test-col"])

    assert result.exit_code == 0, f"CLI exited with {result.exit_code}: {result.output}"

    records = _get_stage_timing_records(caplog)
    assert len(records) == 1, f"Expected 1 stage_timings record, got {len(records)}"
    rec = records[0]
    assert rec.endpoint == "ingest", f"endpoint should be 'ingest', got {rec.endpoint!r}"
    assert "total" in rec.stage_timings_ms, f"'total' key missing from {set(rec.stage_timings_ms)}"
    # CLI mints its own correlation_id (32-char hex)
    assert isinstance(rec.correlation_id, str), "correlation_id should be a string"
    assert len(rec.correlation_id) == 32, f"Expected 32-char hex, got {rec.correlation_id!r}"
    assert rec.correlation_id.isalnum(), f"correlation_id should be alphanumeric hex, got {rec.correlation_id!r}"


def test_cli_ingest_disabled_no_stage_timings_log(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stage_timings_enabled=False → CLI `ingest` emits no stage_timings log record."""
    from archon_search.cli.ingest import ingest

    mock_pipeline = _make_mock_pipeline()
    monkeypatch.setattr("archon_search.cli.ingest.create_pipeline", lambda cfg: mock_pipeline)

    cfg = SearchConfig()
    cfg.observability.stage_timings_enabled = False
    monkeypatch.setattr("archon_search.cli.ingest.load_config", lambda p: cfg)

    runner = CliRunner()
    with caplog.at_level(logging.DEBUG, logger="archon_search"):
        result = runner.invoke(ingest, ["--path", str(tmp_path), "--collection", "test-col"])

    assert result.exit_code == 0, f"CLI exited with {result.exit_code}: {result.output}"
    records = _get_stage_timing_records(caplog)
    assert len(records) == 0, f"Expected 0 stage_timings records when disabled, got {len(records)}"
