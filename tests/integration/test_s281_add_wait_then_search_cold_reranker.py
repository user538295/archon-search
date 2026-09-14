"""S281: a completed ``collection add --wait`` must mean the next /search answers.

Bug (``Documentation/Completed/S281-search_returns_results_for_ingested_content.md``):
the documented three-step round trip

1. ``archon-search collection add <tmpdir> --wait``  -> exits 0, "ingested successfully."
2. ``archon-search collection info <derived-name>``  -> doc_count >= 2, chunk_count > 0
3. ``POST /search {"collection": ..., "query": ...}`` -> 200 with non-empty results

fails at step 3 with ``status=0 body=None`` — no HTTP response at all, i.e. the
client's read timeout expired.

Root cause. Steps 1 and 2 exercise only the *embedder*; nothing on the ingest or
metadata path ever touches the cross-encoder. The cross-encoder is built by the
background startup warm-up (``server/app.py`` ``_run_model_warmup``), which holds
``Reranker._warmup_lock`` for the whole cold ONNX build. The search route then
awaits that same lock at ``server/routes_search.py`` ::

    await pipeline.warmup_models(embedder)      # outside the wait_for below
    result = await asyncio.wait_for(pipeline.search(...), _SEARCH_TIMEOUT_SECONDS)

That await sits *outside* ``_SEARCH_TIMEOUT_SECONDS`` (30 s) and is bounded only by
``Reranker._WARMUP_TIMEOUT_SECONDS`` (300 s, ``reranker.py:16``), so the request
produces no response within any normal client budget. A successful, fully drained
ingest job is therefore no signal whatsoever that search is usable — which is
precisely the signal the documented round trip tells a client to act on.

Reproduced against a real ``uv run archon-search serve`` (default embedder with a
warm ``FASTEMBED_CACHE_PATH``, ``reranker_model = "BAAI/bge-reranker-base"``, fresh
``ARCHON_SEARCH_DATA_DIR`` so only the cross-encoder cache is cold)::

    [  2.11s] GET  /health                       200
    [ 64.86s] collection add <dir> --wait        rc=0, "ingested successfully."
    [ 65.16s] collection info probecol           doc_count: 2, chunk_count: 2
    [ 70.16s] POST /search (1st)                 ReadTimeout after 5.01s   <- status 0
    [ 75.18s] POST /search (2nd)                 ReadTimeout after 5.01s   <- status 0
    [ 79.79s] POST /search (3rd)                 200 after 4.61s

This is the sibling of S279 (``test_s279_max_profile_cold_reranker.py``), which
closed the *readiness-probe* hole: ``/ready`` now stays 503 while warm-up runs.
The hole this test covers is different and still open — the round trip above has
no readiness step. ``collection add --wait`` returning success is itself a
completion signal the server hands the client, and it is emitted while the
cross-encoder is still cold.

The cold build is simulated (``_ColdLoadTextCrossEncoder``) so the test stays
deterministic and weight-free; the ratio, not the absolute number, is the point.
"""
from __future__ import annotations

import sys
import time

import pytest

from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s281")]

#: Stand-in for the cold download+build of the cross-encoder. Short, but far longer
#: than the stubbed ingest that precedes it, so the build is still in flight when
#: the round trip reaches step 3 — the real ordering, compressed.
_COLD_CROSS_ENCODER_LOAD_SECONDS = 4.0

#: Budget a normal HTTP client allows a single search. Overrunning it is what the
#: reporter saw as ``status=0 body=None`` — a client-side read timeout, no response.
_CLIENT_SEARCH_TIMEOUT_SECONDS = 2.0

#: Ceiling for the ingest job poll in step 1 (the ``--wait`` the CLI does for us).
_INGEST_WAIT_TIMEOUT_SECONDS = 30.0

_TOML = """
[database]
reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
"""

_DOCS = {
    "doc1.txt": (
        "Archon Search is a hybrid retrieval server that combines dense vector "
        "search with full-text search over a document collection."
    ),
    "doc2.txt": (
        "The router pre-ranks collections by comparing a query embedding against "
        "each collection's centroid vector before fanning out."
    ),
}


