"""Tests for CLI collection subcommands."""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


# ---------------------------------------------------------------------------
# BE-8: collection migrate --apply flag (in-place sync path)
# ---------------------------------------------------------------------------


def test_migrate_cli_apply_in_place_prints_summary() -> None:
    """--apply calls POST /collections/{name}/migrate and prints applied migration names."""
    runner = CliRunner()
    post_resp = _mock_http_response(200, {"migrations_applied": ["migrate_namespace", "migrate_acl"]})

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp) as mock_post:
        result = runner.invoke(collection, ["migrate", "mycol", "--apply", "--api-key", "test-key"])

    assert result.exit_code == 0, result.output
    assert "migrate_namespace" in result.output
    assert "migrate_acl" in result.output
    mock_post.assert_called_once()
    call_url = mock_post.call_args[0][0]
    assert "/collections/mycol/migrate" in call_url
    _, call_kwargs = mock_post.call_args
    assert call_kwargs.get("headers", {}).get("Authorization") == "Bearer test-key"
    assert call_kwargs.get("json") == {"dry_run": False, "backup_confirmed": False}


def test_migrate_cli_apply_and_dry_run_mutually_exclusive() -> None:
    """Passing both --apply and --dry-run raises a usage error and exits non-zero."""
    runner = CliRunner()

    result = runner.invoke(collection, ["migrate", "mycol", "--apply", "--dry-run", "--api-key", "test-key"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_migrate_cli_apply_empty_migrations_prints_up_to_date() -> None:
    """--apply with no pending migrations prints an 'up to date' message."""
    runner = CliRunner()
    post_resp = _mock_http_response(200, {"migrations_applied": []})

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp):
        result = runner.invoke(collection, ["migrate", "mycol", "--apply", "--api-key", "test-key"])

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output.lower() or "no migrations" in result.output.lower()


def test_migrate_cli_apply_404_prints_not_found() -> None:
    """--apply with 404 response prints collection-not-found error and exits 1."""
    runner = CliRunner()
    post_resp = _mock_http_response(404, {"detail": "Not found"})

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp):
        result = runner.invoke(collection, ["migrate", "mycol", "--apply", "--api-key", "test-key"])

    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_migrate_cli_apply_connection_error_exits_1() -> None:
    """--apply with connection failure prints error and exits 1."""
    runner = CliRunner()

    with patch("archon_search.cli.collection.httpx.post", side_effect=httpx.ConnectError("Connection refused")):
        result = runner.invoke(collection, ["migrate", "mycol", "--apply", "--api-key", "test-key"])

    assert result.exit_code == 1
    assert "error contacting server" in result.output.lower()


# ---------------------------------------------------------------------------
# BE-14: collection migrate --backup-first --wait flags (rewrite async path)
# ---------------------------------------------------------------------------


def _job_response(job_id: str, status: str, phase: str = "", processed: int = 0, total: int = 0) -> dict:
    """Build a minimal GET /jobs/{id} response dict."""
    body: dict = {"job_id": job_id, "status": status}
    if phase or processed or total:
        body["progress"] = {"phase": phase, "processed": processed, "total": total}
    return body


