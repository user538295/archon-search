"""Shared helpers for integration and e2e tests.

New file — do NOT modify the existing ``tests/conftest.py``.

``make_real_app`` is a context manager that yields ``(TestClient, config,
api_key)`` backed by real ``SearchStore`` + ``SearchPipeline`` in
``tmp_path``.  All env vars are set via ``monkeypatch`` so they auto-revert
after each test.  Usage::

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        ...

``ingest_doc`` POSTs to /ingest and polls GET /jobs/{id} until done.

``search`` POSTs to /search and returns the result items.

``make_real_pipeline`` creates an async store+pipeline pair for tests that
call async pipeline/store methods directly without going through TestClient.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient


@contextmanager
def make_real_app(
    tmp_path,
    monkeypatch,
    *,
    backup_enabled: bool = False,
    maintenance_enabled: bool = False,
    mcp_enabled: bool = False,
    namespaces: dict[str, str] | None = None,
) -> Iterator[tuple[TestClient, Any, str]]:
    """Context manager yielding ``(TestClient, config, api_key)`` backed by real store+pipeline.

    Uses ``monkeypatch.setenv`` for env vars so they auto-revert after each test.
    Pass ``backup_enabled=True`` only in Task 2.2 backup tests.
    Pass ``maintenance_enabled=True`` to enable the MaintenanceLoop with interval_hours=1.
    Pass ``namespaces={'key_hex': 'ns-name', ...}`` for multi-namespace tests.
    The TestClient lifespan (startup + shutdown) is managed by the context block.
    """
    import secrets

    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.backup.interval_hours = 0  # disabled by default; trigger loop self-exits immediately
    # MCP is mounted only when explicitly requested; default off keeps unrelated
    # integration tests fast and avoids the FastMCP session-manager startup cost.
    cfg.mcp.enabled = mcp_enabled

    if backup_enabled:
        cfg.backup.interval_hours = 1
        cfg.backup.output_dir = str(tmp_path / "backups")

    if maintenance_enabled:
        cfg.maintenance.interval_hours = 1  # enabled; manual trigger still fires immediately

    if namespaces is not None:
        cfg.namespaces = namespaces

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,  # replaced in lifespan startup
    )

    app = create_app(cfg, job_store, scheduler=scheduler)
    with TestClient(app) as client:
        yield client, cfg, api_key


def ingest_doc(
    client: TestClient,
    col: str,
    text: str,
    path: str,
    *,
    api_key: str,
    timeout_s: float = 10.0,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """POST /ingest with inline documents, poll until done. Returns job_id.

    NOTE: ``SearchPipeline`` does not implement ``ingest_documents``. The route
    handler logs a warning and marks the job DONE without writing any data.
    Use ``ingest_file_via_path`` instead when you need real data in the store.
    This helper is retained for tests that exercise the /ingest path validation
    (auth, path-safety) rather than actual data ingestion.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)
    body = {
        "collection": col,
        "documents": [{"text": text, "source_path": path}],
    }
    resp = client.post("/ingest", json=body, headers=headers)
    assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
    job_id = resp.json()["job_id"]

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=headers)
        assert r.status_code == 200
        status = r.json()["status"]
        if status == "DONE":
            return job_id
        if status == "FAILED":
            pytest.fail(f"ingest job failed (job_id={job_id}): {r.json()}")
        time.sleep(0.1)
    pytest.fail(f"ingest did not complete in {timeout_s}s (job_id={job_id})")


def ingest_file_via_path(
    client: TestClient,
    col: str,
    file_path: str,
    *,
    api_key: str,
    timeout_s: float = 10.0,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """POST /ingest with a file path, poll until done. Returns job_id."""
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)
    body = {"collection": col, "path": file_path}
    resp = client.post("/ingest", json=body, headers=headers)
    assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
    job_id = resp.json()["job_id"]

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=headers)
        assert r.status_code == 200
        status = r.json()["status"]
        if status == "DONE":
            return job_id
        if status == "FAILED":
            pytest.fail(f"ingest job failed (job_id={job_id}): {r.json()}")
        time.sleep(0.1)
    pytest.fail(f"ingest did not complete in {timeout_s}s (job_id={job_id})")


def search(
    client: TestClient,
    col: str,
    query: str,
    *,
    api_key: str,
    **filters: Any,
) -> list[dict]:
    """POST /search, assert 200, return items."""
    headers = {"Authorization": f"Bearer {api_key}"}
    body: dict[str, Any] = {"collection": col, "query": query}
    if filters:
        body["filters"] = filters
    resp = client.post("/search", json=body, headers=headers)
    assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
    return resp.json()["results"]


async def make_real_pipeline(tmp_path, monkeypatch):
    """Create a real SearchStore + SearchPipeline for async pipeline tests.

    Callers must be ``async def`` and call ``await make_real_pipeline(...)``.
    Returns ``(store, pipeline)``.

    Uses stub embedder (same stubs as the test suite's ``install_stubs()``).
    Calls ``await store.connect()`` before returning — missing this is the
    most likely failure since ``SearchStore`` raises ``RuntimeError("not
    connected")`` without it.
    """
    from unittest.mock import MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    store = SearchStore(str(tmp_path / "db"))
    await store.connect()

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    return store, pipeline
