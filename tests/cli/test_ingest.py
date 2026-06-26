"""Tests for archon-search ingest CLI command — path migration."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from archon_search._types import IngestResult
from archon_search.cli.ingest import ingest


@pytest.mark.archon_unset_data_dir
def test_default_ingest_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ingest path must be ~/.archon-search/history/sessions."""
    runner = CliRunner()

    captured_paths: list[Path] = []

    async def fake_run() -> None:
        pass

    import asyncio

    def fake_create_pipeline(cfg: object) -> object:  # noqa: ARG001
        raise SystemExit(0)  # abort early — we only care about path echo

    monkeypatch.setattr("archon_search.cli.ingest.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("archon_search.cli.ingest.load_config", lambda p: object())

    result = runner.invoke(ingest, [])
    expected = str(Path.home() / ".archon-search" / "history" / "sessions")
    assert expected in result.output, (
        f"Expected default path {expected!r} in output, got: {result.output!r}"
    )


def test_ingest_cli_prints_warnings_to_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FE-4: warnings in IngestResult must be printed to stderr after ingest."""
    runner = CliRunner()

    warning_msg = "ACL sidecar /some/file.acl exceeds 64 KB limit; ACL not applied"
    results = [
        IngestResult(doc_id="doc1", chunks_created=1, status="ok", warnings=[warning_msg]),
        IngestResult(doc_id="doc2", chunks_created=2, status="ok", warnings=[]),
    ]

    # Build a config mock with observability.stage_timings_enabled = False
    observability_mock = MagicMock()
    observability_mock.stage_timings_enabled = False
    cfg_mock = MagicMock()
    cfg_mock.observability = observability_mock

    pipeline_mock = MagicMock()
    pipeline_mock.store.connect = AsyncMock()
    pipeline_mock.store.disconnect = AsyncMock()
    pipeline_mock.ingest_directory = AsyncMock(return_value=results)
    pipeline_mock._global_embedder = None

    monkeypatch.setattr("archon_search.cli.ingest.load_config", lambda p: cfg_mock)
    monkeypatch.setattr(
        "archon_search.cli.ingest.create_pipeline", lambda cfg: pipeline_mock
    )

    ingest_dir = tmp_path / "docs"
    ingest_dir.mkdir()
    result = runner.invoke(ingest, ["--path", str(ingest_dir)])

    assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}: {result.output}"
    assert warning_msg in result.stderr, (
        f"Expected warning in stderr, got: {result.stderr!r}"
    )
