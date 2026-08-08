"""S489 -- ``top_k_retrieve`` must bound the explain candidate pool.

``POST /explain`` returns ``top_results + near_misses`` carved from the
first-stage retrieval pool.  ``top_k_retrieve`` caps that pool, so
``len(results) + len(near_misses) <= top_k_retrieve`` must hold regardless
of how many documents match.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration]

TOP_K_RETRIEVE = 5


def test_explain_pool_bounded_by_top_k_retrieve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingest 20 single-chunk docs, POST /explain -- total candidates <= top_k_retrieve."""
    toml = f"""
[database]
top_k_retrieve = {TOP_K_RETRIEVE}
top_k_return = 3
"""
    col = "s489pool"

    # Create 20 docs that all match the query "quarterly report".
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    for i in range(20):
        (seed_dir / f"doc{i:02d}.md").write_text(
            f"# quarterly report {i}\nThis is the quarterly report number {i} with data.",
            encoding="utf-8",
        )

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        # Ingest all docs.
        for md_file in sorted(seed_dir.glob("*.md")):
            ingest_file_via_path(client, col, str(md_file), api_key=api_key)

        # POST /explain with rerank=true.
        resp = client.post(
            "/explain",
            json={"collection": col, "query": "quarterly report", "rerank": True},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        results = body["results"]
        near_misses = body["near_misses"]
        total = len(results) + len(near_misses)

        assert total <= TOP_K_RETRIEVE, (
            f"explain pool ({total} = {len(results)} results + {len(near_misses)} near_misses) "
            f"exceeds top_k_retrieve ({TOP_K_RETRIEVE})"
        )
