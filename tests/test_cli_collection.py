"""Tests for CLI collection subcommands."""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
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


# ---------------------------------------------------------------------------
# Task 10.2 — CLI reindex per-collection embedding model resolution
# ---------------------------------------------------------------------------


def _make_reindex_pipeline(meta, ingest_results=None, ingest_raise=None):
    """Build a mock pipeline for the reindex command tests."""
    from archon_search._types import IngestResult

    pipeline = MagicMock()
    pipeline.store.connect = AsyncMock()
    pipeline.store.disconnect = AsyncMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.store.update_collection_meta = AsyncMock()
    pipeline.store.drop_collection = AsyncMock()
    pipeline._global_embedder = MagicMock(name="global_embedder")

    results = ingest_results or [IngestResult(doc_id="doc1", chunks_created=2, status="ok")]

    async def _ingest(*args, **kwargs):
        if ingest_raise is not None:
            raise ingest_raise
        return results

    pipeline.ingest_directory = AsyncMock(side_effect=_ingest)
    return pipeline


def _invoke_reindex(collection_name, meta, ingest_results=None, ingest_raise=None, config_collections=None):
    """Invoke the `reindex` CLI command with mocked pipeline."""
    from archon_search.collection_meta import CollectionMeta

    runner = CliRunner()
    pipeline = _make_reindex_pipeline(meta, ingest_results, ingest_raise)

    cfg = MagicMock()
    cfg.pinned_collections = []
    cfg.collections = config_collections or [f"/data/{collection_name}"]
    cfg.embedding_model = "global-model"
    cfg.db_path = "~/.archon-search"
    observability = MagicMock()
    observability.stage_timings_enabled = False
    cfg.observability = observability

    state_store_mock = MagicMock()
    state_store_mock.remove_collection = MagicMock()

    with (
        patch("archon_search.cli.collection.load_config", return_value=cfg),
        patch("archon_search.cli.collection.create_pipeline", return_value=pipeline),
        patch("archon_search.progress.IndexingStateStore", return_value=state_store_mock),
    ):
        result = runner.invoke(collection, ["reindex", collection_name])

    return result, pipeline


def test_cli_reindex_uses_pending_model_for_model_change() -> None:
    """When pending_embedding_model is set, reindex uses it and promotes it on success."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(
        name="mycol",
        active_embedding_model="old-model",
        pending_embedding_model="new-model",
        needs_reindex=True,
    )

    fake_embedder = MagicMock(name="new_embedder")

    with patch("archon_search.cli.collection.make_embedder", return_value=fake_embedder) as mock_make:
        result, pipeline = _invoke_reindex("mycol", meta)

    assert result.exit_code == 0, result.output

    # make_embedder called with the pending model
    mock_make.assert_called_once_with("new-model")

    # ingest_directory called with the new embedder
    _, kwargs = pipeline.ingest_directory.call_args
    assert kwargs.get("embedder") is fake_embedder

    # active promoted to pending on success
    update_call = pipeline.store.update_collection_meta.call_args[0][0]
    assert update_call.active_embedding_model == "new-model"
    assert update_call.pending_embedding_model is None
    assert update_call.needs_reindex is False
    assert update_call.reindex_job_id is None


def test_cli_reindex_uses_active_model_for_data_only() -> None:
    """When pending_embedding_model is None, reindex uses active_embedding_model."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(
        name="mycol",
        active_embedding_model="active-model",
        pending_embedding_model=None,
        needs_reindex=False,
    )

    fake_embedder = MagicMock(name="active_embedder")

    with patch("archon_search.cli.collection.make_embedder", return_value=fake_embedder) as mock_make:
        result, pipeline = _invoke_reindex("mycol", meta)

    assert result.exit_code == 0, result.output

    # make_embedder called with active model
    mock_make.assert_called_once_with("active-model")

    # ingest_directory called with that embedder
    _, kwargs = pipeline.ingest_directory.call_args
    assert kwargs.get("embedder") is fake_embedder

    # no state write (data-only reindex)
    pipeline.store.update_collection_meta.assert_not_called()


def test_cli_reindex_failure_leaves_state_unchanged() -> None:
    """If ingest_directory raises, state is NOT written back."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(
        name="mycol",
        active_embedding_model="old-model",
        pending_embedding_model="new-model",
        needs_reindex=True,
    )

    with patch("archon_search.cli.collection.make_embedder", return_value=MagicMock()):
        result, pipeline = _invoke_reindex("mycol", meta, ingest_raise=RuntimeError("boom"))

    # CLI exits with error (non-zero) or at least doesn't write state
    # The key invariant: update_collection_meta must NOT have been called
    pipeline.store.update_collection_meta.assert_not_called()

    # active/pending remain unchanged on the meta object
    assert meta.active_embedding_model == "old-model"
    assert meta.pending_embedding_model == "new-model"
    assert meta.needs_reindex is True


def test_cli_reindex_logs_warning_when_active_differs_from_global() -> None:
    """When active_embedding_model differs from config model, a warning is logged."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(
        name="mycol",
        active_embedding_model="custom-model",
        pending_embedding_model=None,
        needs_reindex=False,
    )

    with (
        patch("archon_search.cli.collection.make_embedder", return_value=MagicMock()),
        patch("archon_search.cli.collection.logger") as mock_logger,
    ):
        result, _ = _invoke_reindex("mycol", meta)

    assert result.exit_code == 0, result.output
    mock_logger.warning.assert_called_once()
    warning_msg = mock_logger.warning.call_args[0]
    assert "custom-model" in warning_msg[1]
    assert "mycol" in warning_msg[2]


