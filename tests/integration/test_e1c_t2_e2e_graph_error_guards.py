"""E1c / T-2 — e2e tests: graph error guard scenarios.

Covers:
- (a) POST /explain with graph_mode="naive" + graph disabled → 422; body.detail
      is a plain string "graph_mode requires [graph] enabled=true in server config"
      (S5)
- (b) POST /explain with graph_mode="local" + communities not built → 422;
      body.detail is a structured dict with code="graph_communities_not_built"
      (S6; both graph_mode="local" and graph_mode="global" are tested)

Both tests exercise the full HTTP request path via a real FastAPI TestClient
backed by a real SearchStore+SearchPipeline (make_real_app).

For (b) the pipeline's explain method is patched to raise
GraphCommunitiesNotBuiltError — the actual community detection wiring is
deferred to BE-7/BE-8, so the natural raise does not occur in the null
pass-through path.  The route-layer exception handler (BE-5) is what is
under test here.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
# (a) test_explain_graph_not_enabled_e2e — S5
# ---------------------------------------------------------------------------


def test_explain_graph_not_enabled_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with graph_mode='naive' on a server with graph disabled → 422.

    The route-layer guard (BE-5) must fire before the pipeline is called and
    return a 422 with a plain string detail — no structured ``code`` field.

    graph.enabled=False is the default; no spaCy stub is needed.
    (S5)
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        assert not cfg.graph.enabled, "graph must be disabled for this test"

        resp = client.post(
            "/explain",
            json={"query": "graph error guard test", "collection": "t2-col", "graph_mode": "naive"},
            headers=_auth(api_key),
        )

        assert resp.status_code == 422, (
            f"Expected 422 when graph disabled; got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        detail = body.get("detail")
        assert isinstance(detail, str), (
            f"Expected plain string detail (no 'code' field); "
            f"got {type(detail).__name__!r}: {detail!r}"
        )
        # Exact string mandated by S5 spec — this is a contract assertion, not a fragile substring test.
        assert detail == "graph_mode requires [graph] enabled=true in server config", (
            f"Detail string mismatch: {detail!r}"
        )


# ---------------------------------------------------------------------------
# (b) test_explain_communities_not_built_e2e — S6
# ---------------------------------------------------------------------------


def test_explain_communities_not_built_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route-handler integration test: graph_mode='local' + communities not built → 422.

    This is a route-layer integration test (not a fully end-to-end test with
    real pipeline behavior).  ``pipeline.explain`` is mocked to raise
    ``GraphCommunitiesNotBuiltError``; this tests the route-layer exception
    handler (BE-5), not the pipeline's community-detection path (which requires
    BE-7/BE-8 wiring to naturally raise this error).

    Graph is enabled so the S5 guard does not fire first.  The handler must
    return a structured 422 with ``code="graph_communities_not_built"``.

    Both graph_mode='local' and graph_mode='global' trigger the same guard;
    this test exercises graph_mode='local' (see test_explain_communities_not_built_global_e2e
    for the global variant).
    (S6)
    """
    from archon_search.pipeline import GraphCommunitiesNotBuiltError

    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        assert cfg.graph.enabled, "graph must be enabled so the S6 guard fires, not S5"

        pipeline = client.app.state.pipeline
        # Mock the collection metadata lookup so the route does not 404 on
        # an unknown collection before reaching the pipeline.explain call.
        meta = MagicMock(active_embedding_model=None)
        pipeline.get_collection_meta = AsyncMock(return_value=meta)
        # Simulate the pipeline detecting that communities are absent.
        pipeline.explain = AsyncMock(
            side_effect=GraphCommunitiesNotBuiltError("t2-col")
        )

        resp = client.post(
            "/explain",
            json={"query": "graph error guard test", "collection": "t2-col", "graph_mode": "local"},
            headers=_auth(api_key),
        )

        assert resp.status_code == 422, (
            f"Expected 422 when communities not built; got {resp.status_code}: {resp.text}"
        )
        pipeline.explain.assert_called_once()
        body = resp.json()
        detail = body.get("detail")
        assert isinstance(detail, dict), (
            f"Expected structured dict detail with 'code'; "
            f"got {type(detail).__name__!r}: {detail!r}"
        )
        assert detail.get("code") == "graph_communities_not_built", (
            f"Expected code='graph_communities_not_built'; got: {detail!r}"
        )
        assert "message" in detail, (
            f"Expected 'message' key in structured detail; got: {detail!r}"
        )
        assert detail["message"], (
            f"'message' must be non-empty; got: {detail!r}"
        )


def test_explain_communities_not_built_global_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route-handler integration test: graph_mode='global' + communities not built → 422.

    This is a route-layer integration test (not a fully end-to-end test with
    real pipeline behavior).  ``pipeline.explain`` is mocked to raise
    ``GraphCommunitiesNotBuiltError``; this tests the route-layer exception
    handler (BE-5), not the pipeline's community-detection path (which requires
    BE-7/BE-8 wiring to naturally raise this error).

    Graph is enabled so the S5 guard does not fire first.  The handler must
    return a structured 422 with ``code="graph_communities_not_built"``.

    This is the global-mode counterpart of test_explain_communities_not_built_e2e;
    together they satisfy S6's "(both tested)" requirement.
    (S6)
    """
    from archon_search.pipeline import GraphCommunitiesNotBuiltError

    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        assert cfg.graph.enabled, "graph must be enabled so the S6 guard fires, not S5"

        pipeline = client.app.state.pipeline
        # Mock the collection metadata lookup so the route does not 404 on
        # an unknown collection before reaching the pipeline.explain call.
        meta = MagicMock(active_embedding_model=None)
        pipeline.get_collection_meta = AsyncMock(return_value=meta)
        # Simulate the pipeline detecting that communities are absent.
        pipeline.explain = AsyncMock(
            side_effect=GraphCommunitiesNotBuiltError("t2-global-col")
        )

        resp = client.post(
            "/explain",
            json={"query": "graph error guard test", "collection": "t2-global-col", "graph_mode": "global"},
            headers=_auth(api_key),
        )

        assert resp.status_code == 422, (
            f"Expected 422 when communities not built; got {resp.status_code}: {resp.text}"
        )
        pipeline.explain.assert_called_once()
        body = resp.json()
        detail = body.get("detail")
        assert isinstance(detail, dict), (
            f"Expected structured dict detail with 'code'; "
            f"got {type(detail).__name__!r}: {detail!r}"
        )
        assert detail.get("code") == "graph_communities_not_built", (
            f"Expected code='graph_communities_not_built'; got: {detail!r}"
        )
        assert "message" in detail, (
            f"Expected 'message' key in structured detail; got: {detail!r}"
        )
        assert detail["message"], (
            f"'message' must be non-empty; got: {detail!r}"
        )
