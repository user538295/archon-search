"""S22: a collection created by a single-file ingest must be reindexable.

Reproduces the backlog bug ``202607280906-S22-reindex_exits_zero``:
ingesting one file into a fresh collection via ``POST /ingest`` writes the
LanceDB table and a meta row, but ``POST /collections/{name}/reindex`` returned
404 — because the handler gated on config-declared collection paths, which a
single-file ingest never adds. Same class of bug as S09 and S12.
"""
from __future__ import annotations

import time

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

_TERMINAL_STATUSES = {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}


@pytest.mark.integration
def test_single_file_ingest_collection_is_reindexable(tmp_path, monkeypatch) -> None:
    doc = tmp_path / "single.md"
    doc.write_text("# Single\nThis is a standalone document about semantic search.\n")

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        ingest_file_via_path(client, "single-docs", str(doc), api_key=api_key)

        # reindex accepts the meta-only collection (202), not 404.
        resp = client.post("/collections/single-docs/reindex", headers=headers)
        assert resp.status_code == 202, resp.text
        assert "job_id" in resp.json()

        # A truly-absent collection still 404s.
        resp = client.post("/collections/does-not-exist/reindex", headers=headers)
        assert resp.status_code == 404, resp.text


@pytest.mark.integration
def test_single_file_ingest_reindex_job_completes(tmp_path, monkeypatch) -> None:
    """The reindex job for a meta-only collection reaches DONE.

    A single-file ingest writes a meta row but no configured source directory, so
    ``collection_path`` is empty. The reindex must NOT scan the server CWD (which
    never terminates sensibly) — it is a no-op that promotes to DONE, so
    ``collection reindex --wait`` exits 0 with a completion message and the
    collection is preserved (``S22`` — reindex_exits_zero).
    """
    doc = tmp_path / "single.md"
    doc.write_text("# Single\nStandalone document about semantic search.\n")
    other = tmp_path / "other.md"
    other.write_text("# Other\nA second standalone document about vector databases.\n")

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        ingest_file_via_path(client, "single-docs", str(doc), api_key=api_key)
        # A bystander meta-only collection — the brief observed only the
        # NON-reindexed collection surviving, so pin the reindexed one AND the
        # bystander against the reindex.
        ingest_file_via_path(client, "other-docs", str(other), api_key=api_key)

        def _chunk_count() -> int:
            info = client.get("/collections/single-docs", headers=headers).json()
            return info["chunk_count"]

        before = _chunk_count()

        resp = client.post("/collections/single-docs/reindex", headers=headers)
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]

        status = None
        for _ in range(200):
            job = client.get(f"/jobs/{job_id}", headers=headers).json()
            status = job["status"]
            if status in _TERMINAL_STATUSES:
                break
            time.sleep(0.05)
        assert status == "DONE", f"reindex job ended {status!r}, expected DONE"

        # No CWD scan leaked stray documents in — chunk count is unchanged. This
        # pins the actual fix (scan skipped) rather than inferring it from timing.
        assert _chunk_count() == before

        # Reindex preserves the reindexed collection AND the bystander — both
        # are still listed afterwards (reindexing one drops neither).
        names = {c["name"] for c in client.get("/collections/", headers=headers).json()}
        assert "single-docs" in names
        assert "other-docs" in names