def _install_cold_cross_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make cross-encoder construction cost ``_COLD_CROSS_ENCODER_LOAD_SECONDS``.

    ``ModelReranker.predict`` imports ``TextCrossEncoder`` from
    ``fastembed.rerank.cross_encoder`` on its first call (``reranker.py``), so
    patching the class there puts the cost exactly where the real ONNX build sits.
    Installed *before* the app starts, so the cost lands in the background startup
    warm-up — which is where it lands in production, and which is what makes the
    warm-up lock still held when the round trip reaches its search.
    """
    module = sys.modules["fastembed.rerank.cross_encoder"]
    base = module.TextCrossEncoder

    class _ColdLoadTextCrossEncoder(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            time.sleep(_COLD_CROSS_ENCODER_LOAD_SECONDS)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, "TextCrossEncoder", _ColdLoadTextCrossEncoder)


def _add_collection_and_wait(client, corpus_dir, headers) -> str:
    """Step 1: the HTTP call ``archon-search collection add <dir> --wait`` makes.

    The CLI command is a thin proxy over ``POST /collections/`` plus a job poll
    (``cli/collection.py``), so driving those two directly exercises the same
    server-side path the reported command does. Returns the derived collection name.
    """
    resp = client.post("/collections/", json={"path": str(corpus_dir)}, headers=headers)
    assert resp.status_code == 202, f"POST /collections/ failed: {resp.status_code} {resp.text}"
    body = resp.json()
    job_id = body["job_id"]
    collection = body["collection"]

    deadline = time.monotonic() + _INGEST_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}", headers=headers).json()
        if job["status"] == "DONE":
            return collection
        assert job["status"] not in {"FAILED", "FAILED_EXPIRED", "CANCELLED"}, f"ingest job failed: {job}"
        time.sleep(0.05)
    pytest.fail(f"ingest job {job_id} did not finish within {_INGEST_WAIT_TIMEOUT_SECONDS}s")


def test_s281_search_answers_after_collection_add_wait_reports_success(tmp_path, monkeypatch):
    """The full round trip: add --wait, info, search — search must answer a client."""
    corpus_dir = tmp_path / "s281corpus"
    corpus_dir.mkdir()
    for name, text in _DOCS.items():
        (corpus_dir / name).write_text(text)
    data_dir = tmp_path / "app"
    data_dir.mkdir()

    # Before the app starts: the warm-up that runs at startup is the one that must
    # still be in flight when step 3 arrives.
    _install_cold_cross_encoder(monkeypatch)

    with make_real_app(data_dir, monkeypatch, toml_content=_TOML) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        # --- Step 1: collection add <tmpdir> --wait ---
        collection = _add_collection_and_wait(client, corpus_dir, headers)

        # --- Step 2: collection info <derived-name> ---
        info = client.get(f"/collections/{collection}", headers=headers)
        assert info.status_code == 200, info.text
        assert info.json()["doc_count"] >= 2, info.text
        assert info.json()["chunk_count"] > 0, info.text

        # --- Step 3: POST /search ---
        started = time.monotonic()
        resp = client.post(
            "/search",
            json={"collection": collection, "query": "hybrid retrieval"},
            headers=headers,
        )
        elapsed = time.monotonic() - started

        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results, "expected at least one result"
        for key in ("doc_id", "text", "score"):
            assert results[0][key] is not None, f"result missing {key}: {results[0]}"

        assert elapsed < _CLIENT_SEARCH_TIMEOUT_SECONDS, (
            f"first POST /search took {elapsed:.1f}s after `collection add --wait` reported "
            f"the ingest complete and `collection info` reported doc_count/chunk_count — the "
            f"cold cross-encoder build was still holding Reranker._warmup_lock and the search "
            f"route awaits it outside _SEARCH_TIMEOUT_SECONDS. A real client times out and "
            f"records status=0 body=None (S281). A completed ingest must not be a signal the "
            f"server hands out while search is still unusable."
        )
