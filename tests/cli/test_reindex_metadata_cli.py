"""Tests for ``archon-search collection reindex-metadata <name>`` CLI.

Implements Task 6.3 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli.collection import collection
from archon_search.store import ReindexResult


def _make_pipeline_patch(reindex_result=None, raise_=None, progress=None):
    """Build a context-managed patch for create_pipeline + load_config."""
    pipeline = MagicMock()
    pipeline.store.connect = AsyncMock()
    pipeline.store.disconnect = AsyncMock()

    async def _reindex(*args, **kwargs):
        if raise_ is not None:
            raise raise_
        cb = kwargs.get("progress_cb")
        if cb is not None and progress is not None:
            for p, t in progress:
                cb(p, t)
        return reindex_result or ReindexResult(processed=0, updated=0)

    pipeline.store.reindex_metadata = AsyncMock(side_effect=_reindex)
    return pipeline


def _invoke(reindex_result=None, raise_=None, progress=None, extra_args=()):
    runner = CliRunner()
    pipeline = _make_pipeline_patch(reindex_result, raise_, progress)
    with (
        patch("archon_search.cli.collection.load_config", return_value=MagicMock()),
        patch("archon_search.cli.collection.create_pipeline", return_value=pipeline),
    ):
        return runner.invoke(collection, ["reindex-metadata", "my-col", *extra_args]), pipeline


def test_reindex_metadata_invokes_store() -> None:
    result, pipeline = _invoke(
        reindex_result=ReindexResult(processed=5, updated=3, skipped=2, warnings=[])
    )
    assert result.exit_code == 0, result.output
    pipeline.store.reindex_metadata.assert_awaited_once()
    _, kwargs = pipeline.store.reindex_metadata.call_args
    assert kwargs.get("dry_run") is False
    pos_args = pipeline.store.reindex_metadata.call_args.args
    assert "my-col" in pos_args


def test_reindex_metadata_dry_run_flag() -> None:
    result, pipeline = _invoke(
        reindex_result=ReindexResult(processed=5, updated=0),
        extra_args=("--dry-run",),
    )
    assert result.exit_code == 0, result.output
    _, kwargs = pipeline.store.reindex_metadata.call_args
    assert kwargs.get("dry_run") is True


def test_reindex_metadata_unknown_collection_exits_1() -> None:
    result, _ = _invoke(raise_=ValueError("collection 'my-col' not found"))
    assert result.exit_code == 1
    assert "not found" in result.output.lower() or "error" in result.output.lower()


def test_reindex_metadata_prints_progress() -> None:
    result, _ = _invoke(
        reindex_result=ReindexResult(processed=100, updated=10),
        progress=[(50, 100), (100, 100)],
    )
    assert result.exit_code == 0, result.output
    assert "50/100" in result.output
    assert "100/100" in result.output


def test_reindex_metadata_prints_warnings() -> None:
    result, _ = _invoke(
        reindex_result=ReindexResult(
            processed=1, updated=1,
            warnings=["missing-source: /tmp/gone.md"],
        )
    )
    assert result.exit_code == 0, result.output
    assert "warnings:" in result.output.lower()
    assert "missing-source" in result.output
