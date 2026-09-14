"""S283: a *latched* warm-up failure pushes the cold model build into POST /search.

Bug (``Documentation/Backlog/S283-search_returns_results_with_documented_fields.md``):
the documented four-step round trip

1. ``archon-search collection add <tmpdir> --wait``  -> exits 0, "ingested successfully."
2. ``archon-search collection list``                 -> the collection appears with docs >= 1
3. ``archon-search collection info <derived-name>``  -> exits 0
4. ``POST /search {"collection": ..., "query": ...}`` -> 200, non-empty ``results``, each
   carrying all eight documented fields and ``collection`` equal to the queried name

fails at step 4 with ``status=0 body=None`` — no HTTP response at all, i.e. the
client's read timeout expired.

Root cause — distinct from S281. ``Reranker._warmup_failed`` (``reranker.py:85-97``)
is a *permanent latch*: the first build that raises for any reason (an HF 429, a
network blip, an unwritable cache) sets it, and every later ``warmup()`` returns
immediately forever. ``SearchPipeline.warmup_models`` swallows that failure with a
WARNING (``pipeline.py:411-415``), so every gate built on it silently becomes a
no-op:

* S281's ingest gate ``_await_models_warm`` (``server/routes_jobs.py:199-226``)
  returns at once, and the job still reports ``DONE`` — step 1 prints success.
* ``server/routes_search.py:369`` ``await pipeline.warmup_models(embedder)``
  returns at once too, so the cold ONNX build is no longer pre-paid off the
  request path. It happens *inside* ``pipeline.search`` →
  ``Reranker.rerank`` → ``asyncio.to_thread(ModelReranker.predict)``
  (``reranker.py:39-48``) — on the request path, past any client's patience.

So this is the inverse of S281. There the warm-up worked and the wait merely sat
outside ``_SEARCH_TIMEOUT_SECONDS``; here the warm-up is latched off and there is
no wait at all, so neither the S279 ``/ready`` gate nor the S281 ingest gate ever
sees a cold model again.

Reproduced against a real ``uv run archon-search serve`` with a model cache made
unwritable for the startup warm-up only (the transient failure), then writable
again before the round trip::

    [  2.52s] GET /health                        200
    [ 92.86s] GET /ready                         ready=true, checks.models="fail"
    [103.32s] collection add <dir> --wait        rc=0, "ingested successfully."
    [103.60s] collection list                    rc=0, docs=1
    [103.84s] collection info archon_s283_live   rc=0
    [113.85s] POST /search (1st)                 ReadTimeout after 10.01s   <- status 0
    [113.93s] POST /search (2nd)                 200 after 0.08s

The transient failure and the cold build are simulated here so the test stays
deterministic and weight-free; the ratio, not the absolute number, is the point.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s283")]

#: Stand-in for the cold download+build of the cross-encoder, paid by whoever
#: constructs it first. Far longer than the stubbed ingest that precedes it.
_COLD_CROSS_ENCODER_LOAD_SECONDS = 4.0

#: Budget a normal HTTP client allows a single search. Overrunning it is what the
#: reporter saw as ``status=0 body=None`` — a client-side read timeout, no response.
_CLIENT_SEARCH_TIMEOUT_SECONDS = 2.0

#: Ceiling for the ingest job poll in step 1 (the ``--wait`` the CLI does for us).
_INGEST_WAIT_TIMEOUT_SECONDS = 30.0

#: Ceiling for waiting out the backgrounded startup warm-up before step 1.
_WARMUP_SETTLE_TIMEOUT_SECONDS = 30.0

#: The eight fields the documented ``SearchResponse`` result carries
#: (``Documentation/Architecture/600_api_reference_or_public_interface.md``).
_DOCUMENTED_RESULT_FIELDS = (
    "doc_id",
    "chunk_id",
    "text",
    "score",
    "source_path",
    "file_type",
    "language",
    "collection",
)

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


class _TransientCrossEncoderFailure(RuntimeError):
    """Stands in for an HF 429 / network blip / unwritable cache during the build."""


def _install_transient_then_cold_cross_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """First construction raises; every later one succeeds after a cold-build delay.

    ``ModelReranker.predict`` imports ``TextCrossEncoder`` from
    ``fastembed.rerank.cross_encoder`` on its first call (``reranker.py:42``), so
    patching the class there puts both the transient failure and the cost exactly
    where the real ONNX build sits. Installed *before* the app starts, so the
    failure lands in the background startup warm-up — which is what latches
    ``Reranker._warmup_failed`` and disarms every downstream warm-up gate.
    """
    module = sys.modules["fastembed.rerank.cross_encoder"]
    base = module.TextCrossEncoder
    attempts = {"n": 0}

    def _built_by_the_reranker() -> bool:
        """True when this construction is the warm-up build, not the startup probe.

        ``model_validation._load_cross_encoder`` imports the same symbol from the
        same module for its resolvability probe, and it runs as a sibling
        background task. Counting *its* construction as attempt 1 would hand the
        transient failure to the probe and leave the warm-up succeeding — the
        exact opposite of what this test sets up. Which task wins the race is
        decided by how many awaits precede the reranker build inside
        ``warmup_models``, so keying off the caller rather than a global counter
        is what makes this deterministic.
        """
        frame = sys._getframe()
        while frame is not None:
            if frame.f_code.co_filename.endswith(f"archon_search{os.sep}reranker.py"):
                return True
            frame = frame.f_back
        return False

    class _TransientThenColdTextCrossEncoder(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            if not _built_by_the_reranker():
                super().__init__(*args, **kwargs)
                return
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _TransientCrossEncoderFailure("cross-encoder download failed (transient)")
            time.sleep(_COLD_CROSS_ENCODER_LOAD_SECONDS)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, "TextCrossEncoder", _TransientThenColdTextCrossEncoder)


def _settle_startup_warmup(client) -> None:
    """Drive the event loop until the background startup warm-up has run.

    ``TestClient`` only advances the app's loop while a request is in flight, so
    the cheap ``/health`` poll is what lets the warm-up task reach the reranker.
    """
    reranker = client.app.state.pipeline._reranker
    deadline = time.monotonic() + _WARMUP_SETTLE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if reranker._warmup_failed or reranker.is_warm:
            return
        client.get("/health")
        time.sleep(0.05)
    pytest.fail("startup warm-up never reached the reranker")


def _add_collection_and_wait(client, corpus_dir, headers) -> str:
    """Step 1: the HTTP calls ``archon-search collection add <dir> --wait`` makes.

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