def test_migrate_cli_wait_polls_until_done_exits_0() -> None:
    """--apply --backup-first --wait polls job until DONE and exits 0; progress output printed."""
    runner = CliRunner()
    job_id = "job-abc-123"

    # POST /migrate returns 202 with job_id (rewrite job created)
    post_resp = _mock_http_response(202, {"job_id": job_id, "status": "RUNNING"})

    # GET /jobs/{id} returns QUEUED → RUNNING (with progress) → DONE
    get_job_sequence = [
        _mock_http_response(200, _job_response(job_id, "QUEUED")),
        _mock_http_response(200, _job_response(job_id, "RUNNING", phase="rewrite", processed=100, total=250)),
        _mock_http_response(200, _job_response(job_id, "DONE")),
    ]

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp) as mock_post,
        patch("archon_search.cli._helpers.httpx.get", side_effect=get_job_sequence),
        patch("archon_search.cli._helpers.time.sleep"),  # no real sleeping in tests
    ):
        result = runner.invoke(
            collection,
            ["migrate", "mycol", "--apply", "--backup-first", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    # Progress line should appear: "rewrite: 100/250"
    assert "100/250" in result.output
    assert "rewrite" in result.output.lower()
    # --backup-first must send backup_confirmed: True in POST body
    _, post_kwargs = mock_post.call_args
    assert post_kwargs.get("json", {}).get("backup_confirmed") is True


def test_migrate_cli_wait_exits_1_on_failed() -> None:
    """--wait exits with code 1 when the job reaches FAILED status."""
    runner = CliRunner()
    job_id = "job-fail-999"

    post_resp = _mock_http_response(202, {"job_id": job_id, "status": "RUNNING"})
    get_job_sequence = [
        _mock_http_response(200, _job_response(job_id, "RUNNING", phase="rewrite", processed=50, total=200)),
        _mock_http_response(200, _job_response(job_id, "FAILED")),
    ]

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=get_job_sequence),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            collection,
            ["migrate", "mycol", "--apply", "--backup-first", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code == 1


def test_migrate_cli_wait_exits_1_on_cancelled() -> None:
    """--wait exits with code 1 when the job reaches CANCELLED status."""
    runner = CliRunner()
    job_id = "job-cancel-999"

    post_resp = _mock_http_response(202, {"job_id": job_id, "status": "RUNNING"})
    get_job_sequence = [
        _mock_http_response(200, _job_response(job_id, "RUNNING", phase="rewrite", processed=50, total=200)),
        _mock_http_response(200, _job_response(job_id, "CANCELLED")),
    ]

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=get_job_sequence),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            collection,
            ["migrate", "mycol", "--apply", "--backup-first", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code == 1


def test_migrate_cli_backup_first_without_wait_prints_job_id() -> None:
    """--apply --backup-first without --wait submits job and prints job_id; no polling occurs."""
    runner = CliRunner()

    post_resp = _mock_http_response(202, {"job_id": "job-xyz", "status": "RUNNING"})

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch("archon_search.cli.collection.httpx.get") as mock_get,
    ):
        result = runner.invoke(
            collection,
            ["migrate", "mycol", "--apply", "--backup-first", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    assert "job-xyz" in result.output
    mock_get.assert_not_called()


def test_migrate_cli_wait_poll_error_exits_1() -> None:
    """When GET /jobs returns 500 during polling, CLI exits 1 with an error message."""
    runner = CliRunner()

    post_resp = _mock_http_response(202, {"job_id": "job-poll-err", "status": "RUNNING"})
    get_resp_500 = _mock_http_response(500, {"detail": "internal server error"})
    get_resp_500.text = "internal server error"

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", return_value=get_resp_500),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            collection,
            ["migrate", "mycol", "--apply", "--backup-first", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "error" in result.output.lower() or "500" in result.output


def test_migrate_cli_backup_first_required_for_rewrite() -> None:
    """When rewrite migration is pending and --backup-first is omitted, server returns 422; CLI exits non-zero."""
    runner = CliRunner()
    # Server rejects because backup_confirmed is False/absent
    post_resp = _mock_http_response(422, {"detail": "backup_confirmed required for rewrite migrations"})

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp):
        result = runner.invoke(
            collection,
            ["migrate", "mycol", "--apply", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code != 0
    # CLI should propagate the error message
    assert "422" in result.output or "backup_confirmed" in result.output.lower() or "error" in result.output.lower()


def test_migrate_cli_wait_without_apply_is_error() -> None:
    """--wait without --apply is a usage error (cannot poll a job that hasn't been created)."""
    runner = CliRunner()

    result = runner.invoke(
        collection,
        ["migrate", "mycol", "--wait", "--api-key", "test-key"],
    )

    assert result.exit_code != 0


def test_migrate_cli_backup_first_without_apply_is_error() -> None:
    """--backup-first without --apply is a usage error; prints a clear error and exits 1."""
    runner = CliRunner()

    result = runner.invoke(
        collection,
        ["migrate", "mycol", "--backup-first", "--api-key", "test-key"],
    )

    assert result.exit_code == 1
    assert "--backup-first requires --apply" in result.output


# ---------------------------------------------------------------------------
# FE-4: collection add — httpx proxy tests
# ---------------------------------------------------------------------------


def _add_job_response(job_id: str, status: str, collection_name: str = "my_docs") -> dict:
    """Build a minimal 202 /collections POST or GET /jobs/{id} response dict."""
    return {"job_id": job_id, "status": status, "collection": collection_name}


def test_add_submits_job_prints_id_and_server_collection_name() -> None:
    """Mocked 202 with 'collection' field → job_id + server-derived collection name printed, exit 0."""
    runner = CliRunner()
    job_id = "job-add-001"
    post_resp = MagicMock()
    post_resp.status_code = 202
    post_resp.json.return_value = _add_job_response(job_id, "QUEUED", "my_docs")

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp) as mock_post:
        result = runner.invoke(
            collection,
            ["add", "/some/path/my docs", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    assert job_id in result.output
    assert "my_docs" in result.output
    mock_post.assert_called_once()
    call_url, call_kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
    assert call_url == "http://localhost:8765/collections/"
    # Must include the path in the request body
    posted_json = call_kwargs.get("json", {})
    assert posted_json.get("path") == "/some/path/my docs"
    # Must send auth header
    assert call_kwargs.get("headers", {}).get("Authorization") == "Bearer test-key"


def test_add_with_wait_polls_to_done() -> None:
    """Mocked poll → completion, exit 0."""
    runner = CliRunner()
    job_id = "job-add-002"

    post_resp = MagicMock()
    post_resp.status_code = 202
    post_resp.json.return_value = _add_job_response(job_id, "QUEUED", "my_collection")

    get_job_sequence = [
        MagicMock(status_code=200, json=lambda: {"job_id": job_id, "status": "RUNNING"}),
        MagicMock(status_code=200, json=lambda: {"job_id": job_id, "status": "DONE"}),
    ]

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=get_job_sequence),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            collection,
            ["add", "/some/path", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    assert job_id in result.output
    # Completion message must appear — proves the RUNNING→DONE poll actually ran
    assert "ingested successfully" in result.output.lower()


def test_add_does_not_call_load_config() -> None:
    """add must not call load_config — it is a pure HTTP proxy."""
    import archon_search.cli.collection as col_mod

    post_resp = MagicMock()
    post_resp.status_code = 202
    post_resp.json.return_value = _add_job_response("job-add-003", "QUEUED", "mypath")

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch.object(col_mod, "load_config") as mock_load_config,
    ):
        runner = CliRunner()
        result = runner.invoke(collection, ["add", "/some/path", "--api-key", "test-key"])

    assert result.exit_code == 0, result.output
    mock_load_config.assert_not_called()


def test_add_409_collection_already_registered() -> None:
    """409 → specific error message, exit 1."""
    runner = CliRunner()

    post_resp = MagicMock()
    post_resp.status_code = 409
    post_resp.text = '{"detail": "collection already registered"}'
    post_resp.json.return_value = {"detail": "collection already registered"}

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp):
        result = runner.invoke(
            collection,
            ["add", "/some/path", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "already" in result.output.lower() or "409" in result.output


def test_add_server_not_running_exits_1() -> None:
    """ConnectError → human-readable error, exit 1."""
    runner = CliRunner()

    with patch(
        "archon_search.cli.collection.httpx.post",
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        result = runner.invoke(
            collection,
            ["add", "/some/path", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "not running" in result.output.lower() or "start it first" in result.output.lower()


def test_add_503_prints_error_exits_1() -> None:
    """503 from server → error printed, exit 1."""
    runner = CliRunner()

    post_resp = MagicMock()
    post_resp.status_code = 503
    post_resp.text = "store busy"

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp):
        result = runner.invoke(
            collection,
            ["add", "/some/path", "--api-key", "test-key"],
        )

    assert result.exit_code == 1


def test_add_generic_http_error_exits_1() -> None:
    """Generic HTTPError (e.g. ReadTimeout) → 'Error contacting server' message, exit 1."""
    runner = CliRunner()

    with patch(
        "archon_search.cli.collection.httpx.post",
        side_effect=httpx.ReadTimeout("Read timeout"),
    ):
        result = runner.invoke(
            collection,
            ["add", "/some/path", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "error contacting server" in result.output.lower()
