"""FE-1 unit tests for E0d: single-file CLI mode, large-file notice, and file-too-large error."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search._types import IngestResult
from archon_search.cli.ingest import ingest


def _make_cfg_mock() -> MagicMock:
    observability = MagicMock()
    observability.stage_timings_enabled = False
    cfg = MagicMock()
    cfg.observability = observability
    return cfg


def _make_pipeline_mock(ingest_file_result: IngestResult) -> MagicMock:
    pipeline = MagicMock()
    pipeline.store.connect = AsyncMock()
    pipeline.store.disconnect = AsyncMock()
    pipeline.ingest_file = AsyncMock(return_value=ingest_file_result)
    pipeline._global_embedder = None
    return pipeline


def test_cli_ingest_single_file_path_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--path <file> routes to pipeline.ingest_file(), not ingest_directory()."""
    runner = CliRunner()
    test_file = tmp_path / "doc.txt"
    test_file.write_text("hello")

    ok_result = IngestResult(doc_id="doc", chunks_created=1, status="ok")
    pipeline_mock = _make_pipeline_mock(ok_result)

    monkeypatch.setattr("archon_search.cli.ingest.load_config", lambda p: _make_cfg_mock())
    monkeypatch.setattr("archon_search.cli.ingest.create_pipeline", lambda cfg: pipeline_mock)

    result = runner.invoke(ingest, ["--path", str(test_file)])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    pipeline_mock.ingest_file.assert_awaited_once()
    pipeline_mock.ingest_directory.assert_not_called()


def test_cli_large_file_notice_printed_to_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Files > 10 MB produce a pre-parse notice on stderr before parsing begins (S9)."""
    runner = CliRunner()
    test_file = tmp_path / "big.pdf"
    test_file.write_text("data")

    ok_result = IngestResult(doc_id="big", chunks_created=5, status="ok")
    pipeline_mock = _make_pipeline_mock(ok_result)

    monkeypatch.setattr("archon_search.cli.ingest.load_config", lambda p: _make_cfg_mock())
    monkeypatch.setattr("archon_search.cli.ingest.create_pipeline", lambda cfg: pipeline_mock)

    # 11 MB — above the _LARGE_FILE_NOTICE_MB = 10 threshold
    large_size = 11 * 1024 * 1024
    with patch("archon_search.cli.ingest.os.path.getsize", return_value=large_size):
        result = runner.invoke(ingest, ["--path", str(test_file)])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert "this may take a while" in result.stderr, (
        f"Expected large-file notice in stderr, got: {result.stderr!r}"
    )


def test_cli_small_file_no_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Files ≤ 10 MB produce no large-file notice."""
    runner = CliRunner()
    test_file = tmp_path / "small.pdf"
    test_file.write_text("data")

    ok_result = IngestResult(doc_id="small", chunks_created=2, status="ok")
    pipeline_mock = _make_pipeline_mock(ok_result)

    monkeypatch.setattr("archon_search.cli.ingest.load_config", lambda p: _make_cfg_mock())
    monkeypatch.setattr("archon_search.cli.ingest.create_pipeline", lambda cfg: pipeline_mock)

    # 5 MB — below the _LARGE_FILE_NOTICE_MB = 10 threshold
    small_size = 5 * 1024 * 1024
    with patch("archon_search.cli.ingest.os.path.getsize", return_value=small_size):
        result = runner.invoke(ingest, ["--path", str(test_file)])

    assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
    assert "this may take a while" not in result.stderr, (
        f"Expected no large-file notice in stderr, got: {result.stderr!r}"
    )


def test_cli_file_too_large_error_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """IngestResult(code='file_too_large') prints result.error to stderr and exits 1."""
    runner = CliRunner()
    test_file = tmp_path / "huge.pdf"
    test_file.write_text("data")

    error_msg = (
        "File size 60 MB exceeds the configured limit of 50 MB "
        "(`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file."
    )
    error_result = IngestResult(
        doc_id="huge",
        chunks_created=0,
        status="error",
        code="file_too_large",
        error=error_msg,
    )
    pipeline_mock = _make_pipeline_mock(error_result)

    monkeypatch.setattr("archon_search.cli.ingest.load_config", lambda p: _make_cfg_mock())
    monkeypatch.setattr("archon_search.cli.ingest.create_pipeline", lambda cfg: pipeline_mock)

    result = runner.invoke(ingest, ["--path", str(test_file)])

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}: {result.output}"
    assert error_msg in result.stderr, (
        f"Expected error message in stderr, got: {result.stderr!r}"
    )
