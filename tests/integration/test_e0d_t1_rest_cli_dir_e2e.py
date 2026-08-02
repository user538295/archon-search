"""T-1: E2e tests — REST 413, CLI non-zero exit, directory mixed-size batch.

Plan: Documentation/Backlog/e0d-pdf-large-file-support-team-plan.md Task T-1.

Tests:
- test_e2e_rest_413_single_file_over_limit
- test_e2e_cli_single_file_over_limit_exits_nonzero
- test_e2e_directory_mixed_sizes_oversized_skipped
- test_e2e_rest_directory_with_oversized_file_returns_202
- test_ingest_result_code_job_store_round_trip
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

ONE_MB = 1 * 1024 * 1024


# ---------------------------------------------------------------------------
# E2e test 1: REST 413 — single-file path over limit
# ---------------------------------------------------------------------------


def test_e2e_rest_413_single_file_over_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TestClient POST /ingest oversized single file → 413, message names both sizes and
    config key; no job created; no chunks in store.

    Completes: S2
    """
    toml_content = "[ingest]\nmax_file_mb = 10\n"
    oversized = tmp_path / "report.pdf"
    oversized.write_bytes(b"x")  # real file so is_file() passes

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        assert cfg.ingest.max_file_mb == 10

        with patch("os.path.getsize", return_value=20 * ONE_MB):
            resp = client.post(
                "/ingest",
                json={"collection": "reports", "path": str(oversized)},
                headers=headers,
            )

        assert resp.status_code == 413, f"Expected 413, got {resp.status_code}: {resp.text}"
        detail = resp.json()["detail"]
        # Message must name file size (20 MB), limit (10 MB), and config key
        assert "20 MB" in detail["message"], f"Expected '20 MB' in detail: {detail!r}"
        assert "10 MB" in detail["message"], f"Expected '10 MB' in detail: {detail!r}"
        assert "[ingest].max_file_mb" in detail["message"], (
            f"Expected config key in detail: {detail!r}"
        )

        # No job must have been created
        jobs_resp = client.get("/jobs", headers=headers)
        assert jobs_resp.status_code == 200
        assert jobs_resp.json()["items"] == [], (
            f"Expected no jobs after 413, got: {jobs_resp.json()['items']}"
        )

        # No chunks in store — collection either doesn't exist (404) or has empty results
        search_resp = client.post(
            "/search",
            json={"collection": "reports", "query": "test"},
            headers=headers,
        )
        assert search_resp.status_code in (200, 404), (
            f"Unexpected search status: {search_resp.status_code}: {search_resp.text}"
        )
        if search_resp.status_code == 200:
            assert search_resp.json()["results"] == [], (
                f"Expected no search results after 413; got: {search_resp.json()['results']!r}"
            )


# ---------------------------------------------------------------------------
# E2e test 2: CLI — single file over limit exits non-zero
# ---------------------------------------------------------------------------


