"""BE-4: Sync 413 pre-check in POST /ingest route.

Tests for the synchronous HTTP 413 size guard in the POST /ingest route handler.
The pre-check fires only for single-file paths, before job_store.create().
Directory paths and `documents` payloads skip the check entirely.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app

TEST_KEY = os.environ.get("ARCHON_SEARCH_API_KEY", "0" * 64)
AUTH = {"Authorization": f"Bearer {TEST_KEY}"}

ONE_MB = 1 * 1024 * 1024


def _make_client(tmp_path: Path, max_file_mb: int = 0) -> tuple[TestClient, JobStore]:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.ingest.max_file_mb = max_file_mb
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    return TestClient(app, headers=AUTH), job_store


# ---------------------------------------------------------------------------
# Unit tests — 413 path
# ---------------------------------------------------------------------------


def test_ingest_route_413_single_file_over_limit(tmp_path: Path) -> None:
    """POST /ingest with oversized single-file path and max_file_mb set → 413 + actionable detail."""
    oversized = tmp_path / "big.pdf"
    oversized.write_bytes(b"x")  # real file so is_file() passes

    client, job_store = _make_client(tmp_path, max_file_mb=1)

    # Patch os.path.getsize so the file appears larger than 1 MB.
    with patch("os.path.getsize", return_value=2 * ONE_MB):
        response = client.post("/ingest", json={"collection": "docs", "path": str(oversized)})

    assert response.status_code == 413
    detail = response.json()["detail"]
    assert "2 MB" in detail["message"]
    assert "1 MB" in detail["message"]
    assert "[ingest].max_file_mb" in detail["message"]


def test_single_file_413_body_carries_file_too_large_code(tmp_path: Path) -> None:
    """POST /ingest oversized single file → 413 body exposes the machine-readable code.

    The rest of the API surfaces structured errors as
    ``{"detail": {"code": ..., "message": ...}}`` and the user manual
    (Documentation/UserManual/50_ingestion_and_collections.md) promises the 413
    body carries ``code="file_too_large"``. This test pins that contract.
    """
    oversized = tmp_path / "big.pdf"
    oversized.write_bytes(b"x")  # real file so is_file() passes

    client, _ = _make_client(tmp_path, max_file_mb=1)

    with patch("os.path.getsize", return_value=2 * ONE_MB):
        response = client.post("/ingest", json={"collection": "docs", "path": str(oversized)})

    assert response.status_code == 413
    detail = response.json()["detail"]
    assert isinstance(detail, dict), f"413 detail must be a structured object, got {detail!r}"
    assert detail["code"] == "file_too_large"


def test_ingest_route_413_no_job_in_store(tmp_path: Path) -> None:
    """POST /ingest oversized single file → 413 AND job store contains zero jobs.

    Verifies the pre-check precedes job_store.create().
    """
    oversized = tmp_path / "big.pdf"
    oversized.write_bytes(b"x")

    client, job_store = _make_client(tmp_path, max_file_mb=1)

    with patch("os.path.getsize", return_value=2 * ONE_MB):
        response = client.post("/ingest", json={"collection": "docs", "path": str(oversized)})

    assert response.status_code == 413
    assert len(job_store.list()) == 0


def test_ingest_route_202_single_file_under_limit(tmp_path: Path) -> None:
    """Single-file path under the limit → 202."""
    small = tmp_path / "small.pdf"
    small.write_bytes(b"x")

    client, job_store = _make_client(tmp_path, max_file_mb=10)

    with patch("os.path.getsize", return_value=5 * ONE_MB):
        response = client.post("/ingest", json={"collection": "docs", "path": str(small)})

    assert response.status_code == 202


def test_ingest_route_202_max_file_mb_zero(tmp_path: Path) -> None:
    """max_file_mb=0 → no size check → 202 for any size."""
    big = tmp_path / "huge.pdf"
    big.write_bytes(b"x")

    client, job_store = _make_client(tmp_path, max_file_mb=0)

    with patch("os.path.getsize", return_value=999 * ONE_MB):
        response = client.post("/ingest", json={"collection": "docs", "path": str(big)})

    assert response.status_code == 202


def test_ingest_route_202_directory_path_no_413(tmp_path: Path) -> None:
    """Directory path containing an oversized file → 202 (no sync 413 at route level).

    Oversized files surface as per-file error IngestResult inside the job, not as 413.
    """
    subdir = tmp_path / "docs"
    subdir.mkdir()

    client, job_store = _make_client(tmp_path, max_file_mb=1)

    # Even if files inside would be oversized, the route pre-check is skipped for dirs.
    with patch("os.path.getsize", return_value=2 * ONE_MB) as mock_getsize:
        response = client.post("/ingest", json={"collection": "docs", "path": str(subdir)})

    assert response.status_code == 202
    mock_getsize.assert_not_called()


def test_ingest_route_202_documents_payload_no_413(tmp_path: Path) -> None:
    """body.documents payload (no path) → 202 (no size check)."""
    client, job_store = _make_client(tmp_path, max_file_mb=1)

    response = client.post(
        "/ingest",
        json={
            "collection": "docs",
            "documents": [{"text": "hello", "source_path": "/fake/path"}],
        },
    )

    assert response.status_code == 202


def test_ingest_route_413_symlink_follows_target_size(tmp_path: Path) -> None:
    """Symlink to an oversized file → 413 (route pre-check follows symlink via os.path.getsize)."""
    target = tmp_path / "big.pdf"
    target.write_bytes(b"x")
    symlink = tmp_path / "link.pdf"
    symlink.symlink_to(target)

    client, job_store = _make_client(tmp_path, max_file_mb=1)

    with patch("os.path.getsize", return_value=2 * ONE_MB):
        response = client.post("/ingest", json={"collection": "docs", "path": str(symlink)})

    assert response.status_code == 413


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ingest_e2e_413_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TestClient + make_real_app(toml_content='[ingest]\\nmax_file_mb=1') + temp file > 1 MB → 413."""
    from tests.integration.conftest import make_real_app

    toml_content = "[ingest]\nmax_file_mb = 1\n"
    oversized = tmp_path / "oversize.txt"
    oversized.write_bytes(b"x")

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        assert cfg.ingest.max_file_mb == 1

        with patch("os.path.getsize", return_value=2 * ONE_MB):
            response = client.post(
                "/ingest",
                json={"collection": "docs", "path": str(oversized)},
                headers=headers,
            )

        assert response.status_code == 413
        detail = response.json()["detail"]
        assert "2 MB" in detail["message"]
        assert "1 MB" in detail["message"]


