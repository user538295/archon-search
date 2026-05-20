"""Tests for archon-search ingest CLI command — Task 1.7 path migration."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from archon_search.cli.ingest import ingest


def test_default_ingest_path(monkeypatch: "pytest.MonkeyPatch") -> None:  # type: ignore[name-defined]
    """Default ingest path must be ~/.archon-search/history/sessions."""
    import pytest
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
