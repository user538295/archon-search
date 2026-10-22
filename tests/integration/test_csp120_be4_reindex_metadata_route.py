"""CSP120 BE-4 — POST /collections/{name}/reindex-metadata integration test.

Verifies that the endpoint returns 202 + RUNNING, enqueues a MetadataReindexJob,
runs SearchStore.reindex_metadata() as a background task, and the completed job
result carries all 5 ReindexResult fields (processed, updated, skipped,
ts_normalized, warnings).

Run with:
    uv run pytest tests/integration/test_csp120_be4_reindex_metadata_route.py -v --no-cov
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests._job_store_faults import JOB_WRITE_FAILURES

pytestmark = pytest.mark.integration


def _poll_job_until_terminal(
    client: TestClient, job_id: str, api_key: str, timeout: float = 10.0
) -> dict:
    """Poll GET /jobs/{id} until a terminal status is reached."""
    deadline = time.monotonic() + timeout
    terminal = {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers={"Authorization": f"Bearer {api_key}"})
        assert r.status_code == 200, f"GET /jobs/{job_id} returned {r.status_code}: {r.text}"
        if r.json()["status"] in terminal:
            return r.json()
        time.sleep(0.05)
    pytest.fail(f"Job {job_id} did not reach a terminal status within {timeout}s")


@pytest.mark.integration
def test_reindex_metadata_job_result_contains_all_fields(tmp_path: Path, monkeypatch) -> None:
    """Real SearchStore via make_real_app; job DONE result has all 5 ReindexResult fields.

    S5 server side; S19 active guard end-to-end.
    """
    from tests.integration.conftest import ingest_file_via_path, make_real_app

    # Seed a real text file so the collection exists in the store.
    doc_path = tmp_path / "sample.txt"
    doc_path.write_text("Hello world. This is a test document for reindex-metadata.", encoding="utf-8")

    collection_dir = tmp_path / "collection_dir"
    collection_dir.mkdir()
    (collection_dir / "sample.txt").write_text(
        "Hello world. This is a test document for reindex-metadata.", encoding="utf-8"
    )

    toml_content = f"[collections]\ncollections=[{str(collection_dir)!r}]\n"

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        from archon_search.sync import path_to_collection_name

        headers = {"Authorization": f"Bearer {api_key}"}
        col = path_to_collection_name(str(collection_dir))

        # Ingest a real file so the table exists in the store.
        ingest_file_via_path(client, col, str(collection_dir / "sample.txt"), api_key=api_key)

        # POST to the reindex-metadata endpoint.
        resp = client.post(
            f"/collections/{col}/reindex-metadata",
            json={},
            headers=headers,
        )
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["status"] == "RUNNING"
        assert "job_id" in body
        job_id = body["job_id"]

        # Poll until DONE.
        job = _poll_job_until_terminal(client, job_id, api_key)
        assert job["status"] == "DONE", f"Expected DONE, got {job['status']}: {job.get('error')}"
        assert job.get("kind") == "metadata_reindex", f"Expected kind='metadata_reindex', got {job.get('kind')!r}"

        result = job.get("result")
        assert result is not None, "Expected job result to be present on DONE"

        for field in ("processed", "updated", "skipped", "ts_normalized", "warnings"):
            assert field in result, f"Missing ReindexResult field: {field}"

        assert isinstance(result["processed"], int)
        assert isinstance(result["updated"], int)
        assert isinstance(result["skipped"], int)
        assert isinstance(result["ts_normalized"], int)
        assert isinstance(result["warnings"], list)

        # T4 (S19): metadata_reindex_job_id must be cleared after DONE, so a
        # subsequent POST returns 202, not 409.
        resp2 = client.post(
            f"/collections/{col}/reindex-metadata",
            json={},
            headers=headers,
        )
        assert resp2.status_code == 202, (
            f"Re-submit after DONE returned {resp2.status_code}: {resp2.text}"
        )


@contextmanager
def _reindex_metadata_app(tmp_path: Path, monkeypatch) -> Iterator[tuple[TestClient, str, dict[str, str]]]:
    """A real app with one ingested collection; yields ``(client, collection, headers)``."""
    from archon_search.sync import path_to_collection_name
    from tests.integration.conftest import ingest_file_via_path, make_real_app

    collection_dir = tmp_path / "collection_dir"
    collection_dir.mkdir()
    (collection_dir / "sample.txt").write_text("Hello world. A test document.", encoding="utf-8")
    toml_content = f"[collections]\ncollections=[{str(collection_dir)!r}]\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, _cfg, api_key):
        col = path_to_collection_name(str(collection_dir))
        ingest_file_via_path(client, col, str(collection_dir / "sample.txt"), api_key=api_key)
        yield client, col, {"Authorization": f"Bearer {api_key}"}


@pytest.mark.integration
def test_reindex_metadata_persists_job_only_as_running(tmp_path: Path, monkeypatch) -> None:
    """The route runs the reindex itself, so its job's first persisted status is RUNNING — never QUEUED."""
    from tests._job_store_faults import recording_job_writes

    with _reindex_metadata_app(tmp_path, monkeypatch) as (client, col, headers):
        with recording_job_writes() as writes:
            resp = client.post(f"/collections/{col}/reindex-metadata", json={}, headers=headers)

        assert resp.status_code == 202, resp.text
        statuses = writes.statuses_of(resp.json()["job_id"])
        assert statuses, "the metadata-reindex job was never persisted"
        assert statuses[0] == "RUNNING", f"metadata-reindex job persisted as {statuses}"
        assert "QUEUED" not in statuses




