"""Tests — CLI ingest / collection add / reindex emit stage_timings log records (B1 Task 5.2)."""
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


# ---------------------------------------------------------------------------
# CLI collection add command
# ---------------------------------------------------------------------------


def test_cli_collection_add_emits_stage_timings_log_record(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI `collection add` command emits one stage_timings log record with correlation_id."""
    from archon_search.cli.collection import add

    mock_pipeline = _make_mock_pipeline()
    monkeypatch.setattr("archon_search.cli.collection.create_pipeline", lambda cfg: mock_pipeline)

    cfg = SearchConfig()

    def _load_config(p: object) -> SearchConfig:
        return cfg

    monkeypatch.setattr("archon_search.cli.collection.load_config", _load_config)
    monkeypatch.setattr("archon_search.cli.collection.get_default_config_path", lambda: tmp_path / "archon-search.toml")

    runner = CliRunner()
    with caplog.at_level(logging.INFO, logger="archon_search"):
        result = runner.invoke(add, [str(tmp_path)])

    assert result.exit_code == 0, f"CLI exited with {result.exit_code}: {result.output}"

    records = _get_stage_timing_records(caplog)
    assert len(records) == 1, f"Expected 1 stage_timings record, got {len(records)}"
    rec = records[0]
    assert rec.endpoint == "ingest", f"endpoint should be 'ingest', got {rec.endpoint!r}"
    assert "total" in rec.stage_timings_ms, f"'total' key missing from {set(rec.stage_timings_ms)}"
    assert isinstance(rec.correlation_id, str), "correlation_id should be a string"
    assert len(rec.correlation_id) == 32, f"Expected 32-char hex, got {rec.correlation_id!r}"
    assert rec.correlation_id.isalnum(), f"correlation_id should be alphanumeric hex, got {rec.correlation_id!r}"


def test_cli_collection_add_disabled_no_stage_timings_log(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stage_timings_enabled=False → CLI `collection add` emits no stage_timings log record."""
    from archon_search.cli.collection import add

    mock_pipeline = _make_mock_pipeline()
    monkeypatch.setattr("archon_search.cli.collection.create_pipeline", lambda cfg: mock_pipeline)

    cfg = SearchConfig()
    cfg.observability.stage_timings_enabled = False
    monkeypatch.setattr("archon_search.cli.collection.load_config", lambda p: cfg)
    monkeypatch.setattr("archon_search.cli.collection.get_default_config_path", lambda: tmp_path / "archon-search.toml")

    runner = CliRunner()
    with caplog.at_level(logging.DEBUG, logger="archon_search"):
        result = runner.invoke(add, [str(tmp_path)])

    assert result.exit_code == 0, f"CLI exited with {result.exit_code}: {result.output}"
    records = _get_stage_timing_records(caplog)
    assert len(records) == 0, f"Expected 0 stage_timings records when disabled, got {len(records)}"


# ---------------------------------------------------------------------------
# CLI collection reindex command
# ---------------------------------------------------------------------------


def test_cli_collection_reindex_emits_stage_timings_log_record(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI `collection reindex` command emits one stage_timings log record with correlation_id."""
    from archon_search.cli.collection import reindex

    mock_pipeline = _make_mock_pipeline()
    monkeypatch.setattr("archon_search.cli.collection.create_pipeline", lambda cfg: mock_pipeline)

    # Config with a collection that matches the collection_name we'll pass
    cfg = SearchConfig()
    cfg.collections = [str(tmp_path)]

    def _load_config(p: object) -> SearchConfig:
        return cfg

    monkeypatch.setattr("archon_search.cli.collection.load_config", _load_config)

    # Mock the state store and store.drop_collection
    mock_pipeline.store.drop_collection = AsyncMock()

    # Patch IndexingStateStore to avoid filesystem operations
    import archon_search.cli.collection as col_module  # noqa: PLC0415
    mock_state_store = MagicMock()
    mock_state_store.remove_collection = MagicMock()
    monkeypatch.setattr(
        "archon_search.progress.IndexingStateStore",
        lambda path: mock_state_store,
    )

    from archon_search.sync import path_to_collection_name  # noqa: PLC0415
    collection_name = path_to_collection_name(str(tmp_path))

    runner = CliRunner()
    with caplog.at_level(logging.INFO, logger="archon_search"):
        result = runner.invoke(reindex, [collection_name])

    assert result.exit_code == 0, f"CLI exited with {result.exit_code}: {result.output}"

    records = _get_stage_timing_records(caplog)
    assert len(records) == 1, f"Expected 1 stage_timings record, got {len(records)}"
    rec = records[0]
    assert rec.endpoint == "ingest", f"endpoint should be 'ingest', got {rec.endpoint!r}"
    assert "total" in rec.stage_timings_ms, f"'total' key missing from {set(rec.stage_timings_ms)}"
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


def test_cli_collection_reindex_disabled_no_stage_timings_log(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stage_timings_enabled=False → CLI `collection reindex` emits no stage_timings log record."""
    from archon_search.cli.collection import reindex

    mock_pipeline = _make_mock_pipeline()
    monkeypatch.setattr("archon_search.cli.collection.create_pipeline", lambda cfg: mock_pipeline)

    cfg = SearchConfig()
    cfg.observability.stage_timings_enabled = False
    cfg.collections = [str(tmp_path)]
    monkeypatch.setattr("archon_search.cli.collection.load_config", lambda p: cfg)

    mock_pipeline.store.drop_collection = AsyncMock()
    mock_state_store = MagicMock()
    mock_state_store.remove_collection = MagicMock()
    monkeypatch.setattr("archon_search.progress.IndexingStateStore", lambda path: mock_state_store)

    from archon_search.sync import path_to_collection_name  # noqa: PLC0415
    collection_name = path_to_collection_name(str(tmp_path))

    runner = CliRunner()
    with caplog.at_level(logging.DEBUG, logger="archon_search"):
        result = runner.invoke(reindex, [collection_name])

    assert result.exit_code == 0, f"CLI exited with {result.exit_code}: {result.output}"
    records = _get_stage_timing_records(caplog)
    assert len(records) == 0, f"Expected 0 stage_timings records when disabled, got {len(records)}"
