"""S284: POST /ingest with a nonexistent path leaves the collection registered with docs=0.

After the background ingest job transitions to FAILED, GET /collections/ must list the
collection with doc_count=0 — matching the behaviour described in
UserManual/50_ingestion_and_collections.md:113.
"""
from __future__ import annotations

import time

import pytest

from tests.integration.conftest import make_real_app


@pytest.mark.integration
def test_failed_ingest_nonexistent_path_collection_remains_registered(
    tmp_path, monkeypatch
) -> None:
    """S284: after a FAILED ingest (nonexistent path), the collection is listed with docs=0."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        # Step 1: POST /ingest with a nonexistent path → 202
        resp = client.post(
            "/ingest",
            json={"collection": "s284_nonexistent_path_test", "path": "/nonexistent/archon/s284/path"},
            headers=headers,
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]

        # Step 2: poll until the job reaches a terminal status (expect FAILED)
        deadline = time.monotonic() + 10.0
        job_status = None
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=headers)
            assert r.status_code == 200
            job_status = r.json()["status"]
            if job_status in ("DONE", "FAILED", "CANCELLED", "FAILED_EXPIRED"):
                break
            time.sleep(0.05)

        assert job_status == "FAILED", f"expected FAILED, got {job_status}"

        # Step 3: the collection must appear in GET /collections/ with doc_count=0
        resp = client.get("/collections/", headers=headers)
        assert resp.status_code == 200, resp.text
        collections = resp.json()
        names = {c["name"] for c in collections}
        assert "s284_nonexistent_path_test" in names, (
            f"S284: collection 's284_nonexistent_path_test' is absent from GET /collections/ "
            f"after a FAILED ingest; the collection must remain registered with docs=0. "
            f"full list={collections}"
        )
        col = next(c for c in collections if c["name"] == "s284_nonexistent_path_test")
        assert col["doc_count"] == 0, (
            f"S284: expected doc_count=0 after failed ingest, got {col['doc_count']}"
        )