@pytest.mark.integration
@pytest.mark.parametrize("write_error", JOB_WRITE_FAILURES)
def test_reindex_metadata_job_write_failure_returns_sanitized_500_and_never_runs(
    tmp_path: Path, monkeypatch, write_error: Exception
) -> None:
    """A failed job persist answers the sanitized 500 envelope (never the raw exception), leaves no
    metadata-reindex job and no metadata_reindex_job_id on the collection, and never starts the reindex (the
    route builds the task coroutine synchronously before returning, so a spy that was not called proves no
    task exists)."""
    from unittest.mock import patch

    import archon_search.server.routes_collections as routes_collections_mod
    from archon_search.types import MetadataReindexJob
    from tests._job_store_faults import failing_job_writes

    with _reindex_metadata_app(tmp_path, monkeypatch) as (client, col, headers):
        job_store = client.app.state.job_store

        with patch.object(routes_collections_mod, "_reindex_metadata_task") as reindex_spy, \
             failing_job_writes(exc=write_error):
            resp = client.post(f"/collections/{col}/reindex-metadata", json={}, headers=headers)

        assert resp.status_code == 500
        assert resp.json() == {"detail": "internal error"}
        assert [j for j in job_store.list() if isinstance(j, MetadataReindexJob)] == []
        reindex_spy.assert_not_called()
        meta = asyncio.run(client.app.state.search_store.get_collection_meta(col, namespace="default"))
        assert meta is not None
        assert meta.metadata_reindex_job_id is None


@pytest.mark.integration
@pytest.mark.parametrize(("dry_run", "expected_status"), [(True, 200), (False, 503)])
def test_delete_collection_refuses_an_active_metadata_reindex_only_when_it_writes(
    tmp_path: Path, monkeypatch, dry_run: bool, expected_status: int
) -> None:
    """Real app and store: a RUNNING non-dry-run metadata reindex makes DELETE answer the store-busy 503 and keeps the
    collection; a RUNNING dry run (read-only) does not — the collection and its meta row are dropped."""
    with _reindex_metadata_app(tmp_path, monkeypatch) as (client, col, headers):
        job = client.app.state.job_store.create_metadata_reindex(collection=col, dry_run=dry_run)

        response = client.delete(f"/collections/{col}", headers=headers)

        assert response.status_code == expected_status, response.text
        if expected_status == 503:
            assert job.job_id in response.json()["detail"]
        meta = asyncio.run(client.app.state.search_store.get_collection_meta(col, namespace="default"))
        assert (meta is None) is (expected_status == 200)
