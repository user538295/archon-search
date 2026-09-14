"""S293: ``POST /ingest`` boundary validation for the inline ``documents`` mode.

The mode is the only ingest mode a network client has, so every field in it is
untrusted input. These tests pin the three boundary guards: item shape (422),
``source_path`` safety (400, the same rule ``body.path`` gets), and the
``[ingest].max_file_mb`` operator cap (413, per document).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app

TEST_KEY = os.environ.get("ARCHON_SEARCH_API_KEY", "0" * 64)
AUTH = {"Authorization": f"Bearer {TEST_KEY}"}

ONE_MB = 1024 * 1024


def _make_client(tmp_path: Path, max_file_mb: int = 0) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.ingest.max_file_mb = max_file_mb
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))
    return TestClient(app, headers=AUTH)


def test_malformed_document_item_is_rejected_with_422(tmp_path: Path) -> None:
    """A wrongly-typed item must fail at the boundary, not inside the job.

    An untyped ``list[dict]`` let ``{"text": 123}`` through to the pipeline, where
    the resulting exception was written verbatim into the wire-facing job ``error``
    field — the exact leak CLAUDE.md's no-``str(exc)`` invariant forbids.
    """
    client = _make_client(tmp_path)

    response = client.post(
        "/ingest",
        json={"collection": "docs", "documents": [{"text": 123, "source_path": "/a.md"}]},
    )

    assert response.status_code == 422


def test_document_item_missing_source_path_is_rejected_with_422(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    response = client.post(
        "/ingest", json={"collection": "docs", "documents": [{"text": "hello"}]}
    )

    assert response.status_code == 422


def test_relative_document_source_path_is_rejected_with_400(tmp_path: Path) -> None:
    """``documents[].source_path` gets the same path-safety rule as ``body.path``."""
    client = _make_client(tmp_path)

    response = client.post(
        "/ingest",
        json={
            "collection": "docs",
            "documents": [{"text": "hello", "source_path": "relative/doc.md"}],
        },
    )

    assert response.status_code == 400
    assert "not_absolute" in response.json()["detail"]


def test_traversing_document_source_path_is_rejected_with_400(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    response = client.post(
        "/ingest",
        json={
            "collection": "docs",
            "documents": [{"text": "hello", "source_path": "/corpus/../../etc/passwd"}],
        },
    )

    assert response.status_code == 400
    assert "contains_dotdot" in response.json()["detail"]


def test_oversized_inline_document_is_rejected_with_413(tmp_path: Path) -> None:
    """``max_file_mb`` is a per-document cap here — the same operator control the
    single-file path mode gets, applied before the job is created."""
    client = _make_client(tmp_path, max_file_mb=1)

    response = client.post(
        "/ingest",
        json={
            "collection": "docs",
            "documents": [{"text": "x" * (2 * ONE_MB), "source_path": "/corpus/big.md"}],
        },
    )

    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["code"] == "file_too_large"
    assert "[ingest].max_file_mb" in detail["message"]


def test_inline_document_within_the_limit_is_accepted(tmp_path: Path) -> None:
    client = _make_client(tmp_path, max_file_mb=1)

    response = client.post(
        "/ingest",
        json={
            "collection": "docs",
            "documents": [{"text": "x" * 1024, "source_path": "/corpus/small.md"}],
        },
    )

    assert response.status_code == 202
