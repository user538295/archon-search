"""S288: multi-collection ``/search`` fan-out must not block on a cold reranker.

Bug. ``POST /search {"collections": [...], "query": "..."}`` produces no HTTP
response at all inside a normal client read budget when the cross-encoder is
still building — the reporter recorded it as ``status=0`` / ``body=None``
(``AssertionError: source /search returned 0`` / ``assert 0 == 200``), the same
symptom family as S278/S279/S281/S283/S286.

Root cause. The ``body.collections is not None`` fan-out branch of
``server/routes_search.py`` ``search()`` awaited ``pipeline.warmup_models()``
with **no** ``reranker_timeout``. ``SearchPipeline.warmup_models`` treats
``reranker_timeout=None`` as "await ``Reranker.warmup()`` unbounded", and the
call sits outside every request-side ``wait_for``, so a cold ONNX
download+build holds the connection open for as long as the build takes. S286
fixed exactly this hole on the *single*-collection branch (which passes
``reranker_timeout=_RERANKER_WARMUP_WAIT_SECONDS`` and degrades to fused
vector+FTS ranking on timeout); commit ``93aad04f`` states verbatim that "the
multi-collection fan-out path is unchanged and still exposed to the same gap",
which is this ticket.

Fixed looks like: the fan-out branch bounds its reranker warm-up the same way
the single-collection branch does — the request answers within a normal client
read timeout, degrading to the fused ranking (``rerank=False`` through
``SearchPipeline.search_many``, so every result carries
``reranker_score is None`` and is ordered by the fused RRF score) while the
build finishes in the background, instead of parking the connection.

The test pins all three halves of that contract, so a merely-warm reranker
cannot make it pass: the cross-encoder is asserted *still cold* at the moment
the fan-out POST is issued, the response must arrive inside the client budget,
and the payload must show the degraded-but-correct fused ranking across **two**
collections (exercising the cross-collection merge plus the single global
rerank pass in ``search_many``).

As in ``test_s286_ingest_file_wait_then_search.py`` the build is *simulated*
(``_ColdLoadTextCrossEncoder``) so the test stays deterministic and weight-free.
The ratio is the point, not the absolute numbers: a real cold cross-encoder
download+ONNX build on a throttled link outruns the ingest-side ceiling exactly
the way ``_COLD_CROSS_ENCODER_LOAD_SECONDS`` outruns the rebound ceiling here.
"""
from __future__ import annotations

import importlib
import time

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s288")]

#: Stand-in for the cold download+build of the cross-encoder, compressed. Must
#: outlive the rebound ingest ceiling below *and* the ingest itself, so the
#: fan-out search lands while the build is still in flight.
_COLD_CROSS_ENCODER_LOAD_SECONDS = 6.0

#: ``routes_jobs._INGEST_WARMUP_WAIT_SECONDS`` rebound for the test, keeping the
#: production relationship (ceiling < build) at a scale a test suite can pay.
_REBOUND_INGEST_WARMUP_CEILING_SECONDS = 0.5

#: Budget a normal HTTP client allows a single search. Overrunning it is what the
#: reporter saw as ``status=0 body=None`` — a client-side read timeout, no response.
_CLIENT_SEARCH_TIMEOUT_SECONDS = 2.0

#: Two collections, so the POST exercises the cross-collection merge and the
#: single global rerank pass in ``SearchPipeline.search_many`` — not a fan-out of one.
_COLLECTIONS = ("s288cola", "s288colb")

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
    ``fastembed.rerank.cross_encoder`` on its first call, so patching the class
    there puts the cost exactly where the real ONNX build sits. Imported via
    ``importlib`` rather than read out of ``sys.modules``, so the helper does not
    depend on some earlier test having imported the module first. Installed
    before the app starts, so the cost lands in the background startup warm-up —
    where production puts it.
    """
    module = importlib.import_module("fastembed.rerank.cross_encoder")
    base = module.TextCrossEncoder

    class _ColdLoadTextCrossEncoder(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            time.sleep(_COLD_CROSS_ENCODER_LOAD_SECONDS)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, "TextCrossEncoder", _ColdLoadTextCrossEncoder)


def test_s288_multi_collection_search_answers_while_reranker_is_cold(tmp_path, monkeypatch):
    """POST /search with ``collections`` must answer inside a client read budget."""
    data_dir = tmp_path / "app"
    data_dir.mkdir()

    monkeypatch.setattr(
        "archon_search.server.routes_jobs._INGEST_WARMUP_WAIT_SECONDS",
        _REBOUND_INGEST_WARMUP_CEILING_SECONDS,
    )
    # Before the app starts: the warm-up that runs at startup is the one that must
    # still be in flight when the fan-out search arrives.
    _install_cold_cross_encoder(monkeypatch)

    with make_real_app(data_dir, monkeypatch, toml_content=_TOML) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        for collection in _COLLECTIONS:
            doc = tmp_path / f"{collection}.txt"
            doc.write_text(f"{_FILE_TEXT} Indexed into {collection}.")
            ingest_file_via_path(client, collection, str(doc), api_key=api_key)

        # The whole point of the regression: the cross-encoder must still be
        # building when the fan-out POST is issued. If startup+ingest outran the
        # simulated build, the unfixed route would also answer in time and this
        # test would pass vacuously — so pin the precondition instead.
        pipeline = client.app.state.pipeline
        assert pipeline._reranker is not None, (
            "this deployment has no reranker at all, so reranker_is_warm is False and every "
            "reranker_score is None for a reason unrelated to the cold-build degradation — "
            "the assertions below would pass vacuously."
        )
        assert not pipeline.reranker_is_warm, (
            "cross-encoder warmed up before the fan-out POST was issued, so this run would "
            "not exercise the cold-reranker path at all — raise "
            "_COLD_CROSS_ENCODER_LOAD_SECONDS rather than letting the test pass vacuously."
        )

        started = time.monotonic()
        resp = client.post(
            "/search",
            json={"collections": list(_COLLECTIONS), "query": "hybrid retrieval"},
            headers=headers,
        )
        elapsed = time.monotonic() - started

        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results, "expected at least one result"

        assert elapsed < _CLIENT_SEARCH_TIMEOUT_SECONDS, (
            f"multi-collection POST /search took {elapsed:.1f}s with a cold cross-encoder. "
            f"The fan-out branch of routes_search.search() awaits pipeline.warmup_models() "
            f"with no reranker_timeout, so it blocks for the whole ONNX build — outside every "
            f"request-side wait_for. A real client times out and records status=0 body=None "
            f"(S288). S286 bounded this on the single-collection branch; the fan-out branch "
            f"must do the same and degrade to the fused vector+FTS ranking instead of parking "
            f"the connection."
        )

        # Degraded path, positively asserted: warmup_models returned False, so
        # search_many ran with rerank=False and no candidate carries a
        # reranker_score. A warm cross-encoder would populate it — which is how
        # this distinguishes "bounded and degraded" from "happened to be fast".
        for item in results:
            assert item["reranker_score"] is None, (
                "results were reranked, so the cold-reranker degradation never ran: "
                f"{item}"
            )

        # Field shape and fused-ranking semantics (as in the S286 test).
        for key in ("doc_id", "text", "score"):
            for item in results:
                assert item[key] is not None, f"result missing {key}: {item}"
        scores = [item["score"] for item in results]
        assert scores == sorted(scores, reverse=True), (
            f"fused RRF ranking is not ordered by descending score: {scores}"
        )

        # The cross-collection merge actually ran: both legs contributed.
        assert {item["collection"] for item in results} == set(_COLLECTIONS), (
            f"fan-out merge did not include both collections: {results}"
        )
