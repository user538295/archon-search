"""T-1 — real, executable e2e tests against a live llama-server (LLCP feature).

These tests are deliberately NOT gated by ``pytest.skip()`` or a dedicated
marker: they make genuine HTTP calls to a live llama-server expected at
``LLAMA_CPP_BASE_URL_DEFAULT`` (``http://localhost:8080``) and run as part of
the default ``uv run pytest`` invocation. When no llama-server is running
there, every test below is EXPECTED to fail loudly — via an uncaught
connection error or an assertion on a response that never arrived — rather
than skip silently. Start a real llama-server serving a model at that address
before running this file to see it pass.

See Documentation/Backlog/llama-cpp-local-provider-tasks.md (T-1) and
Documentation/Backlog/llama-cpp-local-provider-team-plan.md (S1, S2, S3, S4,
S17) for the scenarios this file proves against real infrastructure.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit

from archon_search.config import LLAMA_CPP_BASE_URL_DEFAULT
from archon_search.install.config_writer import WizardFeatures, _apply_wizard_features_to_toml
from archon_search.install.wizard import _fetch_llama_cpp_models, _prompt_llama_cpp_model
from tests.integration.conftest import ingest_file_via_path, install_spacy_stub, make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def test_live_hyde_via_llama_cpp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S1: ``[hyde] provider="llama_cpp"`` against a live llama-server.

    No httpx mocking — ``POST /search hyde=true`` triggers a real
    ``LlamaCppQueryExpansionProvider`` call to
    ``{LLAMA_CPP_BASE_URL_DEFAULT}/v1/chat/completions``. The provider never
    raises on transport failure (S6 fallback contract): when llama-server is
    unreachable it returns ``None`` and ``hyde_applied`` stays ``False``, so
    the assertion below fails loudly and points straight at the missing
    server rather than masking the problem.
    """
    toml_content = '[hyde]\nenabled = true\nprovider = "llama_cpp"\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content, hyde_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        doc = tmp_path / "doc.txt"
        doc.write_text(
            "archon-search combines dense vector retrieval with full text search "
            "for hybrid recall over local document collections.\n" * 3,
            encoding="utf-8",
        )
        ingest_file_via_path(client, "llcp-hyde", str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": "llcp-hyde", "query": "hybrid retrieval", "hyde": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body["hyde_applied"] is True, (
            "hyde_applied was not True — is a llama-server running and reachable at "
            f"{cfg.hyde.llama_cpp_base_url} with a loaded model? body={body!r}"
        )


def test_live_rag_fusion_via_llama_cpp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S2: ``[rag_fusion] provider="llama_cpp"`` against a live llama-server.

    ``POST /search rag_fusion=true`` triggers a real
    ``LlamaCppQueryExpansionProvider.decompose_query`` call. On transport
    failure it returns ``[]`` (never raises), so ``rag_fusion_applied`` stays
    ``False`` — the assertion below fails loudly when llama-server is down.
    """
    toml_content = '[rag_fusion]\nenabled = true\nprovider = "llama_cpp"\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content, rag_fusion_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        doc = tmp_path / "doc.txt"
        doc.write_text(
            "archon-search combines dense vector retrieval with full text search "
            "for hybrid recall over local document collections.\n" * 3,
            encoding="utf-8",
        )
        ingest_file_via_path(client, "llcp-rag-fusion", str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collection": "llcp-rag-fusion",
                "query": "hybrid retrieval example",
                "rag_fusion": True,
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body["rag_fusion_applied"] is True, (
            "rag_fusion_applied was not True — is a llama-server running and reachable at "
            f"{cfg.rag_fusion.llama_cpp_base_url} with a loaded model? body={body!r}"
        )


def test_live_graph_enrichment_via_llama_cpp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S3: ``[graph] provider="llama_cpp"`` + ``extraction_model`` set, against
    a live llama-server.

    Ingest exercises ``GraphExtractor``'s real per-chunk
    ``label_relationships`` call; a direct ``CommunityBuilder.build()`` call
    (using the server's own composition-root enrichment client) exercises the
    real ``summarize_community`` call. Both are try/except-guarded in
    production (S9) and fall back silently on transport failure, so the
    assertions below — not an exception — are what surfaces an unreachable
    llama-server.
    """
    install_spacy_stub(monkeypatch)

    toml_content = (
        "[graph]\nenabled = true\nprovider = \"llama_cpp\"\nextraction_model = \"local-model\"\n"
    )
    col = "llcp-graph-enrichment"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content, graph_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        doc = tmp_path / "entities.txt"
        doc.write_text(
            "Alice and Bob both work at Google on the search infrastructure team.\n",
            encoding="utf-8",
        )
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        enrichment_client = client.app.state.enrichment_client
        assert enrichment_client is not None, "app.state.enrichment_client was not built for provider=llama_cpp"

        async def _build_communities_and_read_edges():
            from archon_search.community_builder import CommunityBuilder
            from archon_search.graph_store import GraphStore
            from archon_search.store import SearchStore

            graph_store = GraphStore(cfg.db_path)
            await graph_store.connect()
            search_store = SearchStore(cfg.db_path)
            await search_store.connect()
            try:
                builder = CommunityBuilder(
                    graph_store,
                    cfg.graph,
                    search_store=search_store,
                    enrichment_client=enrichment_client,
                )
                communities = await builder.build(col, "default")
                edges = await graph_store.get_all_edges(col, ns="default")
                return communities, edges
            finally:
                await graph_store.disconnect()
                await search_store.disconnect()

        communities, edges = asyncio.run(_build_communities_and_read_edges())

        assert communities, "Leiden produced zero communities from a 3-entity graph (Alice, Bob, Google)"
        assert any(c.summary_text for c in communities), (
            "no LLM-generated community summary was produced — is a llama-server running and "
            f"reachable at {cfg.graph.llama_cpp_base_url} with model {cfg.graph.extraction_model!r} "
            f"loaded? communities={communities!r}"
        )

        typed_relationship_types = {"uses", "implements", "depends_on"}
        assert any(e.relationship_type in typed_relationship_types for e in edges), (
            "no typed relationship edges were produced by the live label_relationships call — is "
            f"a llama-server running and reachable at {cfg.graph.llama_cpp_base_url}? "
            f"edge relationship_types={[e.relationship_type for e in edges]!r}"
        )