def test_cli_reindex_meta_none_uses_global_embedder() -> None:
    """When get_collection_meta returns None, the global embedder is used and no state is written."""
    with patch("archon_search.cli.collection.make_embedder") as mock_make:
        result, pipeline = _invoke_reindex("mycol", meta=None)

    assert result.exit_code == 0, result.output
    # make_embedder must NOT be called — global embedder is used directly
    mock_make.assert_not_called()
    # No state write
    pipeline.store.update_collection_meta.assert_not_called()


def test_cli_reindex_empty_active_model_uses_global_embedder() -> None:
    """When active_embedding_model is empty string, the global embedder is used."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(
        name="mycol",
        active_embedding_model="",
        pending_embedding_model=None,
        needs_reindex=False,
    )

    with patch("archon_search.cli.collection.make_embedder") as mock_make:
        result, pipeline = _invoke_reindex("mycol", meta)

    assert result.exit_code == 0, result.output
    mock_make.assert_not_called()
    pipeline.store.update_collection_meta.assert_not_called()


# ---------------------------------------------------------------------------
# BE-5: collection migrate --dry-run subcommand
# ---------------------------------------------------------------------------


def _pending_response(collection: str, specs: list[dict], schema_version: int = 0) -> dict:
    """Build a MigrationPendingResponse-shaped dict."""
    return {
        "collection": collection,
        "pending": specs,
        "schema_version": schema_version,
    }


def _mock_http_response(status_code: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    return resp


def test_migrate_cli_dry_run_prints_pending() -> None:
    """--dry-run fetches GET /collections/{name}/migrations/pending and prints migration names."""
    runner = CliRunner()
    specs = [
        {"name": "migrate_namespace", "kind": "in_place", "description": "Add namespace column", "introduced_at": 0},
        {"name": "migrate_description_embedding", "kind": "in_place", "description": "Add description_embedding", "introduced_at": 0},
    ]
    get_resp = _mock_http_response(200, _pending_response("mycol", specs))

    with patch("archon_search.cli.collection.httpx.get", return_value=get_resp) as mock_get:
        result = runner.invoke(collection, ["migrate", "mycol", "--dry-run", "--api-key", "test-key"])

    assert result.exit_code == 0, result.output
    assert "migrate_namespace" in result.output
    assert "migrate_description_embedding" in result.output
    mock_get.assert_called_once()
    call_url = mock_get.call_args[0][0]
    assert "/collections/mycol/migrations/pending" in call_url
    _, call_kwargs = mock_get.call_args
    assert call_kwargs.get("headers", {}).get("Authorization") == "Bearer test-key"


def test_migrate_cli_no_flags_defaults_to_dry_run() -> None:
    """Running without flags behaves identically to --dry-run (prints pending, no mutation)."""
    runner = CliRunner()
    specs = [
        {"name": "migrate_acl", "kind": "in_place", "description": "Add ACL columns", "introduced_at": 0},
    ]
    get_resp = _mock_http_response(200, _pending_response("mycol", specs))

    with patch("archon_search.cli.collection.httpx.get", return_value=get_resp) as mock_get:
        with patch("archon_search.cli.collection.httpx.post") as mock_post:
            result = runner.invoke(collection, ["migrate", "mycol", "--api-key", "test-key"])

    assert result.exit_code == 0, result.output
    assert "migrate_acl" in result.output
    mock_get.assert_called_once()
    mock_post.assert_not_called()


def test_migrate_cli_empty_pending_prints_up_to_date() -> None:
    """When no migrations are pending, CLI prints an 'up to date' message."""
    runner = CliRunner()
    get_resp = _mock_http_response(200, _pending_response("mycol", []))

    with patch("archon_search.cli.collection.httpx.get", return_value=get_resp):
        result = runner.invoke(collection, ["migrate", "mycol", "--dry-run", "--api-key", "test-key"])

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output.lower() or "no pending" in result.output.lower()


def test_migrate_cli_404_prints_not_found() -> None:
    """404 response prints collection-not-found error and exits with code 1."""
    runner = CliRunner()
    get_resp = _mock_http_response(404, {"detail": "Collection 'mycol' not found"})

    with patch("archon_search.cli.collection.httpx.get", return_value=get_resp):
        result = runner.invoke(collection, ["migrate", "mycol", "--api-key", "test-key"])

    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_migrate_cli_connection_error_exits_1() -> None:
    """Connection failure prints error and exits with code 1."""
    runner = CliRunner()

    with patch("archon_search.cli.collection.httpx.get", side_effect=httpx.ConnectError("Connection refused")):
        result = runner.invoke(collection, ["migrate", "mycol", "--api-key", "test-key"])

    assert result.exit_code == 1
    assert "error contacting server" in result.output.lower()
