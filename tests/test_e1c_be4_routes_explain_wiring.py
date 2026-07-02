"""BE-4: Tests for route handler wiring of graph_mode to pipeline.explain() and
graph_mode_applied / hyde_applied in ExplainResponse.

Covers:
- ``explain_endpoint()`` passes ``graph_mode=body.graph_mode`` to single-collection
  ``pipeline.explain()`` call site
- ``ExplainResponse.from_pipeline_result()`` receives
  ``graph_mode_applied=result.graph_mode_applied`` at both call sites
- HyDE invariant: when ``body.graph_mode is not None``, ``hyde_applied=False``
  regardless of what ``resolve_hyde_vector()`` returned (S15)
- ``ExplainNearMiss`` carries no ``graph_provenance`` field (S9)

Scenarios: S1, S9, S13, S15.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.pipeline import ExplainPipelineResult
from archon_search.server.routes_explain import ExplainNearMiss


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a minimal spaCy stub that satisfies _check_graph_deps.

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


def _minimal_pipeline_result(**kwargs) -> ExplainPipelineResult:
    return ExplainPipelineResult(
        top_results=[],
        near_misses=[],
        acl_filtered=False,
        **kwargs,
    )


def _minimal_meta() -> MagicMock:
    meta = MagicMock()
    meta.active_embedding_model = None  # causes route to use config.embedding_model
    return meta


# ---------------------------------------------------------------------------
# Unit test — ExplainNearMiss schema has no graph_provenance field (S9)
# ---------------------------------------------------------------------------


def test_near_miss_no_graph_provenance_field() -> None:
    """ExplainNearMiss schema must NOT have a graph_provenance attribute (S9).

    Near misses carry no provenance — this is a structural omission by design.
    The field must be absent from both model_fields and the class itself.
    """
    assert "graph_provenance" not in ExplainNearMiss.model_fields


# ---------------------------------------------------------------------------
# Unit tests — route handler graph_mode forwarding to pipeline.explain()
# ---------------------------------------------------------------------------


def test_explain_endpoint_graph_mode_none_forwarded(tmp_path, monkeypatch) -> None:
    """body.graph_mode=None → pipeline.explain called with graph_mode=None explicitly;
    response.graph_mode_applied=None.

    When graph_mode is omitted (None), the pipeline is called and the response
    reflects the null mode — identical to pre-E1c behaviour (S1, S13).
    """
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        pipeline = client.app.state.pipeline
        client.app.state.embedder_cache = None  # force _global_embedder path

        pipeline.get_collection_meta = AsyncMock(return_value=_minimal_meta())
        pipeline.explain = AsyncMock(
            return_value=_minimal_pipeline_result(graph_mode_applied=None)
        )

        response = client.post(
            "/explain",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": "test", "collection": "test-col", "graph_mode": None},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["graph_mode_applied"] is None

        # pipeline.explain must have been called with graph_mode=None explicitly —
        # distinguishes "kwarg absent" (pre-BE-4) from "kwarg=None" (post-BE-4).
        pipeline.explain.assert_called_once()
        call_kwargs = pipeline.explain.call_args[1]
        assert "graph_mode" in call_kwargs
        assert call_kwargs["graph_mode"] is None


@pytest.mark.parametrize("graph_mode", ["naive", "local", "global"])
def test_explain_endpoint_graph_mode_forwarded(tmp_path, monkeypatch, graph_mode: str) -> None:
    """body.graph_mode=<mode> → pipeline.explain called with graph_mode=<mode>;
    response.graph_mode_applied=<mode>.

    BE-4's primary contract: the single-collection route call site must forward
    all three valid graph_mode values to pipeline.explain() and must set
    graph_mode_applied from the returned ExplainPipelineResult.

    graph_enabled=True is required so the BE-5 guard (graph_not_enabled) does not
    fire before the mocked pipeline.explain is reached. The spaCy stub must be
    installed before make_real_app because create_app calls _check_graph_deps
    synchronously.
    """
    from tests.integration.conftest import make_real_app

    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        pipeline = client.app.state.pipeline
        client.app.state.embedder_cache = None

        pipeline.get_collection_meta = AsyncMock(return_value=_minimal_meta())
        pipeline.explain = AsyncMock(
            return_value=_minimal_pipeline_result(graph_mode_applied=graph_mode)
        )

        response = client.post(
            "/explain",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": "test", "collection": "test-col", "graph_mode": graph_mode},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["graph_mode_applied"] == graph_mode

        # Verify the exact mode was forwarded to pipeline.explain
        call_kwargs = pipeline.explain.call_args[1]
        assert call_kwargs.get("graph_mode") == graph_mode


# ---------------------------------------------------------------------------
# Integration tests — real app + TestClient response structure
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_explain_route_graph_mode_null_response_structure(tmp_path, monkeypatch) -> None:
    """POST /explain with graph_mode=null → 200; graph_mode_applied=null in response (S1, S13).

    Uses a real app + TestClient with a collection that has ingested data.
    Response structure must contain graph_mode_applied=null and all
    result.graph_provenance=null (null pass-through, no behaviour change for
    non-graph callers).
    """
    from tests.integration.conftest import ingest_file_via_path, make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Create a real collection with ingested data
        txt_file = tmp_path / "doc.txt"
        txt_file.write_text(
            "archon search graph explain provenance test document", encoding="utf-8"
        )
        ingest_file_via_path(
            client, "test-col", str(txt_file), api_key=api_key
        )

        response = client.post(
            "/explain",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": "archon search", "collection": "test-col", "graph_mode": None},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["graph_mode_applied"] is None
        for result in data.get("results", []):
            assert result.get("graph_provenance") is None
        for nm in data.get("near_misses", []):
            # near misses carry no graph_provenance key — absent is correct
            assert "graph_provenance" not in nm


@pytest.mark.integration
def test_explain_route_graph_mode_and_hyde_true_returns_hyde_applied_false(
    tmp_path, monkeypatch
) -> None:
    """graph_mode='naive' + hyde=True → response.hyde_applied=False; graph_mode_applied='naive' (S15).

    When graph_mode is non-null, the route must pass hyde_applied=False to
    ExplainResponse.from_pipeline_result() regardless of what resolve_hyde_vector()
    returned. Additionally, the HyDE vector must be nulled out so it does not
    drive retrieval via query_vector even in the stub path.

    resolve_hyde_vector is patched to return (b"fakevector", True) to simulate
    the case where a non-null HyDE vector was produced; without BE-4's override,
    both hyde_applied=True and query_vector=b"fakevector" would reach the pipeline
    — this test fails before the fix.

    graph_enabled=True is required so the BE-5 guard (graph_not_enabled) does not
    fire before the mocked pipeline.explain is reached.
    """
    from tests.integration.conftest import make_real_app

    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        pipeline = client.app.state.pipeline
        client.app.state.embedder_cache = None

        # Mock pipeline methods so the route returns 200
        pipeline.get_collection_meta = AsyncMock(return_value=_minimal_meta())
        pipeline.explain = AsyncMock(
            return_value=_minimal_pipeline_result(graph_mode_applied="naive")
        )

        # Simulate HyDE returning a non-null vector with hyde_applied=True (would cause
        # misleading response and HyDE-driven retrieval without BE-4's forced override)
        with patch(
            "archon_search.server.routes_explain.resolve_hyde_vector",
            new=AsyncMock(return_value=(b"fakevector", True)),
        ):
            response = client.post(
                "/explain",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "query": "test",
                    "collection": "test-col",
                    "graph_mode": "naive",
                    "hyde": True,
                },
            )

        assert response.status_code == 200
        data = response.json()
        # HyDE invariant: hyde_applied must be False when graph_mode is non-null
        assert data["hyde_applied"] is False
        # graph_mode_applied must reflect what the pipeline returned
        assert data["graph_mode_applied"] == "naive"
        # Vector invariant: pipeline.explain must receive query_vector=None, not the HyDE vector
        call_kwargs = pipeline.explain.call_args
        assert call_kwargs.kwargs.get("query_vector") is None, (
            "pipeline.explain must receive query_vector=None when graph_mode is set; "
            "HyDE vector must be nulled before reaching the pipeline"
        )
