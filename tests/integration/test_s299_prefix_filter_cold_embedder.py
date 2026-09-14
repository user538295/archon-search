"""tests/integration/test_s299_prefix_filter_cold_embedder.py

S299 regression — ``source_path_prefix`` filter flow over a cold query embedder.

Bug report: ``Documentation/Backlog/S299-results_restricted_to_prefix.md``.  The
captured evidence is ``unfiltered POST /search returned 0: None`` — status 0, i.e.
the request never produced an HTTP response at all.

Root cause exercised here: ``routes_search.py`` resolves the request's embedder with
``asyncio.wait_for(embedder_cache.get_or_load(...), _EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS)``
*before* ``warmup_for_search``.  That wait used to run for 30 s and then raise
``EmbedderNotReadyError`` → 503, which defeats the S290 policy stated in
``_search_budget.py`` — an unavailable embedder must degrade to the FTS leg alone
(``fts_only=True``), never park the connection.  The FTS index is built at ingest,
so the corpus below is fully answerable without any query vector.

``make_embedder`` is monkeypatched to be slow because that is the only thing this
stage can actually block on: it returns a *lazy* backend (``embedder.py``), so no
ONNX build happens here — in production the equivalent stall is a saturated
``asyncio.to_thread`` executor or a deduplicated wait behind another loader.

Run with:
    uv run pytest tests/integration/test_s299_prefix_filter_cold_embedder.py --no-cov
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_DOC_TEXT = (
    "# {name}\n\n"
    "The widget system is documented here. A widget can be configured.\n"
    "Widget behaviour, widget lifecycle and widget troubleshooting are described.\n"
)

# Pinned onto the production constants (tests/CLAUDE.md convention: rebind the
# *TIMEOUT* constant, never shrink an outer wait_for).  This is the value the route
# already uses, so the pin is what fails if someone raises the route's bound back
# towards the 30 s that S299 was: the assertions below stay in a 0.5 s window.
_SHORT_WAIT_SECONDS = 0.5
# Longer than every rebound wait above, so the load is still in flight when the
# route gives up on it — exactly the window S299 was captured in.
_COLD_LOAD_SECONDS = 3.0
# The warning the route must log when it gives up and degrades.  PRESENCE anchor:
# without it a 200 below would only prove the request was served, not that it was
# served through the S299 degrade path.
_DEGRADE_LOG_FRAGMENT = "unavailable after"


def test_s299_prefix_filter_survives_cold_query_embedder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S299 steps 1-3 while the query embedder is mid-cold-build.

    Step 2 (unfiltered) must answer 200 with both a ``guide_`` and a ``manual_``
    path — the FTS leg alone can serve this corpus, which is what ``fts_only``
    degradation exists for.  Step 3 (``source_path_prefix``) must answer 200 with
    a non-empty, fully prefix-scoped result set and echo ``applied_filters``.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name in ("guide_alpha", "guide_beta", "manual_gamma", "manual_delta"):
        (corpus / f"{name}.md").write_text(_DOC_TEXT.format(name=name))

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        col = "s299-prefix-col"
        for name in ("guide_alpha", "guide_beta", "manual_gamma", "manual_delta"):
            ingest_file_via_path(client, col, str(corpus / f"{name}.md"), api_key=api_key)

        # --- put the query embedder back into its cold-build state -----------
        import archon_search.embedder_cache as embedder_cache_mod
        import archon_search.server.routes_search as routes_search_mod

        real_make_embedder = embedder_cache_mod.make_embedder
        loads_finished: list[threading.Event] = []

        def _slow_make_embedder(model_name: str, **kwargs: object) -> object:
            """Stand in for a stalled cache handoff (seconds, in a worker thread)."""
            done = threading.Event()
            loads_finished.append(done)
            try:
                time.sleep(_COLD_LOAD_SECONDS)
                return real_make_embedder(model_name, **kwargs)  # type: ignore[arg-type]
            finally:
                done.set()

        monkeypatch.setattr(embedder_cache_mod, "make_embedder", _slow_make_embedder)
        monkeypatch.setattr(
            embedder_cache_mod, "_LOAD_WAIT_TIMEOUT_SECONDS", _SHORT_WAIT_SECONDS
        )
        monkeypatch.setattr(
            routes_search_mod, "_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS", _SHORT_WAIT_SECONDS
        )
        cache = client.app.state.embedder_cache
        cache._cache.clear()

        # --- step 2: unfiltered search ---------------------------------------
        t0 = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="archon_search.server.routes_search"):
            resp = client.post(
                "/search", json={"collection": col, "query": "widget"}, headers=headers
            )
        elapsed = time.monotonic() - t0
        # PRESENCE anchor: prove the request went through the S299 degrade path
        # rather than simply finding a warm embedder (which would make every
        # assertion below pass without exercising the fix at all).
        assert any(_DEGRADE_LOG_FRAGMENT in r.getMessage() for r in caplog.records), (
            "route never logged the cold-embedder degrade; this request did not "
            f"exercise the S299 path. Records: {[r.getMessage() for r in caplog.records]}"
        )
        assert resp.status_code == 200, (
            f"unfiltered POST /search returned {resp.status_code} after {elapsed:.2f}s: "
            f"{resp.text} — a cold query embedder must degrade to the FTS leg "
            f"(fts_only=True, _search_budget.py EMBEDDER_WARMUP_WAIT_SECONDS), not park "
            f"the connection on the cold build and then 503"
        )
        unfiltered = resp.json()["results"]
        paths = [r["source_path"] for r in unfiltered]
        guides = [p for p in paths if Path(p).name.startswith("guide_")]
        manuals = [p for p in paths if Path(p).name.startswith("manual_")]
        if not guides or not manuals:
            pytest.skip(
                f"corpus/ranking precondition unmet — top-K lacks a whole group: {paths}"
            )

        # --- step 3: same search, prefix-filtered ----------------------------
        prefix = str(Path(guides[0]).parent / "guide")
        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "widget",
                "filters": {"source_path_prefix": prefix},
            },
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"prefix-filtered POST /search returned {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        filtered = data["results"]
        assert filtered, f"prefix {prefix!r} must not empty the result set"
        for r in filtered:
            assert r["source_path"].startswith(prefix), (
                f"result outside the prefix scope: {r['source_path']!r} !^ {prefix!r}"
            )
        applied = data["applied_filters"]
        assert applied is not None, "applied_filters must be non-null when filters were sent"
        assert applied["source_path_prefix"] == prefix, (
            f"applied_filters must echo the parsed prefix; got {applied['source_path_prefix']!r}"
        )

        # The abandoned loads keep running in their worker threads; join them on
        # their own events so the monkeypatched factory is not still executing when
        # monkeypatch unwinds and tmp_path is torn down.  (A threading.active_count()
        # poll would never settle — pytest-xdist and TestClient both keep threads
        # alive for the whole session, so it would just burn its full budget.)
        for done in loads_finished:
            assert done.wait(_COLD_LOAD_SECONDS + 10), "abandoned embedder load never finished"
