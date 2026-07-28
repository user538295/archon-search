"""S22: a collection created by a single-file ingest must be reindexable.

Reproduces the backlog bug ``202607280906-S22-reindex_exits_zero``:
ingesting one file into a fresh collection via ``POST /ingest`` writes the
LanceDB table and a meta row, but ``POST /collections/{name}/reindex`` returned
404 — because the handler gated on config-declared collection paths, which a
single-file ingest never adds. Same class of bug as S09 and S12.
"""
from __future__ import annotations

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app


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
