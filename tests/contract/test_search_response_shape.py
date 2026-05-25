"""REST /search response shape contract.

Implements Task 4.3 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search._types import SearchResult
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.pipeline import SearchPipelineResult
from archon_search.server.app import create_app


def _make_client(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    with patch("archon_search.chunker.DocumentChunker.__init__", return_value=None):
        app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=[
                SearchResult(
                    doc_id="d" * 64,
                    chunk_id="d" * 64 + "-000000",
                    text="hello",
                    score=0.9,
                    source_path="/tmp/x.md",
                    file_type="md",
                    indexed_at="2026-05-21T10:00:00+00:00",
                    updated_at="2026-05-21T11:00:00+00:00",
                    ingested_by="cli",
                    metadata={"k": "v"},
                    acl=["team-a"],
                )
            ],
            acl_filtered=False,
        )
    )
    app.state.pipeline = pipeline
    return client


def test_rest_search_response_includes_new_keys(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/search", json={"collection": "col", "query": "hello"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["results"], "expected at least one result"
    item = data["results"][0]
    for key in ("file_type", "indexed_at", "updated_at", "ingested_by", "metadata", "acl"):
        assert key in item, f"missing key {key!r} in REST /search response"
    assert item["file_type"] == "md"
    assert item["ingested_by"] == "cli"
    # metadata is suppressed by default (include_metadata=False); the key must
    # still be present but with an empty dict.
    assert item["metadata"] == {}
    assert item["acl"] == ["team-a"]
