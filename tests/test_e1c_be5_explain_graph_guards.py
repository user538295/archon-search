"""BE-5: Integration tests for graph error guards in POST /explain.

Guards added in routes_explain.py:
- (1) graph_not_enabled: graph_mode with graph.enabled=False → 422 plain string detail (S5)
- (2) graph_mode_with_collections: graph_mode + collections together → 422 (S14)
- (3) graph_communities_not_built: pipeline raises GraphCommunitiesNotBuiltError → 422
      with structured {"code": "graph_communities_not_built"} body (S6)

The GraphCommunitiesNotBuiltError handler is added to BOTH call sites in
routes_explain.py:
  - multi-collection fanout path (body.collections is not None)
  - single-collection path (body.collection / routing)

Scenarios: S5, S6, S14.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# spaCy stub helpers (required for graph_enabled=True)
# ---------------------------------------------------------------------------


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a minimal spaCy stub that returns no named entities.

    Must be called BEFORE make_real_app when graph_enabled=True;
    create_app calls _check_graph_deps which imports spacy synchronously.
    """

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


# ---------------------------------------------------------------------------
# (1) graph_not_enabled guard — S5
# ---------------------------------------------------------------------------


def test_explain_route_graph_not_enabled_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with graph_mode='naive' + graph disabled in config → 422.

    The route must return a plain string detail (no 'code' field) matching the
    /search pattern exactly: "graph_mode requires [graph] enabled=true in server config".

    graph.enabled=False is the default; no spaCy stub needed.
    (S5)
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        assert not cfg.graph.enabled, "graph must be disabled for this test"

        resp = client.post(
            "/explain",
            json={"query": "test query", "collection": "test-col", "graph_mode": "naive"},
            headers=_auth(api_key),
        )

        assert resp.status_code == 422, (
            f"Expected 422 when graph disabled; got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        detail = body.get("detail")
        assert isinstance(detail, str), (
            f"Expected plain string detail (no 'code' field); got {type(detail).__name__!r}: {detail!r}"
        )
        assert detail == "graph_mode requires [graph] enabled=true in server config", (
            f"Detail string mismatch: {detail!r}"
        )


def test_explain_route_graph_not_enabled_local_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with graph_mode='local' + graph disabled → 422 plain string detail."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        resp = client.post(
            "/explain",
            json={"query": "test query", "collection": "test-col", "graph_mode": "local"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422
        detail = resp.json().get("detail")
        assert isinstance(detail, str)
        assert detail == "graph_mode requires [graph] enabled=true in server config", (
            f"Detail string mismatch: {detail!r}"
        )


def test_explain_route_graph_not_enabled_global_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with graph_mode='global' + graph disabled → 422 plain string detail."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        resp = client.post(
            "/explain",
            json={"query": "test query", "collection": "test-col", "graph_mode": "global"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422
        detail = resp.json().get("detail")
        assert isinstance(detail, str)
        assert detail == "graph_mode requires [graph] enabled=true in server config", (
            f"Detail string mismatch: {detail!r}"
        )


# ---------------------------------------------------------------------------
# (3) graph_communities_not_built guard — S6, single-collection path
# ---------------------------------------------------------------------------


def test_explain_route_communities_not_built_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph_mode='local' + communities not built → 422; body.detail.code == 'graph_communities_not_built'.

    Uses mock pipeline.explain raising GraphCommunitiesNotBuiltError to simulate the
    pipeline guard. Graph must be enabled so the graph_not_enabled guard does not fire first.
    (S6)
    """
    from archon_search.pipeline import GraphCommunitiesNotBuiltError

    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        pipeline = client.app.state.pipeline
        client.app.state.embedder_cache = None

        # Mock meta lookup so the route doesn't 404
        meta = type("Meta", (), {"active_embedding_model": None})()
        pipeline.get_collection_meta = AsyncMock(return_value=meta)
        # Simulate the pipeline raising when communities are absent
        pipeline.explain = AsyncMock(
            side_effect=GraphCommunitiesNotBuiltError("test-col")
        )

        resp = client.post(
            "/explain",
            json={"query": "test query", "collection": "test-col", "graph_mode": "local"},
            headers=_auth(api_key),
        )

        assert resp.status_code == 422, (
            f"Expected 422 when communities not built; got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        detail = body.get("detail")
        assert isinstance(detail, dict), (
            f"Expected structured dict detail with 'code'; got {type(detail).__name__!r}: {detail!r}"
        )
        assert detail.get("code") == "graph_communities_not_built", (
            f"Expected code='graph_communities_not_built'; got: {detail!r}"
        )
        assert "message" in detail, (
            f"Expected 'message' key in error detail; got: {detail!r}"
        )
        assert detail["message"], f"'message' must be non-empty; got: {detail!r}"


def test_explain_route_communities_not_built_global_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph_mode='global' + communities not built → 422; body.detail.code == 'graph_communities_not_built'.

    Same guard, different graph_mode value. Tests the single-collection path. (S6)
    """
    from archon_search.pipeline import GraphCommunitiesNotBuiltError

    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        pipeline = client.app.state.pipeline
        client.app.state.embedder_cache = None

        meta = type("Meta", (), {"active_embedding_model": None})()
        pipeline.get_collection_meta = AsyncMock(return_value=meta)
        pipeline.explain = AsyncMock(
            side_effect=GraphCommunitiesNotBuiltError("test-col")
        )

        resp = client.post(
            "/explain",
            json={"query": "test query", "collection": "test-col", "graph_mode": "global"},
            headers=_auth(api_key),
        )

        assert resp.status_code == 422, (
            f"Expected 422 for global no-communities; got {resp.status_code}: {resp.text}"
        )
        detail = resp.json().get("detail")
        assert isinstance(detail, dict)
        assert detail.get("code") == "graph_communities_not_built"
        assert "message" in detail, (
            f"Expected 'message' key in error detail; got: {detail!r}"
        )
        assert detail["message"], f"'message' must be non-empty; got: {detail!r}"


# ---------------------------------------------------------------------------
# (3) graph_communities_not_built guard — multi-collection fanout path
# ---------------------------------------------------------------------------


def test_explain_route_communities_not_built_multi_collection_fanout_path_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GraphCommunitiesNotBuiltError from the multi-collection fanout pipeline.explain call → 422.

    BE-5 requires handlers at BOTH call sites. This test targets the multi-collection
    fanout path (body.collections is not None). We send collections without graph_mode so
    the S14 guard does not fire, then mock pipeline.explain to raise
    GraphCommunitiesNotBuiltError, verifying the handler at the multi-collection call site.

    No graph_enabled=True needed because no graph_mode is sent.
    """
    from archon_search.pipeline import GraphCommunitiesNotBuiltError

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        pipeline = client.app.state.pipeline
        # Mock pipeline.explain to raise GraphCommunitiesNotBuiltError on the multi-collection path
        pipeline.explain = AsyncMock(
            side_effect=GraphCommunitiesNotBuiltError("test-col")
        )

        resp = client.post(
            "/explain",
            json={
                "query": "test query",
                "collections": ["col-a", "col-b"],
                # no graph_mode: S14 guard does not fire
            },
            headers=_auth(api_key),
        )

        assert resp.status_code == 422, (
            f"Expected 422 for communities not built on multi-collection path; got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        detail = body.get("detail")
        assert isinstance(detail, dict), (
            f"Expected structured dict detail with 'code'; got {type(detail).__name__!r}: {detail!r}"
        )
        assert detail.get("code") == "graph_communities_not_built", (
            f"Expected code='graph_communities_not_built'; got: {detail!r}"
        )
        assert "message" in detail, (
            f"Expected 'message' key in error detail; got: {detail!r}"
        )
        assert detail["message"], f"'message' must be non-empty; got: {detail!r}"


# ---------------------------------------------------------------------------
# (2) graph_mode_with_collections guard — S14
# ---------------------------------------------------------------------------


def test_explain_route_graph_mode_with_collections_rejected_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with graph_mode='naive' + collections=['a','b'] → 422 (S14).

    graph_mode with multi-collection fanout is not supported in E1c.
    graph_enabled=True is required so the graph_not_enabled guard does not fire before
    the S14 guard. The S14 guard must fire and return the specific S14 error message.
    """
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        assert cfg.graph.enabled, "graph must be enabled so S14 guard fires, not graph_not_enabled"

        resp = client.post(
            "/explain",
            json={
                "query": "test query",
                "collections": ["col-a", "col-b"],
                "graph_mode": "naive",
            },
            headers=_auth(api_key),
        )

        assert resp.status_code == 422, (
            f"Expected 422 for graph_mode + multi-collection; got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        detail = body.get("detail")
        assert isinstance(detail, str), (
            f"Expected plain string detail from S14 guard; got {type(detail).__name__!r}: {detail!r}"
        )
        assert detail == "graph_mode is not supported with multi-collection fanout; use a single collection", (
            f"Expected S14-specific error message; got: {detail!r}"
        )


def test_explain_route_graph_mode_with_single_collection_not_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph_mode with single collection (body.collection set) does NOT trigger S14 guard.

    The S14 guard is only for body.collections (multi-collection fanout).
    graph_mode + body.collection (singular) is the normal single-collection graph path.
    With graph disabled, the graph_not_enabled guard fires — but NOT the S14 guard.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        resp = client.post(
            "/explain",
            json={"query": "test query", "collection": "test-col", "graph_mode": "naive"},
            headers=_auth(api_key),
        )

        # 422 is expected here due to graph_not_enabled (graph is disabled by default),
        # NOT due to the S14 multi-collection guard. If S14 fired incorrectly,
        # the error detail might differ. We just confirm it's still 422 (graph guard).
        assert resp.status_code == 422
        detail = resp.json().get("detail")
        # The graph_not_enabled guard (plain string) should have fired, not S14
        assert isinstance(detail, str), (
            f"Expected plain string from graph_not_enabled guard; got {type(detail).__name__!r}: {detail!r}"
        )
        assert "graph_mode requires" in detail, (
            f"Expected graph_not_enabled error detail; got: {detail!r}"
        )
