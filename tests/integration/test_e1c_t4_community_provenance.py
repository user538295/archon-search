"""E1c / T-4 — HTTP-level integration tests: community mode traversal provenance.

Covers:
- (a) test_explain_community_local_mode_provenance_http — POST /explain
      graph_mode="local" → response carries community_id in TraversalStep (S3)
- (b) test_explain_community_global_mode_provenance_http — POST /explain
      graph_mode="global" → response carries community_id in TraversalStep (S4)
- (c) test_explain_community_local_table_not_exists_returns_422_http — POST /explain
      graph_mode="local" + communities table not built → 422 (S6)

These tests exercise the full HTTP stack (TestClient → route handler → pipeline →
store) for the community-mode explain success path.  The graph store is stubbed
at the lowest possible level (pipeline._graph_store) so community lookup
succeeds without requiring a real Leiden community detection run.

This is the AI-driven equivalent of the T-4 manual tests:
  "operator ingests corpus, runs E1b community detection, calls POST /explain
   graph_mode='local'/'global', inspects TraversalStep.community_id values."
"""
from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# spaCy stub (same minimal stub as T-2 — no entities extracted; needed so
# create_app's _check_graph_deps can import spacy without the real package)
# ---------------------------------------------------------------------------


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a minimal spaCy stub that returns no named entities."""

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents: list = []

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


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _build_community(community_id: str, entity_ids: list[str], chunk_ids: list[str]):
    """Build a Community object for use in graph store stubs."""
    from archon_search.graph_types import Community
    return Community(
        community_id=community_id,
        entity_ids=entity_ids,
        representative_chunk_ids=chunk_ids,
        built_at=datetime.now(UTC),
    )


def _build_matched_node(node_id: str, name: str, collection: str):
    """Build a GraphNode object for use in graph store stubs."""
    from archon_search.graph_types import EntityType, GraphNode
    return GraphNode(
        id=node_id,
        entity_name=name,
        entity_type=EntityType.system,
        source_doc_id="stub-doc",
        collection_name=collection,
    )


def _get_chunk_id_from_explain(client, col: str, api_key: str) -> str | None:
    """Run a standard (no-graph) explain and return the first chunk_id, or None."""
    resp = client.post(
        "/explain",
        json={"query": "payment service", "collection": col, "top_k": 1},
        headers=_auth(api_key),
    )
    if resp.status_code != 200:
        return None
    results = resp.json().get("results", [])
    if not results:
        return None
    return results[0].get("chunk_id")


# ---------------------------------------------------------------------------
# (a) test_explain_community_local_mode_provenance_http  (S3)
# ---------------------------------------------------------------------------


def test_explain_community_local_mode_provenance_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain graph_mode='local' → response carries community_id in TraversalStep (S3).

    Setup:
    - Ingest one document via HTTP ingest endpoint.
    - Retrieve the chunk_id by calling standard explain.
    - Stub pipeline._graph_store to return a community referencing that chunk_id.
    - Call POST /explain with graph_mode='local'.

    Asserts:
    - response.graph_mode_applied == "local"
    - At least one result has graph_provenance with community_id set.
    - TraversalStep.relationship is None (community mode, not naive).
    """
    _install_spacy_stub(monkeypatch)

    col = "t4-local-s3"
    doc = tmp_path / "payment_service.txt"
    doc.write_text("PaymentService handles all payment transactions securely.\n")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        # Ingest document.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Get real chunk_id via standard explain (no graph_mode).
        chunk_id = _get_chunk_id_from_explain(client, col, api_key)
        assert chunk_id is not None, (
            "Standard explain returned no results; cannot stub community"
        )

        # Set up mock graph store with community data referencing the real chunk_id.
        community = _build_community("comm-payment", ["entity-pay"], [chunk_id])
        matched_node = _build_matched_node("entity-pay", "PaymentService", col)

        pipeline = client.app.state.pipeline
        mock_gs = MagicMock()
        mock_gs.find_nodes_by_name = AsyncMock(return_value=[matched_node])
        mock_gs.communities_table_exists = AsyncMock(return_value=True)
        mock_gs.get_communities_for_entities = AsyncMock(return_value=[community])
        pipeline._graph_store = mock_gs

        # Call POST /explain with graph_mode="local".
        resp = client.post(
            "/explain",
            json={
                "query": "PaymentService",
                "collection": col,
                "graph_mode": "local",
                "top_k": 5,
            },
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, (
            f"POST /explain graph_mode='local' failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()

        # S3: graph_mode_applied must be "local".
        assert body.get("graph_mode_applied") == "local", (
            f"Expected graph_mode_applied='local'; got {body.get('graph_mode_applied')!r}"
        )

        # S3: at least one result must have community_id in TraversalStep.
        results = body.get("results", [])
        graph_results = [r for r in results if r.get("graph_provenance") is not None]
        assert graph_results, (
            f"Expected at least one result with non-null graph_provenance in local mode; "
            f"got zero. Total results: {len(results)}"
        )

        prov = graph_results[0]["graph_provenance"]
        assert prov.get("steps"), f"Expected non-empty steps; got {prov!r}"
        step = prov["steps"][0]
        assert step.get("community_id") is not None, (
            f"TraversalStep must have community_id set in local mode (S3); step={step!r}"
        )
        assert step.get("relationship") is None, (
            f"TraversalStep.relationship should be None in community mode; step={step!r}"
        )


# ---------------------------------------------------------------------------
# (b) test_explain_community_global_mode_provenance_http  (S4)
# ---------------------------------------------------------------------------


def test_explain_community_global_mode_provenance_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain graph_mode='global' → response carries community_id in TraversalStep (S4).

    Setup:
    - Ingest one document via HTTP ingest endpoint.
    - Retrieve the chunk_id by calling standard explain.
    - Stub pipeline._graph_store to return a community via list_community_representatives.
    - Call POST /explain with graph_mode='global'.

    Asserts:
    - response.graph_mode_applied == "global"
    - At least one result has graph_provenance with community_id set.
    - TraversalStep.relationship is None (community mode, not naive).
    """
    _install_spacy_stub(monkeypatch)

    col = "t4-global-s4"
    doc = tmp_path / "order_service.txt"
    doc.write_text("OrderService coordinates order fulfilment across all regions.\n")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        # Ingest document.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Get real chunk_id via standard explain (no graph_mode).
        chunk_id = _get_chunk_id_from_explain(client, col, api_key)
        assert chunk_id is not None, (
            "Standard explain returned no results; cannot stub community"
        )

        # Set up mock graph store for global mode (list_community_representatives).
        community = _build_community("comm-orders", ["entity-orders"], [chunk_id])

        pipeline = client.app.state.pipeline
        mock_gs = MagicMock()
        mock_gs.list_community_representatives = AsyncMock(return_value=[community])
        pipeline._graph_store = mock_gs

        # Call POST /explain with graph_mode="global".
        resp = client.post(
            "/explain",
            json={
                "query": "OrderService",
                "collection": col,
                "graph_mode": "global",
                "top_k": 5,
            },
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, (
            f"POST /explain graph_mode='global' failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()

        # S4: graph_mode_applied must be "global".
        assert body.get("graph_mode_applied") == "global", (
            f"Expected graph_mode_applied='global'; got {body.get('graph_mode_applied')!r}"
        )

        # S4: at least one result must have community_id in TraversalStep.
        results = body.get("results", [])
        graph_results = [r for r in results if r.get("graph_provenance") is not None]
        assert graph_results, (
            f"Expected at least one result with non-null graph_provenance in global mode; "
            f"got zero. Total results: {len(results)}"
        )

        prov = graph_results[0]["graph_provenance"]
        assert prov.get("steps"), f"Expected non-empty steps; got {prov!r}"
        step = prov["steps"][0]
        assert step.get("community_id") is not None, (
            f"TraversalStep must have community_id set in global mode (S4); step={step!r}"
        )
        assert step.get("relationship") is None, (
            f"TraversalStep.relationship should be None in community mode; step={step!r}"
        )


# ---------------------------------------------------------------------------
# (c) test_explain_community_local_table_not_exists_returns_422_http  (S6)
# ---------------------------------------------------------------------------


def test_explain_community_local_table_not_exists_returns_422_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain graph_mode='local' + communities table not built → 422 (S6).

    This tests the natural raise path: _explain_community_candidates raises
    GraphCommunitiesNotBuiltError when communities_table_exists returns False,
    and the route handler catches it and returns 422 with structured detail.
    """
    _install_spacy_stub(monkeypatch)

    col = "t4-local-no-communities"
    doc = tmp_path / "payment_no_comm.txt"
    doc.write_text("PaymentService handles payments.\n")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        # Ingest so the collection exists and explain doesn't 404.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Stub _graph_store: entity matching succeeds, but communities table is absent.
        from archon_search.graph_types import EntityType, GraphNode
        matched_node = GraphNode(
            id="entity-pay",
            entity_name="PaymentService",
            entity_type=EntityType.system,
            source_doc_id="any",
            collection_name=col,
        )
        pipeline = client.app.state.pipeline
        mock_gs = MagicMock()
        mock_gs.find_nodes_by_name = AsyncMock(return_value=[matched_node])
        mock_gs.communities_table_exists = AsyncMock(return_value=False)
        pipeline._graph_store = mock_gs

        resp = client.post(
            "/explain",
            json={"query": "PaymentService", "collection": col, "graph_mode": "local"},
            headers=_auth(api_key),
        )

        assert resp.status_code == 422, (
            f"Expected 422 when communities not built (local mode); "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        detail = body.get("detail")
        assert isinstance(detail, dict), (
            f"Expected structured dict detail with 'code'; got {type(detail).__name__!r}: {detail!r}"
        )
        assert detail.get("code") == "graph_communities_not_built", (
            f"Expected code='graph_communities_not_built'; got: {detail!r}"
        )
