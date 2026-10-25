"""A directory over ``[ingest].max_discovered_files`` fails its REST job and its MCP call with the curated text (SecReview #7d)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import IngestTooManyFilesError
from archon_search.config import SearchConfig
from archon_search.jobs.model import JobStatus
from archon_search.jobs.store import JobStore
from archon_search.server import routes_jobs
from archon_search.server.routes_jobs import IngestRequest, _default_ingest_task
from tests._fastmcp_stub import stub_mcp_tool

_LIMIT = 7


@pytest.mark.asyncio
async def test_rest_job_fails_with_the_curated_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_jobs, "_dispatch_ingest", AsyncMock(side_effect=IngestTooManyFilesError(_LIMIT)))
    job_store = JobStore(path=tmp_path / "jobs.json")
    body = IngestRequest(collection="docs", path=str(tmp_path))
    job = job_store.create(namespace="default", collection="docs", path=str(tmp_path))

    await _default_ingest_task(
        job.job_id, job_store, body,
        search_store=MagicMock(), embedder_cache=MagicMock(), pipeline=MagicMock(), config=SearchConfig(),
    )

    finished = job_store.get(job.job_id)
    assert finished is not None
    assert finished.status is JobStatus.FAILED
    assert finished.error == str(IngestTooManyFilesError(_LIMIT))
    assert "max_discovered_files" in (finished.error or "")


@pytest.mark.asyncio
async def test_mcp_ingest_directory_reports_the_limit(tmp_path: Path) -> None:
    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(side_effect=IngestTooManyFilesError(_LIMIT))
    pipeline.store.collection_writer = MagicMock()
    tool = stub_mcp_tool("ingest_directory", pipeline, "col", config=SearchConfig())

    result = await tool(path=str(tmp_path))

    assert result["code"] == "validation_error"
    assert result["error"] == str(IngestTooManyFilesError(_LIMIT))
