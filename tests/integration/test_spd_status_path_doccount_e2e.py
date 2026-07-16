"""SPD e2e: GET /status returns the real per-collection path and cached doc_count.

Scenarios covered (team plan 2026-07-15-100-status-path-doccount-team-plan.md):
- S1: a collection with N ingested documents, configured with a path whose basename
  matches the collection name, shows the absolute storage path and doc_count = N.
- S4: with two configured collections, each entry carries its own real path and count.

Run with:
    uv run pytest tests/integration/test_spd_status_path_doccount_e2e.py --no-cov
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def test_status_shows_real_path_and_doc_count(tmp_path: Path, monkeypatch) -> None:
    """S1: after ingesting one document into a configured collection, /status reports its
    absolute path and doc_count = 1 (the cached meta value, not a placeholder)."""
    docs_dir = tmp_path / "mydocs"
    docs_dir.mkdir()
    doc_file = docs_dir / "a.txt"
    doc_file.write_text("archon search hybrid retrieval and routing server", encoding="utf-8")

    toml_content = f'[collections]\ncollections = ["{docs_dir}"]\n'

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, config, api_key):
        ingest_file_via_path(client, "mydocs", str(doc_file), api_key=api_key)

        resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        data = resp.json()

        entry = next((c for c in data["collections"] if c["name"] == "mydocs"), None)
        assert entry is not None, f"mydocs missing from /status: {data['collections']}"
        assert entry["path"] == str(docs_dir.resolve())
        assert entry["doc_count"] == 1


def test_status_two_collections_each_get_own_path_and_count(tmp_path: Path, monkeypatch) -> None:
    """S4-style: two configured collections each report their own real path and cached count."""
    alpha_dir = tmp_path / "alpha"
    beta_dir = tmp_path / "beta"
    alpha_dir.mkdir()
    beta_dir.mkdir()
    alpha_file = alpha_dir / "doc.txt"
    beta_file = beta_dir / "doc.txt"
    alpha_file.write_text("alpha collection content about vectors", encoding="utf-8")
    beta_file.write_text("beta collection content about reranking", encoding="utf-8")

    toml_content = (
        f'[collections]\ncollections = ["{alpha_dir}", "{beta_dir}"]\n'
    )

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, config, api_key):
        ingest_file_via_path(client, "alpha", str(alpha_file), api_key=api_key)
        ingest_file_via_path(client, "beta", str(beta_file), api_key=api_key)

        data = client.get("/status", headers={"Authorization": f"Bearer {api_key}"}).json()
        by_name = {c["name"]: c for c in data["collections"]}

        assert by_name["alpha"]["path"] == str(alpha_dir.resolve())
        assert by_name["alpha"]["doc_count"] == 1
        assert by_name["beta"]["path"] == str(beta_dir.resolve())
        assert by_name["beta"]["doc_count"] == 1
