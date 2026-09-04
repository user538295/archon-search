"""Integration tests for BE-3: PPR dispatch stub in pipeline.

Covers:
- POST /search graph_mode="ppr" with no entity match → 200, ppr_entities_matched=0 (S3)
- POST /search graph_mode="ppr" when graph disabled → 422 (S5)
- POST /search graph_mode="ppr" + scope_filter → 422 (S6)
- SearchPipelineResult.ppr_entities_matched=0 propagated to SearchResponse
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


def test_pprMode_noEntityMatch_fallsBackToHybrid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S3: PPR with no entity match falls back to hybrid search, ppr_entities_matched=0."""
    _install_spacy_stub_no_entities(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Ingest a document so the collection exists
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
        assert body["ppr_entities_matched"] == 0


def test_pprMode_graphDisabled_returns422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S5: PPR with graph disabled → 422."""
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


def test_pprMode_scopeFilterConflict_returns422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S6: scope_filter + graph_mode=ppr → 422."""
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


def test_pprMode_searchPipelineResult_carriesPprCount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SearchPipelineResult.ppr_entities_matched=0 is propagated to SearchResponse."""
    _install_spacy_stub_no_entities(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc = tmp_path / "doc.txt"
        doc.write_text("Retrieval augmented generation pipeline test.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": "col", "query": "retrieval", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # ppr_entities_matched must be present and equal to 0 (stub always returns 0)
        assert "ppr_entities_matched" in body
        assert body["ppr_entities_matched"] == 0
        # Verify it's a normal search response (results list present)
        assert "results" in body
