"""S22: reindex must not remove the collection.

Third assertion of the S22 reindex scenario (see backlog
``202607280906-S22-collection_still_in_list``): after
``archon-search collection reindex <name> --wait`` the collection MUST still
appear in ``archon-search collection list``. Reindex re-embeds the corpus; it
must never drop the collection's table or ``CollectionMeta`` row.

Pins the invariant end-to-end: real app + real single-file ingest, drive the
reindex job to a terminal state, then assert the collection is STILL listed.
The reindex is run against an empty working directory so the meta-only
collection's ``Path("")`` glob root resolves to a directory with no files —
the job completes as a no-op re-embed (which is the path that must preserve
the collection), rather than re-globbing the pytest CWD.
"""
from __future__ import annotations

import time

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app


def _collection_names(client, api_key: str) -> list[str]:
    resp = client.get("/collections/", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200, resp.text
    return [c["name"] for c in resp.json()]


@pytest.mark.integration
def test_reindex_does_not_remove_collection(tmp_path, monkeypatch) -> None:
    doc = tmp_path / "single.md"
    doc.write_text("# Single\nThis is a standalone document about semantic search.\n")

    # Meta-only collections resolve their reindex glob root to Path("") == CWD;
    # point CWD at an empty dir so the reindex is a clean no-op re-embed.
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        ingest_file_via_path(client, "single-docs", str(doc), api_key=api_key)

        # Guard against vacuity: the collection is listed BEFORE reindex.
        assert "single-docs" in _collection_names(client, api_key)

        resp = client.post("/collections/single-docs/reindex", headers=headers)
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]

        # Drive the reindex job to a terminal state (TestClient background tasks run).
        deadline = time.monotonic() + 10.0
        status = None
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=headers)
            assert r.status_code == 200
            status = r.json()["status"]
            if status in {"DONE", "FAILED", "CANCELLED"}:
                break
            time.sleep(0.05)
        assert status == "DONE", f"reindex did not complete: {status}"

        # The invariant: reindex did NOT remove the collection — still listed.
        assert "single-docs" in _collection_names(client, api_key)
