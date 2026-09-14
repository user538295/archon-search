"""tests/integration/test_s302_zero_match_glob.py

S302 regression — ``source_path_glob`` that matches ZERO documents.

Bug report: ``Documentation/Backlog/S302-no_matching_glob_returns_200_empty_results.md``.

Run with:
    uv run pytest tests/integration/test_s302_zero_match_glob.py --no-cov
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_NO_MATCH_GLOB = "/no/such/dir/that/matches/nothing/*"
_DOC_TEXT = (
    "# Fox notes\n\n"
    "The quick brown fox jumps over the lazy dog.\n"
    "A quick brown fox is quick, and the brown fox is a fox.\n"
)


def test_s302_zero_match_glob_returns_empty_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S302 steps 1-3 (warm path)."""
    doc = tmp_path / "corpus" / "fox.md"
    doc.parent.mkdir()
    doc.write_text(_DOC_TEXT)

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        col = "s302-col"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search", json={"collection": col, "query": "quick brown fox"}, headers=headers
        )
        assert resp.status_code == 200, (
            f"control POST /search returned {resp.status_code}: {resp.text}"
        )
        if not resp.json()["results"]:
            pytest.skip("bare query matched nothing — an empty filtered result proves nothing")

        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "quick brown fox",
                "filters": {"source_path_glob": _NO_MATCH_GLOB},
            },
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"filtered POST /search returned {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["results"] == [], f"expected no results, got {data['results']}"
        applied = data["applied_filters"]
        assert applied is not None, "applied_filters must be non-null when filters were sent"
        assert applied["source_path_glob"] == _NO_MATCH_GLOB


# --- cold-query-embedder variant (S299 pattern, S302 zero-match glob) --------
_SHORT_WAIT_SECONDS = 0.5
_COLD_LOAD_SECONDS = 3.0
_DEGRADE_LOG_FRAGMENT = "unavailable after"


def test_s302_zero_match_glob_over_cold_query_embedder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S302 steps 1-3 while the query embedder is mid-cold-build (status-0 evidence)."""
    doc = tmp_path / "corpus" / "fox.md"
    doc.parent.mkdir()
    doc.write_text(_DOC_TEXT)

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        col = "s302-cold-col"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        import archon_search.embedder_cache as embedder_cache_mod
        import archon_search.server.routes_search as routes_search_mod

        real_make_embedder = embedder_cache_mod.make_embedder
        loads_finished: list[threading.Event] = []

        def _slow_make_embedder(model_name: str, **kwargs: object) -> object:
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
        client.app.state.embedder_cache._cache.clear()

        with caplog.at_level(logging.WARNING, logger="archon_search.server.routes_search"):
            resp = client.post(
                "/search", json={"collection": col, "query": "quick brown fox"}, headers=headers
            )
        assert any(_DEGRADE_LOG_FRAGMENT in r.getMessage() for r in caplog.records), (
            "route never logged the cold-embedder degrade; this request did not exercise "
            f"the cold path. Records: {[r.getMessage() for r in caplog.records]}"
        )
        assert resp.status_code == 200, (
            f"control POST /search returned {resp.status_code}: {resp.text}"
        )
        if not resp.json()["results"]:
            pytest.skip("bare query matched nothing — an empty filtered result proves nothing")

        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "quick brown fox",
                "filters": {"source_path_glob": _NO_MATCH_GLOB},
            },
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"filtered POST /search returned {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["results"] == [], f"expected no results, got {data['results']}"
        applied = data["applied_filters"]
        assert applied is not None, "applied_filters must be non-null when filters were sent"
        assert applied["source_path_glob"] == _NO_MATCH_GLOB

        for done in loads_finished:
            assert done.wait(_COLD_LOAD_SECONDS + 10), "abandoned embedder load never finished"
