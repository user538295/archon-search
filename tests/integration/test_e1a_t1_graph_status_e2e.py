"""E1a / T-1 — e2e: ingest doc with entities; GET /status shows node_count > 0.

Scenario: S3 — GET /status graph collection entry shows node_count > 0 after ingest
with graph enabled.

This is a TestClient-based e2e test exercising the full application stack:
- graph enabled in config
- real LanceDB store + GraphStore
- stubbed spaCy returning two entities for any text
- ingest triggers GraphExtractor → GraphStore.write_graph
- GET /status reflects live GraphStore node_count
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy package into sys.modules that returns two named entities
    for any text.  Must be called before make_real_app because create_app calls
    _check_graph_deps which does ``import spacy``."""

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self) -> None:
            # Always return two named entities — Alice (PERSON) and Google (ORG) —
            # regardless of input text.  This guarantees node_count > 0 after ingest.
            self.ents = [_FakeEnt("Alice", "PERSON"), _FakeEnt("Google", "ORG")]

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]

    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]

    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


# ---------------------------------------------------------------------------
# T-1 e2e test
# ---------------------------------------------------------------------------


def test_e2e_ingest_and_graph_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """make_real_app(graph_enabled=True): ingest entity-rich doc → GET /status shows
    node_count > 0 and backend_threshold_edges present for the ingested collection.

    Covers S3: GET /status graph collection entry has node_count > 0 after ingest.
    """
    # Stub spaCy BEFORE create_app so _check_graph_deps import succeeds and
    # GraphExtractor.extract() uses the stub NLP during asyncio.to_thread.
    _install_spacy_stub(monkeypatch)

    col = "e1a-t1-graph-status"
    doc = tmp_path / "entity_doc.txt"
    doc.write_text(
        "Alice is a software engineer at Google. "
        "She works on the AuthService and TokenValidator components.\n" * 10,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, (
            f"GET /status failed: {resp.status_code} {resp.text}"
        )
        data = resp.json()

        assert "graph" in data, (
            f"'graph' key missing from status response: {list(data.keys())}"
        )
        graph = data["graph"]
        assert graph is not None, (
            "graph sub-object is None even though graph.enabled=True"
        )
        assert graph["enabled"] is True
        assert "backend_threshold_edges" in graph

        # Confirm the ingested collection appears in the per-collection stats.
        col_entries = [c for c in graph["collections"] if c["collection"] == col]
        assert len(col_entries) == 1, (
            f"Expected 1 entry for collection {col!r} in graph.collections, "
            f"got: {graph['collections']}"
        )
        col_stats = col_entries[0]
        assert col_stats["node_count"] > 0, (
            f"Expected node_count > 0 after ingest (spaCy stub returns 2 entities "
            f"per chunk), got: {col_stats['node_count']}"
        )
        # The stub returns Alice (PERSON) and Google (ORG) in the same chunk;
        # co-occurrence edge creation produces 1 edge (N*(N-1)/2 = 1 for N=2).
        # Stable IDs deduplicate across chunks, so edge_count should equal 1.
        assert col_stats["edge_count"] > 0, (
            f"Expected edge_count > 0 after ingest (co-occurrence of 2 entities "
            f"in same chunk produces 1 edge), got: {col_stats['edge_count']}"
        )
