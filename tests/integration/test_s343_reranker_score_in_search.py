"""S343: POST /search results carry a non-null reranker_score when a reranker ran."""
from __future__ import annotations

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s343")]


def test_search_results_carry_reranker_score(tmp_path, monkeypatch):
    """Ingest a doc, search with the default reranker active, assert reranker_score is not null."""
    doc = tmp_path / "test_doc.md"
    doc.write_text("Archon search is a hybrid retrieval server with vector and full-text search.")

    with make_real_app(tmp_path / "app", monkeypatch) as (client, cfg, api_key):
        col = "s343col"
        headers = {"Authorization": f"Bearer {api_key}"}

        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "hybrid retrieval"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results, "Expected at least one search result"

        for r in results:
            assert "reranker_score" in r, (
                f"reranker_score field missing from search result: {list(r.keys())}"
            )
            assert r["reranker_score"] is not None, (
                f"reranker_score is null but a reranker was active: {r}"
            )
