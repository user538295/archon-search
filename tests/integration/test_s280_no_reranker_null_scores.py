"""S280 — a server configured with ``reranker_model = ""`` must return
``reranker_score = null`` on every result, without ``rerank=false`` in the request.
"""
from __future__ import annotations

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_NO_RERANKER_TOML = """
[database]
reranker_model = ""
"""


def test_search_reranker_score_null_when_reranker_disabled(tmp_path, monkeypatch) -> None:
    doc = tmp_path / "fox.md"
    doc.write_text("The quick brown fox jumps over the lazy dog.\n", encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, toml_content=_NO_RERANKER_TOML) as (client, cfg, api_key):
        assert cfg.reranker_model == ""
        ingest_file_via_path(client, "no_reranker_test", str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": "no_reranker_test", "query": "fox"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results, "expected a non-empty result set"
        for i, r in enumerate(results):
            assert r.get("reranker_score") is None, (
                f"Expected null reranker_score; got {r.get('reranker_score')} on result[{i}]"
            )