def test_e2e_cli_single_file_over_limit_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CliRunner ingest --path oversized-file → server returns 413 → CLI exits non-zero;
    stderr contains actionable message from server.

    CSP120: ingest CLI is now a proxy; oversized-file rejection happens server-side.
    We mock httpx.post to return 413 with the server's error detail.

    Completes: S3
    """
    import httpx
    from click.testing import CliRunner
    from unittest.mock import MagicMock

    from archon_search.cli.ingest import ingest
    from archon_search._types import IngestError

    oversized = tmp_path / "bigfile.pdf"
    oversized.write_bytes(b"x")  # real file so is_file() passes

    err = IngestError(file_size_mb=5, limit_mb=1)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 413
    mock_resp.text = f'{{"detail": {{"code": "{err.code}", "message": "{err.message}"}}}}'

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "a" * 64)  # must be valid 64-char lowercase hex

    runner = CliRunner()
    with patch("archon_search.cli.ingest.httpx.post", return_value=mock_resp):
        result = runner.invoke(
            ingest,
            ["--path", str(oversized)],
        )

    assert result.exit_code != 0, (
        f"Expected non-zero exit for oversized single file, got {result.exit_code}. "
        f"stdout={result.output!r} stderr={result.stderr!r}"
    )
    assert "5 MB" in result.stderr, (
        f"Expected '5 MB' in stderr; stderr={result.stderr!r}"
    )
    assert "1 MB" in result.stderr, (
        f"Expected '1 MB' in stderr; stderr={result.stderr!r}"
    )
    assert "[ingest].max_file_mb" in result.stderr, (
        f"Expected '[ingest].max_file_mb' in stderr; stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# E2e test 3: directory with mixed sizes — oversized skipped, others indexed
# ---------------------------------------------------------------------------


def test_e2e_directory_mixed_sizes_oversized_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ingest_directory with 2 files (1 over limit, 1 under); under-limit file indexed;
    over-limit file has error IngestResult.

    Completes: S10
    """
    import hashlib

    toml_content = "[ingest]\nmax_file_mb = 1\n"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    small_file = corpus / "small.md"
    small_file.write_text("# Small\n\n" + "word " * 50 + "\n")
    big_file = corpus / "big.pdf"
    big_file.write_bytes(b"x")

    big_path_str = str(big_file.resolve())
    _real_getsize = os.path.getsize

    def _fake_getsize(path: str | bytes) -> int:
        if str(path) == big_path_str:
            return 2 * ONE_MB
        return _real_getsize(path)

    # Use monkeypatch so the patch persists through the background task execution
    monkeypatch.setattr("os.path.getsize", _fake_getsize)

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        assert cfg.ingest.max_file_mb == 1

        resp = client.post(
            "/ingest",
            json={"collection": "mixed", "path": str(corpus)},
            headers=headers,
        )

        # Directory ingest always returns 202 (no sync 413 at route level)
        assert resp.status_code == 202, f"Expected 202 for directory, got {resp.status_code}: {resp.text}"
        job_id = resp.json()["job_id"]

        # Poll job to completion
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=headers)
            assert r.status_code == 200
            if r.json()["status"] == "DONE":
                break
            if r.json()["status"] == "FAILED":
                pytest.fail(f"Job failed: {r.json()}")
            time.sleep(0.1)
        else:
            pytest.fail(f"Job did not complete in 10s: {r.json()}")

        # Small file should be indexed; big file should produce error result
        # Verify small file was indexed via search
        search_resp = client.post(
            "/search",
            json={"collection": "mixed", "query": "small"},
            headers=headers,
        )
        assert search_resp.status_code == 200
        results = search_resp.json()["results"]
        indexed_ids = [r["source_path"] for r in results]
        assert any(str(small_file) in str(sid) for sid in indexed_ids), (
            f"Expected small file indexed, got results: {results!r}"
        )

        # Over-limit file must have error IngestResult with code="file_too_large"
        job_data = r.json()
        result = job_data.get("result") or {}
        file_results = result.get("file_results", [])
        big_doc_id = hashlib.sha256(str(big_file.resolve()).encode()).hexdigest()
        big_result = next(
            (fr for fr in file_results if fr.get("doc_id") == big_doc_id),
            None,
        )
        assert big_result is not None, (
            f"Expected file_results entry for big.pdf; file_results={file_results!r}"
        )
        assert big_result.get("code") == "file_too_large", (
            f"Expected code='file_too_large' for oversized file; got: {big_result!r}"
        )


# ---------------------------------------------------------------------------
# E2e test 4: REST with directory path containing oversized file → 202
# ---------------------------------------------------------------------------


