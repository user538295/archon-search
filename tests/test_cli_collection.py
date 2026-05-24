"""Tests for Task 5.1: --normalize-timestamps flag on reindex-metadata CLI.

Implements Task 5.1 of A2 plan.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli.collection import collection
from archon_search.store import ReindexResult, _FIXED_WIDTH_TS_RE


# ---------------------------------------------------------------------------
# 1. Pure regex contract tests (no I/O)
# ---------------------------------------------------------------------------


def test_legacy_format_regex_rejects_known_legacy_shapes() -> None:
    """The strict fixed-width regex must reject non-canonical forms."""
    from datetime import datetime, timezone
    from archon_search._types import normalize_iso_utc

    rejects = [
        "2026-05-21T10:00:00Z",         # no microseconds
        "2026-05-21T10:00:00+00:00",    # offset notation
        "2026-05-21T10:00:00.123Z",     # 3-digit microseconds (milliseconds only)
        "2026-05-21T10:00:00",          # no tz at all
    ]
    accepts = [
        "2026-05-21T10:00:00.000000Z",  # canonical fixed-width
    ]
    for s in rejects:
        assert not _FIXED_WIDTH_TS_RE.match(s), f"Expected regex to reject: {s!r}"
    for s in accepts:
        assert _FIXED_WIDTH_TS_RE.match(s), f"Expected regex to accept: {s!r}"

    # normalize_iso_utc always produces output accepted by the regex
    normalized = normalize_iso_utc(datetime.now(timezone.utc))
    assert _FIXED_WIDTH_TS_RE.match(normalized), (
        f"normalize_iso_utc output {normalized!r} not accepted by _FIXED_WIDTH_TS_RE"
    )


# ---------------------------------------------------------------------------
# Helpers shared by unit tests below
# ---------------------------------------------------------------------------


def _make_pipeline_patch(reindex_result=None, raise_=None, progress=None):
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
        return (
            runner.invoke(collection, ["reindex-metadata", "my-col", *extra_args]),
            pipeline,
        )


# ---------------------------------------------------------------------------
# 2. Unit test: --dry-run reports count, writes nothing
# ---------------------------------------------------------------------------


def test_reindex_metadata_no_normalize_timestamps_passes_false_to_store() -> None:
    """--no-normalize-timestamps must pass normalize_timestamps=False to store."""
    result, pipeline = _invoke(
        reindex_result=ReindexResult(processed=2, updated=0, skipped=0, ts_normalized=0),
        extra_args=("--no-normalize-timestamps",),
    )
    assert result.exit_code == 0, result.output
    _, kwargs = pipeline.store.reindex_metadata.call_args
    assert kwargs.get("normalize_timestamps") is False


def test_reindex_metadata_normalize_timestamps_dry_run_reports_count() -> None:
    """--dry-run with --normalize-timestamps reports ts_normalized count; store not mutated."""
    result, pipeline = _invoke(
        reindex_result=ReindexResult(processed=5, updated=0, skipped=0, ts_normalized=3),
        extra_args=("--dry-run", "--normalize-timestamps"),
    )
    assert result.exit_code == 0, result.output
    # dry_run must be passed through
    _, kwargs = pipeline.store.reindex_metadata.call_args
    assert kwargs.get("dry_run") is True
    # normalize_timestamps must be passed through
    assert kwargs.get("normalize_timestamps") is True
    # The CLI must surface ts_normalized in output
    assert "ts_normalized=3" in result.output


# ---------------------------------------------------------------------------
# Integration tests (require a real LanceDB store)
# ---------------------------------------------------------------------------

_DIM = 4


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk_record(source_path: str, **overrides):
    from datetime import datetime, timezone

    from archon_search._types import ChunkRecord

    did = overrides.pop("doc_id", _doc_id())
    return ChunkRecord(
        doc_id=did,
        chunk_id=overrides.pop("chunk_id", f"{did}-000000"),
        text=overrides.pop("text", "timestamp normalization test"),
        vector=overrides.pop("vector", [0.0] * _DIM),
        source_path=source_path,
        indexed_at=overrides.pop("indexed_at", datetime.now(timezone.utc).isoformat()),
        ingested_by=overrides.pop("ingested_by", "cli"),
        **overrides,
    )


async def _force_legacy_timestamps(store, col: str, chunk_id: str) -> None:
    """Inject legacy (non-fixed-width) timestamp values into a row."""
    db = store._require_connected()
    table = await db.open_table(col)
    await table.update(
        where=f"chunk_id = '{chunk_id}'",
        updates={
            "indexed_at": "2026-05-21T10:00:00Z",
            "updated_at": "2026-05-21T10:00:00+00:00",
        },
    )


async def _read_raw(store, col: str, chunk_id: str) -> dict:
    db = store._require_connected()
    table = await db.open_table(col)
    rows = await table.query().where(f"chunk_id = '{chunk_id}'").to_list()
    assert len(rows) == 1
    return rows[0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_metadata_normalize_timestamps_rewrites_legacy_rows(
    connected_store,
    tmp_path: Path,
) -> None:
    """Legacy timestamp formats are rewritten to fixed-width after reindex-metadata."""
    col = f"test-ts-{uuid.uuid4().hex[:8]}"
    await connected_store.ensure_collection(col, _DIM)

    src = tmp_path / "doc.md"
    src.write_text("hello")
    chunk = _chunk_record(str(src))
    await connected_store.ingest_chunks(col, [chunk])
    await _force_legacy_timestamps(connected_store, col, chunk.chunk_id)

    result = await connected_store.reindex_metadata(col, normalize_timestamps=True)
    assert result.ts_normalized >= 1

    row = await _read_raw(connected_store, col, chunk.chunk_id)
    assert _FIXED_WIDTH_TS_RE.match(row["indexed_at"]), f"indexed_at not fixed-width: {row['indexed_at']!r}"
    assert not row["updated_at"] or _FIXED_WIDTH_TS_RE.match(row["updated_at"]), (
        f"updated_at not fixed-width: {row['updated_at']!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_metadata_normalize_timestamps_idempotent(
    connected_store,
    tmp_path: Path,
) -> None:
    """Running reindex-metadata --normalize-timestamps twice is a no-op on second run."""
    col = f"test-ts-idem-{uuid.uuid4().hex[:8]}"
    await connected_store.ensure_collection(col, _DIM)

    src = tmp_path / "doc2.md"
    src.write_text("world")
    chunk = _chunk_record(str(src))
    await connected_store.ingest_chunks(col, [chunk])
    await _force_legacy_timestamps(connected_store, col, chunk.chunk_id)

    # First run — should normalize
    r1 = await connected_store.reindex_metadata(col, normalize_timestamps=True)
    assert r1.ts_normalized >= 1

    # Second run — should find nothing to normalize
    r2 = await connected_store.reindex_metadata(col, normalize_timestamps=True)
    assert r2.ts_normalized == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_metadata_normalize_timestamps_progress_logged(
    connected_store,
    tmp_path: Path,
) -> None:
    """Progress callback fires during timestamp normalization pass."""
    col = f"test-ts-prog-{uuid.uuid4().hex[:8]}"
    await connected_store.ensure_collection(col, _DIM)

    src = tmp_path / "doc3.md"
    src.write_text("progress check")
    chunk = _chunk_record(str(src))
    await connected_store.ingest_chunks(col, [chunk])
    await _force_legacy_timestamps(connected_store, col, chunk.chunk_id)

    calls: list[tuple[int, int]] = []
    await connected_store.reindex_metadata(
        col,
        normalize_timestamps=True,
        progress_cb=lambda p, t: calls.append((p, t)),
    )
    assert calls, "progress_cb must be invoked at least once"
    assert calls[-1][0] == calls[-1][1]  # processed == total at final call