def test_s283_search_answers_with_documented_fields_after_failed_warmup(tmp_path, monkeypatch):
    """The documented round trip must answer a client even after a warm-up failure."""
    corpus_dir = tmp_path / "archon_s283corpus"
    corpus_dir.mkdir()
    for name, text in _DOCS.items():
        (corpus_dir / name).write_text(text)
    data_dir = tmp_path / "app"
    data_dir.mkdir()

    # Before the app starts: the startup warm-up must be the call that fails, so
    # the latch is already set when the round trip begins.
    _install_transient_then_cold_cross_encoder(monkeypatch)

    with make_real_app(data_dir, monkeypatch, toml_content=_TOML) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        _settle_startup_warmup(client)
        reranker = client.app.state.pipeline._reranker
        assert reranker._warmup_failed, "startup warm-up was expected to fail transiently"
        assert not reranker.is_warm, "the cross-encoder must still be cold after that failure"

        # --- Step 1: collection add <tmpdir> --wait ---
        collection = _add_collection_and_wait(client, corpus_dir, headers)

        # --- Step 2: collection list ---
        listing = client.get("/collections/", headers=headers)
        assert listing.status_code == 200, listing.text
        entry = next((c for c in listing.json() if c["name"] == collection), None)
        assert entry is not None, f"{collection!r} missing from GET /collections/: {listing.text}"
        assert entry["doc_count"] >= 1, listing.text

        # --- Step 3: collection info <derived-name> ---
        info = client.get(f"/collections/{collection}", headers=headers)
        assert info.status_code == 200, info.text

        # --- Step 4: POST /search ---
        started = time.monotonic()
        resp = client.post(
            "/search",
            json={"collection": collection, "query": "hybrid retrieval"},
            headers=headers,
        )
        elapsed = time.monotonic() - started

        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results, "expected at least one result for content just ingested"
        for field in _DOCUMENTED_RESULT_FIELDS:
            assert field in results[0], f"result missing documented field {field}: {results[0]}"
        assert results[0]["collection"] == collection, results[0]

        assert elapsed < _CLIENT_SEARCH_TIMEOUT_SECONDS, (
            f"first POST /search took {elapsed:.1f}s after `collection add --wait` reported the "
            f"ingest complete — Reranker._warmup_failed (reranker.py:85) was latched by a "
            f"transient startup failure, so both the S281 ingest gate (routes_jobs.py:218) and "
            f"the search route's own pre-pay (routes_search.py:369) silently no-opped and the "
            f"cold cross-encoder build ran inside the request. A real client times out and "
            f"records status=0 body=None (S283). One transient warm-up failure must not disarm "
            f"every warm-up gate for the life of the process."
        )
