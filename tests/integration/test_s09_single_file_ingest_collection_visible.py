"""S09: a single-file ingest must make the collection visible to list + info.

Reproduces the backlog bug ``202607280906-S09-collection_info_exits_zero``:
ingesting one file into a fresh collection via ``POST /ingest`` wrote the
LanceDB table and a meta row, but ``GET /collections/`` omitted it and
``GET /collections/{name}`` returned 404 — because both handlers iterated only
over config-declared collection paths, which a single-file ingest never adds.
"""
from __future__ import annotations

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app


@pytest.mark.integration
def test_single_file_ingest_is_visible_in_list_and_info(tmp_path, monkeypatch) -> None:
    doc = tmp_path / "single.md"
    doc.write_text("# Single\nThis is a standalone document about semantic search.\n")

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        ingest_file_via_path(client, "single-docs", str(doc), api_key=api_key)

        # Step 2: list shows single-docs
        resp = client.get("/collections/", headers=headers)
        assert resp.status_code == 200, resp.text
        names = {c["name"] for c in resp.json()}
        assert "single-docs" in names, f"single-docs missing from list: {names}"

        # Step 3: info exits 0 with metadata
        resp = client.get("/collections/single-docs", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "single-docs"