def test_e2e_rest_directory_with_oversized_file_returns_202(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /ingest with directory path containing 1 oversized + 1 normal file → 202
    accepted (not 413); oversized file has error IngestResult in job; normal file indexed.

    Completes: S10 (route-level behavior)
    """
    import hashlib

    toml_content = "[ingest]\nmax_file_mb = 1\n"
    corpus = tmp_path / "corpus2"
    corpus.mkdir()
    small_file = corpus / "normal.txt"
    small_file.write_text("Normal content.\n" * 20)
    big_file = corpus / "huge.pdf"
    big_file.write_bytes(b"x")

    big_path_str = str(big_file.resolve())
    _real_getsize = os.path.getsize

    def _fake_getsize(path: str | bytes) -> int:
        if str(path) == big_path_str:
            return 5 * ONE_MB
        return _real_getsize(path)

    # Use monkeypatch so the patch persists through the background task execution
    monkeypatch.setattr("os.path.getsize", _fake_getsize)

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.post(
            "/ingest",
            json={"collection": "dir-ingest", "path": str(corpus)},
            headers=headers,
        )

        # Must be 202 — directory path never produces a sync 413
        assert resp.status_code == 202, (
            f"Expected 202 for directory path, got {resp.status_code}: {resp.text}"
        )

        job_id = resp.json()["job_id"]

        # Poll to completion
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=headers)
            assert r.status_code == 200
            if r.json()["status"] == "DONE":
                break
            if r.json()["status"] == "FAILED":
                pytest.fail(f"Job failed: {r.json()}")
            time.sleep(0.1)
        else:
            pytest.fail(f"Job did not complete in 10s: {r.json()}")

        # Job must be DONE (not FAILED) — partial success is still DONE
        assert r.json()["status"] == "DONE"

        # Oversized file must have error IngestResult with code="file_too_large"
        job_data = r.json()
        result = job_data.get("result") or {}
        file_results = result.get("file_results", [])
        big_doc_id = hashlib.sha256(str(big_file.resolve()).encode()).hexdigest()
        big_result = next(
            (fr for fr in file_results if fr.get("doc_id") == big_doc_id),
            None,
        )
        assert big_result is not None, (
            f"Expected file_results entry for huge.pdf; file_results={file_results!r}"
        )
        assert big_result.get("code") == "file_too_large", (
            f"Expected code='file_too_large' for oversized file; got: {big_result!r}"
        )

        # Normal file must be indexed — search returns results
        search_resp = client.post(
            "/search",
            json={"collection": "dir-ingest", "query": "Normal content"},
            headers=headers,
        )
        assert search_resp.status_code == 200, (
            f"Search failed: {search_resp.status_code}: {search_resp.text}"
        )
        assert len(search_resp.json()["results"]) > 0, (
            f"Expected search results for normal.txt; got: {search_resp.json()['results']!r}"
        )


# ---------------------------------------------------------------------------
# Integration test 5: per-file code="file_too_large" survives job result round-trip
# ---------------------------------------------------------------------------


def test_ingest_result_code_job_store_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ingest_directory with oversized file → job completes → GET /jobs/{id};
    per-file result contains code="file_too_large" (verifies code survives
    job_to_dict() JSON serialization).

    The mock for os.path.getsize must remain active while the background task
    runs. We therefore use monkeypatch.setattr so it lasts for the full test.
    """
    import hashlib

    toml_content = "[ingest]\nmax_file_mb = 1\n"
    corpus = tmp_path / "corpus3"
    corpus.mkdir()
    big_file = corpus / "big.pdf"
    big_file.write_bytes(b"x")
    small_file = corpus / "small.md"
    small_file.write_text("# Small doc\n\nSome content.\n")

    big_path_str = str(big_file.resolve())

    _real_getsize = os.path.getsize

    def _fake_getsize(path: str | bytes) -> int:
        if str(path) == big_path_str:
            return 3 * ONE_MB
        return _real_getsize(path)

    # Patch os.path.getsize for the entire test so the background task sees it.
    monkeypatch.setattr("os.path.getsize", _fake_getsize)

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.post(
            "/ingest",
            json={"collection": "round-trip", "path": str(corpus)},
            headers=headers,
        )

        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=headers)
            assert r.status_code == 200
            if r.json()["status"] == "DONE":
                break
            if r.json()["status"] in ("FAILED",):
                pytest.fail(f"Job failed: {r.json()}")
            time.sleep(0.1)
        else:
            pytest.fail(f"Job did not complete in 10s: {r.json()}")

        job_data = r.json()
        assert job_data["status"] == "DONE"

        # The job result must contain per-file code information
        result = job_data.get("result") or {}
        file_results = result.get("file_results", [])

        # Find the big file's result using doc_id (sha256 of resolved path)
        big_doc_id = hashlib.sha256(str(big_file.resolve()).encode()).hexdigest()
        big_result = next(
            (fr for fr in file_results if fr.get("doc_id") == big_doc_id),
            None,
        )
        assert big_result is not None, (
            f"Expected file result for big.pdf (doc_id={big_doc_id!r}) in job result; "
            f"file_results={file_results!r}"
        )
        assert big_result.get("code") == "file_too_large", (
            f"Expected code='file_too_large' for oversized file; "
            f"got: {big_result!r}"
        )
