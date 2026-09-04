"""Integration e2e tests for T-1: PPR mode accepted; guard paths return correct errors.

Covers:
- POST /search graph_mode="ppr" with graph-enabled app → 200, ppr_entities_matched=0.
  The spaCy stub satisfies the app-creation graph-deps import (_check_graph_deps); the
  ppr_entities_matched=0 value is the BE-3 hardcoded fallback in _search_ppr_mode
  (pipeline.py), not a consequence of empty entity extraction.
- POST /search graph_mode="ppr" when graph disabled → 422 with exact error message.
- POST /search scope_filter + graph_mode="ppr" → 422 with exact error message.

Production code: archon_search/server/routes_search.py (422 guards),
pipeline.py (_search_ppr_mode stub path).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_spacy_stub_no_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the gliner-backed extraction engine to return zero entities.

    Historical name kept for minimal diff; no longer touches spaCy — BE-11
    rewired GraphExtractor onto ProseExtractionBackend/gliner.
    """
    from tests._graph_engine_stub import install_graph_engine_stub_no_entities

    install_graph_engine_stub_no_entities(monkeypatch)


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_e2h_t1_pprMode_returns200_pprEntitiesMatchedPresent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PPR mode with graph enabled and no entity matches → 200, ppr_entities_matched=0."""
    _install_spacy_stub_no_entities(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc = tmp_path / "doc.txt"
        doc.write_text("Hello world, this is a test document.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": "col", "query": "hello", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "ppr_entities_matched" in body
        assert body["ppr_entities_matched"] == 0
        assert "results" in body
        assert len(body["results"]) > 0, "Expected at least one search result after ingest"


def test_e2h_t1_pprMode_graphDisabled_returns422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PPR mode with graph disabled → 422, detail mentions 'graph'."""
    # No spaCy stub needed — graph_enabled=False so _check_graph_deps is not called
    with make_real_app(tmp_path, monkeypatch, graph_enabled=False) as (client, cfg, api_key):
        doc = tmp_path / "doc.txt"
        doc.write_text("Hello world.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": "col", "query": "hello", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        detail = body.get("detail")
        assert detail == "graph_mode requires [graph] enabled=true in server config", (
            f"Unexpected detail: {detail!r}"
        )


def test_e2h_t1_pprMode_scopeFilter_returns422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scope_filter + graph_mode=ppr → 422 (REST surface guard)."""
    _install_spacy_stub_no_entities(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc = tmp_path / "doc.txt"
        doc.write_text("Hello world.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collection": "col",
                "query": "hello",
                "graph_mode": "ppr",
                "scope_filter": "user:alice",
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        detail = body.get("detail")
        assert detail == "scope_filter is not supported with graph_mode", (
            f"Unexpected detail: {detail!r}"
        )
