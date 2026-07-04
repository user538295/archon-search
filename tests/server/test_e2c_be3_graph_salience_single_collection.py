"""Tests for BE-3: ?salience= query param and namespace fix for GET /graph/{collection}.

Five tests:
1. test_view_to_response_includes_salience_mode        — unit: _view_to_response propagates salience_mode
2. test_graph_route_salience_frequency_default          — integration: no ?salience= → "frequency" in response
3. test_graph_route_salience_invalid_returns_422        — integration: ?salience=bm25 → 422
4. test_graph_route_salience_tfidf_returns_salience_mode — integration: ?salience=tfidf → "tfidf" in response
5. test_graph_route_cross_namespace_collection_returns_404 — integration: ns-b key, col in DEFAULT → 404
"""
from __future__ import annotations

import secrets
import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app


def _auth(api_key: str) -> dict[str, str]:
    """Return Authorization header dict for a bearer token."""
    return {"Authorization": f"Bearer {api_key}"}


def _register_collection(client, col_path: str, api_key: str) -> None:
    """POST /collections with the given path and assert 202.

    This writes collection metadata synchronously before the background ingest job
    starts, so get_collection_meta() will find the collection immediately after this
    call returns.
    """
    resp = client.post(
        "/collections",
        json={"path": col_path},
        headers=_auth(api_key),
    )
    assert resp.status_code == 202, (
        f"Expected 202 from POST /collections, got {resp.status_code}: {resp.text}"
    )


@pytest.fixture(scope="module", autouse=True)
def _inject_spacy_stub() -> None:
    """Inject a minimal spaCy stub so graph-enabled apps can start without the real package."""
    if "spacy" not in sys.modules:
        sys.modules["spacy"] = types.ModuleType("spacy")


# ---------------------------------------------------------------------------
# Test 1 — unit: _view_to_response propagates salience_mode
# ---------------------------------------------------------------------------


class TestViewToResponseIncludesSalienceMode:
    """Unit: _view_to_response propagates the salience_mode field from the view."""

    def test_view_to_response_includes_salience_mode(self) -> None:
        from archon_search.graph_inspector import CollectionGraphView
        from archon_search.server.routes_graph import _view_to_response

        for mode in ("frequency", "tfidf"):
            view = CollectionGraphView(
                nodes=[],
                edges=[],
                node_count=0,
                edge_count=0,
                truncated=False,
                salience_mode=mode,  # type: ignore[arg-type]
            )
            response = _view_to_response(view)
            assert response.salience_mode == mode, (
                f"Expected salience_mode={mode!r}, got {response.salience_mode!r}"
            )


# ---------------------------------------------------------------------------
# Test 2 — integration: no ?salience= param → salience_mode="frequency" in response
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGraphRouteSalienceFrequencyDefault:
    """Integration: omitting ?salience= yields salience_mode='frequency' in JSON response."""

    def test_graph_route_salience_frequency_default(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        col_dir = tmp_path / "graphcol"
        col_dir.mkdir()
        with make_real_app(
            tmp_path, monkeypatch, graph_enabled=True
        ) as (client, cfg, api_key):
            # Register collection under DEFAULT_NAMESPACE so get_collection_meta finds it
            _register_collection(client, str(col_dir), api_key)

            response = client.get("/graph/graphcol", headers=_auth(api_key))
            assert response.status_code == 200, response.text
            data = response.json()
            assert data.get("salience_mode") == "frequency"


# ---------------------------------------------------------------------------
# Test 3 — integration: ?salience=bm25 (invalid value) → 422
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGraphRouteSalienceInvalidReturns422:
    """Integration: an invalid ?salience= value triggers FastAPI 422 before handler runs."""

    def test_graph_route_salience_invalid_returns_422(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        with make_real_app(
            tmp_path, monkeypatch, graph_enabled=True
        ) as (client, cfg, api_key):
            response = client.get(
                "/graph/some-collection?salience=bm25", headers=_auth(api_key)
            )
            assert response.status_code == 422


# ---------------------------------------------------------------------------
# Test 4 — integration: ?salience=tfidf → salience_mode="tfidf" in response
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGraphRouteSalienceTfidfReturnsMode:
    """Integration: ?salience=tfidf yields salience_mode='tfidf' in JSON response.

    Empty-graph smoke test — registers a collection with no ingested content and
    calls GET /graph/{col}?salience=tfidf. Verifies:
      - the handler does not crash (200 response)
      - salience_mode is forwarded to the response (would fail if hardcoded to "frequency")
      - entity_presence is non-None (inspect_collection raises ValueError + 500 if None)

    The full IDF ranking proof — verifying num_collections wiring and that a
    domain-specific entity outranks an ubiquitous one — is T-1's responsibility
    (test_get_graph_tfidf_reranks_nodes_end_to_end in T-1 tests).
    """

    def test_graph_route_salience_tfidf_returns_salience_mode(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        col_dir = tmp_path / "tfidfcol"
        col_dir.mkdir()
        with make_real_app(
            tmp_path, monkeypatch, graph_enabled=True
        ) as (client, cfg, api_key):
            _register_collection(client, str(col_dir), api_key)

            response = client.get("/graph/tfidfcol?salience=tfidf", headers=_auth(api_key))
            assert response.status_code == 200, response.text
            data = response.json()
            assert data.get("salience_mode") == "tfidf"


# ---------------------------------------------------------------------------
# Test 5 — integration: collection in DEFAULT_NAMESPACE, request auth as ns-b → 404
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGraphRouteCrossNamespaceCollectionReturns404:
    """Integration: collection in DEFAULT_NAMESPACE is invisible to a ns-b-authenticated request.

    Scenario: a collection is registered under DEFAULT_NAMESPACE.
    A second API token resolves to namespace "ns-b".  Calling GET /graph/{col} with
    the ns-b token must return 404 because the handler passes namespace="ns-b" to
    get_collection_meta, which finds no collection in that namespace.
    """

    def test_graph_route_cross_namespace_collection_returns_404(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        col_dir = tmp_path / "nsacol"
        col_dir.mkdir()
        ns_b_key = secrets.token_hex(32)
        with make_real_app(
            tmp_path,
            monkeypatch,
            graph_enabled=True,
            namespaces={ns_b_key: "ns-b"},
        ) as (client, cfg, api_key):
            # Register "nsacol" under DEFAULT_NAMESPACE using the main api_key
            _register_collection(client, str(col_dir), api_key)

            # Collection "nsacol" exists only in DEFAULT_NAMESPACE.
            # Request authenticated as ns-b → get_collection_meta(namespace="ns-b") → None → 404.
            response = client.get("/graph/nsacol", headers=_auth(ns_b_key))
            assert response.status_code == 404, (
                f"Expected 404 (cross-namespace isolation), got {response.status_code}: {response.text}"
            )
