"""S12: a collection created by a single-file ingest must be removable.

Reproduces the backlog bug ``202607280906-S12-remove_exits_zero``:
ingesting one file into a fresh collection via ``POST /ingest`` writes the
LanceDB table and a meta row, but ``DELETE /collections/{name}`` returned 404 —
because the handler gated on config-declared collection paths, which a
single-file ingest never adds. Same class of bug as S09.
"""
from __future__ import annotations

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app


@pytest.mark.integration
def test_single_file_ingest_collection_is_removable(tmp_path, monkeypatch) -> None:
    doc = tmp_path / "single.md"
    doc.write_text("# Single\nThis is a standalone document about semantic search.\n")

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        ingest_file_via_path(client, "single-docs", str(doc), api_key=api_key)

        # Step 1: remove exits 0 (200), not 404.
        resp = client.delete("/collections/single-docs", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] is True

        # Step 2: list no longer shows single-docs.
        resp = client.get("/collections/", headers=headers)
        assert resp.status_code == 200, resp.text
        names = {c["name"] for c in resp.json()}
        assert "single-docs" not in names, f"single-docs still listed: {names}"