@pytest.mark.integration
def test_size_check_boundary_consistent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """File exactly at max_file_mb bytes: route returns 202 (not 413).

    Verifies the route uses strictly-greater-than (same as pipeline guard).
    """
    from tests.integration.conftest import make_real_app

    limit_mb = 1
    exact_bytes = limit_mb * ONE_MB
    toml_content = f"[ingest]\nmax_file_mb = {limit_mb}\n"

    at_limit = tmp_path / "exact.txt"
    at_limit.write_bytes(b"x")

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        with patch("os.path.getsize", return_value=exact_bytes):
            response = client.post(
                "/ingest",
                json={"collection": "docs", "path": str(at_limit)},
                headers=headers,
            )

        assert response.status_code == 202, (
            f"A file exactly at the limit must be accepted (strictly greater-than); "
            f"got {response.status_code}"
        )

    # Also verify the pipeline's own size guard uses strictly-greater-than.
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    async def _make_pipeline_with_limit():
        class _MockEmbedderBackend:
            model_name: str = "mock-embedder"
            is_warm: bool = False

            def encode(self, texts: list[str]) -> list[list[float]]:
                return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        class _MockRerankerBackend:
            is_warm: bool = False

            def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
                return [0.5] * len(pairs)

        store = SearchStore(str(tmp_path / "pipe" / "db"))
        await store.connect()
        pipeline = SearchPipeline(
            store=store,
            embedder=Embedder(_MockEmbedderBackend()),
            reranker=Reranker(_MockRerankerBackend()),
            chunker=DocumentChunker(chunk_size=128),
            parser=DocumentParser(),
            top_k_retrieve=10,
            top_k_return=5,
            max_file_mb=limit_mb,
        )
        return store, pipeline

    store, pipeline = asyncio.run(_make_pipeline_with_limit())

    with patch("os.path.getsize", return_value=exact_bytes):
        result = asyncio.run(
            pipeline.ingest_file(at_limit, "docs", embedder=pipeline._global_embedder)
        )

    assert result.status == "ok", (
        f"Pipeline must accept a file exactly at the limit (strictly greater-than); "
        f"got status={result.status!r}, error={result.error!r}"
    )
