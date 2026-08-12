"""S184 — ``POST /search`` must return 200 (not 504) for a query that matches nothing.

Bug report: ``Documentation/Backlog/S184-no_match_returns_200.md``

    AssertionError: POST /search returned 504 (not 200); body={'detail': 'Search timed out'}
    assert 504 == 200

Root cause
----------
``routes_search.search`` wraps the whole pipeline call in
``asyncio.wait_for(..., timeout=_SEARCH_TIMEOUT_SECONDS)`` (a hard-coded 30 s;
``routes_search.py:32``) and maps ``asyncio.TimeoutError`` to
``504 "Search timed out"`` (``routes_search.py:397-416``).

Both ML backends on that path are **lazy**: ``ModelEmbedder.encode``
(``embedder.py:34-41``) and ``ModelReranker.predict`` (``reranker.py:31-44``)
construct their ONNX model on *first call*, inside ``asyncio.to_thread`` — i.e.
inside that 30 s budget.  Nothing warms the cross-encoder at startup: the
lifespan only preloads *embedders* (``app.py:341-357``, ``eager_load_embedders``),
and ``validate_models_async`` probes a throw-away ``TextCrossEncoder`` in its own
thread (``model_validation.py:207-217``) rather than the pipeline's instance.

So the first ``/search`` after startup spends its entire timeout budget on model
download/initialisation and 504s — which is exactly what the e2e probe in the
report (a no-match query, typically the first search of a scenario run) observed.
A no-match query is a *successful* search: the caller must get 200 and a
``results`` array, and a 504 additionally invites clients to retry a request that
cannot succeed any faster.

The cold-load cost is simulated here (``_ColdLoadRerankerBackend``) and
``_SEARCH_TIMEOUT_SECONDS`` is scaled down so the test does not burn 30 s of wall
clock — the same technique the A3 plan prescribed when it extracted that literal
into a module constant.  The ratio (load 4x the budget) matches the real
failure: an ONNX cross-encoder cold start on a machine without cached weights
exceeds 30 s.

Run with:
    uv run pytest tests/integration/test_s184_no_match_returns_200.py --no-cov -n0 -x
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from archon_search.server import routes_search
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

# The no-match probe from the bug report's reproduction steps.
_NO_MATCH_QUERY = "xyzzy_no_such_term_9999"

# Scaled stand-ins for the production 30 s budget vs. a cold ONNX cross-encoder start.
_SCALED_SEARCH_TIMEOUT_S = 0.5
_SCALED_COLD_LOAD_S = 2.0


class _ColdLoadRerankerBackend:
    """Mirrors ``ModelReranker``: builds its model on the FIRST ``predict`` call.

    ``reranker.py:31-44`` does exactly this — the ``TextCrossEncoder(...)``
    constructor runs under a ``threading.Lock`` inside ``predict``.  Here that
    constructor is replaced by a sleep so the cold-start cost is deterministic.
    """

    def __init__(self, load_seconds: float) -> None:
        self._load_seconds = load_seconds
        self._model: object | None = None
        self._lock = threading.Lock()

    @property
    def is_warm(self) -> bool:
        return self._model is not None

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        if self._model is None:
            with self._lock:
                if self._model is None:
                    time.sleep(self._load_seconds)  # TextCrossEncoder(...) construction
                    self._model = object()
        return [0.5] * len(pairs)


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _write_corpus(tmp_path: Path) -> Path:
    doc = tmp_path / "docs.md"
    doc.write_text(
        "# Archon Test Docs\n\nHybrid retrieval, routing and reranking notes.\n" * 5,
        encoding="utf-8",
    )
    return doc


def test_s184_no_match_query_returns_200_on_first_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S184 repro: the first search pays the lazy reranker load inside the
    route's timeout budget, so a no-match query 504s instead of returning 200."""
    doc = _write_corpus(tmp_path)

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        col = "archon_test_docs"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Reproduce a server whose cross-encoder has never been loaded: nothing in
        # the lifespan warms it, so the first /search constructs it in-request.
        pipeline = client.app.state.pipeline
        assert pipeline._reranker is not None, "search path must have a reranker for this repro"
        monkeypatch.setattr(
            pipeline._reranker, "_backend", _ColdLoadRerankerBackend(_SCALED_COLD_LOAD_S)
        )
        monkeypatch.setattr(routes_search, "_SEARCH_TIMEOUT_SECONDS", _SCALED_SEARCH_TIMEOUT_S)

        resp = client.post(
            "/search",
            json={"collection": col, "query": _NO_MATCH_QUERY},
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, (
            f"POST /search returned {resp.status_code} (not 200); body={resp.json()}"
        )
        body = resp.json()
        assert isinstance(body.get("results"), list), (
            f"response must carry a 'results' array; got: {body!r}"
        )


def test_s184_no_match_query_returns_200_when_reranker_is_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: identical request, reranker already warm → 200 + results array.

    Pins the cold first-use model load — not the query, the collection, or LanceDB
    FTS having no hit — as the cause of the 504 in the test above.
    """
    doc = _write_corpus(tmp_path)

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        col = "archon_test_docs"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        pipeline = client.app.state.pipeline
        backend = _ColdLoadRerankerBackend(_SCALED_COLD_LOAD_S)
        backend.predict([("warm", "up")])  # pay the load cost before the request
        assert backend.is_warm, "control requires an already-loaded backend"
        monkeypatch.setattr(pipeline._reranker, "_backend", backend)
        monkeypatch.setattr(routes_search, "_SEARCH_TIMEOUT_SECONDS", _SCALED_SEARCH_TIMEOUT_S)

        resp = client.post(
            "/search",
            json={"collection": col, "query": _NO_MATCH_QUERY},
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, (
            f"warm-reranker control must return 200; got {resp.status_code}: {resp.text}"
        )
        assert isinstance(resp.json().get("results"), list)
