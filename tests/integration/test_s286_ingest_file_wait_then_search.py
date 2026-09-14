"""S286: a completed single-file ``ingest --wait`` must mean the next /search answers.

Bug. The documented two-step round trip

1. ``archon-search ingest --path <file> --collection <name> --wait`` -> exits 0
   (``cli/ingest.py`` POSTs ``/ingest`` then ``_poll_job``\\ s ``GET /jobs/{id}``
   until terminal, exiting 1 only on a non-DONE terminal state)
2. ``POST /search {"collection": "<name>", "query": "<content phrase>"}`` -> 200,
   non-empty ``results``, each carrying ``doc_id`` / ``text`` / ``score`` /
   ``collection``

fails at step 2 with ``status=0 body=None`` — no HTTP response at all, i.e. the
client's read timeout expired.

Root cause. S281 made an ingest job absorb the model warm-up before reporting
DONE (``server/routes_jobs.py`` ``_await_models_warm``), but that absorption is
*bounded* by ``_INGEST_WARMUP_WAIT_SECONDS`` (180 s) while the search side is
**not** bounded at all: ``server/routes_search.py:373`` awaits
``pipeline.warmup_models(embedder)`` outside the ``_SEARCH_TIMEOUT_SECONDS``
(30 s) ``wait_for`` that guards the search itself. When the cold cross-encoder
build outlives the ingest ceiling, ``_await_models_warm`` deliberately logs a
WARNING and lets the job report DONE anyway — its own docstring concedes "a
timeout only means the first search pays the build cost, which is the old
behaviour". So the ingest hands the client the documented completion signal
while search is still unusable, and the search that follows produces no response
inside any normal client read budget: exactly S281's symptom, through the hole
S281 left open and explicitly deferred.

The ratio is the point, not the absolute numbers: a real cold cross-encoder
download+ONNX build on a throttled link outruns the 180 s ceiling the same way
``_COLD_CROSS_ENCODER_LOAD_SECONDS`` outruns the rebound ceiling here. The build
is simulated (``_ColdLoadTextCrossEncoder``, as in
``test_s281_add_wait_then_search_cold_reranker.py``) so the test stays
deterministic and weight-free.

Distinct from the ``Embedder._warmup_failed`` latch flagged alongside S283:
ingest embeds the file's chunks through the very embedder the search resolves
from ``embedder_cache``, so that backend is warm by the time step 2 runs no
matter what the latch says. The cross-encoder is the model nothing on the ingest
path touches.
"""
from __future__ import annotations

import sys
import time

import pytest

from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s286")]

#: Stand-in for the cold download+build of the cross-encoder, compressed. Longer
#: than the rebound ingest ceiling below, mirroring a real build that outruns the
#: production 180 s one.
_COLD_CROSS_ENCODER_LOAD_SECONDS = 4.0

#: ``routes_jobs._INGEST_WARMUP_WAIT_SECONDS`` rebound for the test, keeping the
#: production relationship (ceiling < build) at a scale a test suite can pay.
_REBOUND_INGEST_WARMUP_CEILING_SECONDS = 0.5

#: Budget a normal HTTP client allows a single search. Overrunning it is what the
#: reporter saw as ``status=0 body=None`` — a client-side read timeout, no response.
_CLIENT_SEARCH_TIMEOUT_SECONDS = 2.0

#: Ceiling for the ingest job poll in step 1 (the ``--wait`` the CLI does for us).
_INGEST_WAIT_TIMEOUT_SECONDS = 30.0

_COLLECTION = "s286col"

_FILE_TEXT = (
    "Archon Search is a hybrid retrieval server that combines dense vector "
    "search with full-text search over a document collection."
)

_TOML = """
[database]
reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
"""


