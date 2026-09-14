"""tests/integration/test_s303_language_filter.py

S303 regression — ``filters.language`` acceptance/rejection contract.

Bug report: ``Documentation/Backlog/S303-literal_unknown_accepted.md``.

Steps:
  1. non-ISO ``language`` ("notalanguage")  -> 422
  2. ``language=unknown``                   -> 200 (accepted sentinel)
  3. ``language=en`` (ISO 639-1)            -> 200, applied_filters.language in {en, eng}
  4. ``language=eng`` (ISO 639-3)           -> 200

On the ``minimal`` baseline every chunk is ``""``-tagged, so the strict-equality
language filter excludes all of them: empty ``results`` is expected for steps
2-4 and the per-result equality checks hold vacuously.

Run with:
    uv run pytest tests/integration/test_s303_language_filter.py --no-cov
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
    "# Fox notes\n\n"
    "The quick brown fox jumps over the lazy dog.\n"
    "A quick brown fox is quick, and the brown fox is a fox.\n"
)
_QUERY = "quick brown fox"


def _assert_language_contract(client, col: str, api_key: str) -> None:
    """Steps 1-4 of S303 against an already-ingested collection."""
    headers = {"Authorization": f"Bearer {api_key}"}

    def _post(language: str):
        return client.post(
            "/search",
            json={"collection": col, "query": _QUERY, "filters": {"language": language}},
            headers=headers,
        )

    # Step 1 — non-ISO language is an invalid filter.
    resp = _post("notalanguage")
    assert resp.status_code == 422, (
        f"non-ISO language must be rejected with 422, got {resp.status_code}: {resp.text}"
    )

    # Step 2 — "unknown" is an accepted sentinel.
    resp = _post("unknown")
    assert resp.status_code == 200, (
        f"expected 200 for accepted 'unknown' sentinel, got {resp.status_code}: {resp.text}"
    )
    for item in resp.json()["results"]:
        assert item.get("language") == "unknown", (
            f"strict equality violated: result carries language={item.get('language')!r}"
        )

    # Step 3 — valid ISO 639-1 code.
    resp = _post("en")
    assert resp.status_code == 200, (
        f"expected 200 for ISO 639-1 'en', got {resp.status_code}: {resp.text}"
    )
    data = resp.json()
    applied = data["applied_filters"]
    assert applied is not None, "applied_filters must be non-null when filters were sent"
    assert applied["language"] in {"en", "eng"}, (
        f"applied_filters.language must echo the parsed filter, got {applied['language']!r}"
    )
    for item in data["results"]:
        assert item.get("language") == "en", (
            f"strict equality violated: result carries language={item.get('language')!r}"
        )

    # Step 4 — valid ISO 639-3 code is accepted too.
    resp = _post("eng")
    assert resp.status_code == 200, (
        f"expected 200 for ISO 639-3 'eng', got {resp.status_code}: {resp.text}"
    )


def test_s303_language_filter_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S303 steps 1-4 (warm path)."""
    doc = tmp_path / "corpus" / "fox.md"
    doc.parent.mkdir()
    doc.write_text(_DOC_TEXT)

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        col = "s303-docs"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post("/search", json={"collection": col, "query": _QUERY}, headers=headers)
        assert resp.status_code == 200, (
            f"control POST /search returned {resp.status_code}: {resp.text}"
        )

        _assert_language_contract(client, col, api_key)


# --- cold-query-embedder variant (S299 pattern) ------------------------------
_SHORT_WAIT_SECONDS = 0.5
_COLD_LOAD_SECONDS = 3.0
_DEGRADE_LOG_FRAGMENT = "unavailable after"


def test_s303_language_filter_over_cold_query_embedder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S303 steps 1-4 while the query embedder is mid-cold-build (status-0 evidence)."""
    doc = tmp_path / "corpus" / "fox.md"
    doc.parent.mkdir()
    doc.write_text(_DOC_TEXT)

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        col = "s303-cold-docs"
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
        monkeypatch.setattr(embedder_cache_mod, "_LOAD_WAIT_TIMEOUT_SECONDS", _SHORT_WAIT_SECONDS)
        monkeypatch.setattr(
            routes_search_mod, "_EMBEDDER_LOAD_WAIT_TIMEOUT_SECONDS", _SHORT_WAIT_SECONDS
        )
        client.app.state.embedder_cache._cache.clear()

        with caplog.at_level(logging.WARNING, logger="archon_search.server.routes_search"):
            resp = client.post(
                "/search", json={"collection": col, "query": _QUERY}, headers=headers
            )
        assert any(_DEGRADE_LOG_FRAGMENT in r.getMessage() for r in caplog.records), (
            "route never logged the cold-embedder degrade; this request did not exercise "
            f"the cold path. Records: {[r.getMessage() for r in caplog.records]}"
        )
        assert resp.status_code == 200, (
            f"control POST /search returned {resp.status_code}: {resp.text}"
        )

        _assert_language_contract(client, col, api_key)

        for done in loads_finished:
            assert done.wait(_COLD_LOAD_SECONDS + 10), "abandoned embedder load never finished"