def test_wizard_v1_models_picker_live() -> None:
    """S4: the wizard's llama-server model picker against a live llama-server.

    ``_fetch_llama_cpp_models`` makes a real ``GET /v1/models`` call (no
    mocking) and swallows every failure into ``[]`` by design (never raises),
    so an unreachable server surfaces as a plain, informative assertion
    failure below rather than an exception. The fetched model is then routed
    through the real ``_prompt_llama_cpp_model`` picker and the real TOML
    writer to confirm the whole wizard chain wires it through to
    ``[graph] provider``/``extraction_model``/``llama_cpp_base_url``.
    """
    models = _fetch_llama_cpp_models(LLAMA_CPP_BASE_URL_DEFAULT)
    assert models, (
        f"the wizard fetched no models from a live llama-server at {LLAMA_CPP_BASE_URL_DEFAULT} — "
        "start llama-server with a loaded model before running this e2e suite"
    )

    with patch("builtins.input", side_effect=["", "1"]):
        base_url, model = _prompt_llama_cpp_model("graph enrichment")
    assert base_url == ""  # "" means "use the built-in default" (config default resolves it)
    assert model == models[0]

    features = WizardFeatures(
        graph_provider="llama_cpp",
        graph_extraction_model=model,
        graph_llama_cpp_base_url=base_url,
    )
    doc = tomlkit.document()
    _apply_wizard_features_to_toml(doc, features)
    assert doc["graph"]["provider"] == "llama_cpp"
    assert doc["graph"]["extraction_model"] == model


def test_status_probe_llama_cpp_reachability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S17/S25: ``GET /status`` reflects a real probe against a live
    llama-server; ``GET /ready`` is never degraded by it (warn-not-block).

    ``model_validation.py``'s background probe makes a real, un-mocked
    ``GET {base_url}/v1/models`` call. It never raises (S7); an unreachable
    server yields ``llama_cpp_ok=False``, which fails the assertion below
    loudly and points at the missing server.
    """
    toml_content = '[hyde]\nprovider = "llama_cpp"\n'
    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and client.app.state.model_validation is None:
            time.sleep(0.05)
        assert client.app.state.model_validation is not None, "model validation probe did not complete in time"

        ready_resp = client.get("/ready")
        assert ready_resp.status_code == 200, f"/ready degraded: {ready_resp.status_code} {ready_resp.text}"

        status_resp = client.get("/status", headers=_auth(api_key))
        assert status_resp.status_code == 200, f"/status failed: {status_resp.status_code} {status_resp.text}"
        body = status_resp.json()
        model_validation = body.get("model_validation") or {}
        assert model_validation.get("llama_cpp_ok") is True, (
            "GET /status did not report llama_cpp_ok=true — is a llama-server running and reachable "
            f"at {cfg.hyde.llama_cpp_base_url}? model_validation={model_validation!r}"
        )