def _install_cold_cross_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make cross-encoder construction cost ``_COLD_CROSS_ENCODER_LOAD_SECONDS``.

    ``ModelReranker.predict`` imports ``TextCrossEncoder`` from
    ``fastembed.rerank.cross_encoder`` on its first call (``reranker.py:42``), so
    patching the class there puts the cost exactly where the real ONNX build sits.
    Installed before the app starts, so the cost lands in the background startup
    warm-up — where production puts it.
    """
    module = sys.modules["fastembed.rerank.cross_encoder"]
    base = module.TextCrossEncoder

    class _ColdLoadTextCrossEncoder(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            time.sleep(_COLD_CROSS_ENCODER_LOAD_SECONDS)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, "TextCrossEncoder", _ColdLoadTextCrossEncoder)


def _ingest_file_and_wait(client, file_path, headers) -> None:
    """Step 1: the HTTP calls ``archon-search ingest --path <file> --wait`` makes.

    ``cli/ingest.py`` POSTs ``/ingest`` with ``{collection, path}`` and then polls
    ``GET /jobs/{id}`` until a terminal status, so driving those two directly
    exercises the same server-side path the reported command does. A non-DONE
    terminal state is the CLI's exit 1.
    """
    resp = client.post(
        "/ingest",
        json={"collection": _COLLECTION, "path": str(file_path)},
        headers=headers,
    )
    assert resp.status_code == 202, f"POST /ingest failed: {resp.status_code} {resp.text}"
    job_id = resp.json()["job_id"]

    deadline = time.monotonic() + _INGEST_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}", headers=headers).json()
        if job["status"] == "DONE":
            return
        assert job["status"] not in {"FAILED", "FAILED_EXPIRED", "CANCELLED"}, (
            f"ingest job did not succeed — `ingest --wait` would exit 1: {job}"
        )
        time.sleep(0.05)
    pytest.fail(f"ingest job {job_id} did not finish within {_INGEST_WAIT_TIMEOUT_SECONDS}s")


def test_s286_search_answers_after_single_file_ingest_wait_reports_success(tmp_path, monkeypatch):
    """ingest --path <file> --collection <name> --wait, then search: search must answer."""
    doc = tmp_path / "note.txt"
    doc.write_text(_FILE_TEXT)
    data_dir = tmp_path / "app"
    data_dir.mkdir()

    monkeypatch.setattr(
        "archon_search.server.routes_jobs._INGEST_WARMUP_WAIT_SECONDS",
        _REBOUND_INGEST_WARMUP_CEILING_SECONDS,
    )
    # Before the app starts: the warm-up that runs at startup is the one that must
    # still be in flight when step 2 arrives.
    _install_cold_cross_encoder(monkeypatch)

    with make_real_app(data_dir, monkeypatch, toml_content=_TOML) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}", "X-Ingested-By": "cli"}

        # --- Step 1: archon-search ingest --path <file> --collection <name> --wait ---
        _ingest_file_and_wait(client, doc, headers)

        # --- Step 2: POST /search ---
        started = time.monotonic()
        resp = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": "hybrid retrieval"},
            headers=headers,
        )
        elapsed = time.monotonic() - started

        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results, "expected at least one result"
        for key in ("doc_id", "text", "score"):
            assert results[0][key] is not None, f"result missing {key}: {results[0]}"
        assert results[0]["collection"] == _COLLECTION, results[0]

        assert elapsed < _CLIENT_SEARCH_TIMEOUT_SECONDS, (
            f"first POST /search took {elapsed:.1f}s after `ingest --path <file> --collection "
            f"{_COLLECTION} --wait` reported the job DONE (the CLI's exit 0). The cold "
            f"cross-encoder build outran _INGEST_WARMUP_WAIT_SECONDS, so the ingest job "
            f"reported DONE with the model still cold, and routes_search.py:373 then awaits "
            f"warmup_models() outside _SEARCH_TIMEOUT_SECONDS with no bound of its own. A real "
            f"client times out and records status=0 body=None (S286). A completed ingest must "
            f"not be a signal the server hands out while search is still unusable."
        )
