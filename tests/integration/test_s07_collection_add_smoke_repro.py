"""Evidence tests for smoke scenarios S07/S14 (collection add 409) and the
S15/S20/S22 cascade (search/reindex 404 for ``archon_test_docs``).

These are NOT a fix — they establish, at the code level, whether the smoke
failures reflect a product defect or an environmental (dirty-state) precondition.

Two facts are pinned:

1. Happy path (``test_fresh_collection_add_then_search_and_reindex_succeed``):
   on a clean data dir, ``POST /collections/`` on a fresh directory returns 202,
   the ingest job completes, ``GET /collections/`` lists it, ``POST /search``
   returns 200, and ``POST /collections/{name}/reindex`` returns 202. The product
   executes the full S07 -> S15 -> S20 -> S22 sequence correctly. No defect.

2. Divergence (``test_stale_config_path_without_store_meta_reproduces_smoke``):
   the exact smoke symptom (``collection add`` -> 409 "already registered" AND
   ``POST /search`` -> 404) is reproducible ONLY when the config path list and the
   store meta have diverged (path registered in config, but no meta row in the
   store). ``POST /collections/`` 409s on config membership
   (routes_collections.py:186-187) while ``POST /search`` 404s on store meta
   absence (routes_search.py:296-297) — two independent state stores. Such
   divergence is created by a harness that reuses/persists config while resetting
   the store between runs, not by any single product operation.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_DOCS = {
    "alpha.md": "# Alpha\nThe quick brown fox jumps over the lazy dog.\n",
    "beta.md": "# Beta\nPython is a programming language created by Guido van Rossum.\n",
    "gamma.md": "# Gamma\nDocker containers are lightweight isolated environments.\n",
}


def _make_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "archon-test-docs"
    corpus.mkdir()
    for name, text in _DOCS.items():
        (corpus / name).write_text(text, encoding="utf-8")
    return corpus


def _poll_job(client, job_id: str, headers: dict, timeout_s: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=headers)
        assert r.status_code == 200
        data = r.json()
        if data["status"] in {"DONE", "FAILED", "CANCELLED"}:
            return data
        time.sleep(0.1)
    pytest.fail(f"job {job_id} did not finish in {timeout_s}s")


def test_fresh_collection_add_then_search_and_reindex_succeed(tmp_path, monkeypatch):
    """S07/S14/S15/S20/S22 happy path on a CLEAN data dir — proves no product defect."""
    corpus = _make_corpus(tmp_path)
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}", "X-Ingested-By": "cli"}

        # S07 / S14: collection add (POST /collections/) -> 202
        resp = client.post("/collections/", json={"path": str(corpus)}, headers=headers)
        assert resp.status_code == 202, resp.text
        job = resp.json()
        col_name = job["collection"]
        assert col_name == "archon_test_docs"
        done = _poll_job(client, job["job_id"], headers)
        assert done["status"] == "DONE", done

        # S07 step 2: collection list shows it
        listing = client.get("/collections/", headers=headers)
        assert listing.status_code == 200
        names = {c["name"] for c in listing.json()}
        assert col_name in names

        # S15: basic search -> 200
        s = client.post("/search", json={"collection": col_name, "query": "programming language"}, headers=headers)
        assert s.status_code == 200, s.text

        # S20: filtered search -> 200
        sf = client.post(
            "/search",
            json={"collection": col_name, "query": "fox", "filters": {"file_type": "md"}},
            headers=headers,
        )
        assert sf.status_code == 200, sf.text

        # S22: reindex -> 202
        r = client.post(f"/collections/{col_name}/reindex", headers=headers)
        assert r.status_code == 202, r.text


def test_stale_config_path_without_store_meta_reproduces_smoke(tmp_path, monkeypatch):
    """Reproduces the EXACT smoke symptom and pins its precondition: divergence
    between config path membership and store meta.

    Seeds ``config.collections`` with the corpus path (as a persisted/prior-run
    config would) while the store has NO meta row for it. Result: add -> 409,
    search -> 404 — identical to S07 (409) + S15/S20 (404). This state cannot be
    produced by a single successful ``collection add`` (which writes the stub meta
    synchronously before returning 202); it requires config and store to be reset
    on different schedules, i.e. an environmental / harness precondition.
    """
    corpus = _make_corpus(tmp_path)
    resolved = str(Path(str(corpus)).expanduser().resolve())
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}", "X-Ingested-By": "cli"}
        # Simulate dirty prior state: path is in config, but the store was reset
        # (no meta row for archon_test_docs).
        cfg.collections.append(resolved)

        # S07/S14 symptom: add -> 409 "collection already registered"
        add = client.post("/collections/", json={"path": str(corpus)}, headers=headers)
        assert add.status_code == 409
        assert "already registered" in add.json()["detail"]

        # S15/S20 symptom: search the collection the harness expects -> 404
        s = client.post(
            "/search", json={"collection": "archon_test_docs", "query": "programming language"}, headers=headers
        )
        assert s.status_code == 404
        assert s.json()["detail"] == "collection not found"
