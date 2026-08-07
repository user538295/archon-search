"""S571 — GET /collections/ must report the same doc_count as GET /collections/{name}.

The list endpoint was hardcoding doc_count=0.  After the fix both endpoints
call ``count_documents()`` and agree.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def test_list_doc_count_equals_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed = tmp_path / "docs"
    seed.mkdir()
    (seed / "a.md").write_text("# Alpha\nFirst document.\n")
    (seed / "b.md").write_text("# Beta\nSecond document.\n")

    toml = f'[collections]\ncollections = ["{seed}"]\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, api_key):
        col = seed.name
        ingest_file_via_path(client, col, str(seed), api_key=api_key)

        headers = {"Authorization": f"Bearer {api_key}"}

        detail_resp = client.get(f"/collections/{col}", headers=headers)
        assert detail_resp.status_code == 200
        detail_doc_count = detail_resp.json()["doc_count"]
        assert detail_doc_count >= 1, f"detail reports {detail_doc_count} docs after ingest"

        list_resp = client.get("/collections/", headers=headers)
        assert list_resp.status_code == 200
        entries = {e["name"]: e for e in list_resp.json()}
        assert col in entries, f"{col} missing from list: {list(entries)}"
        list_doc_count = entries[col]["doc_count"]

        assert list_doc_count == detail_doc_count, (
            f"list doc_count={list_doc_count} != detail doc_count={detail_doc_count}"
        )
