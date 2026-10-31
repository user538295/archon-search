"""Task 1.6 — HyDE and RAG-Fusion HTTP integration.

Exercises the full HTTP layer for:
- HyDE kill-switch (config.enabled=False) suppresses HyDE even when requested
- Multi-collection standard fan-out with rag_fusion=True in body but disabled in config
- HyDE dependency absent returns 4xx with the fixed dependency detail
- RAG Fusion dependency absent returns 4xx with the fixed dependency detail (single-collection path)
- RAG Fusion dependency absent returns 4xx with the fixed dependency detail (multi-collection path)
- BE-11: HyDE through the shared LLM client (``app.state.hyde_client``), success and provider-down fallback
- BE-12: RAG Fusion provider-down fallback through the shared client (``app.state.rag_fusion_client``)

Run with:
    uv run pytest tests/integration/test_http_hyde_rag_fusion.py -v
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from archon_search.hyde import HYDE_DEPENDENCY_DETAIL, HyDEDependencyError
from archon_search.rag_fusion import (
    RAG_FUSION_DEPENDENCY_DETAIL,
    RAGFusionDependencyError,
)
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

# anthropic is stubbed for all integration tests by the autouse fixture in
# tests/integration/conftest.py (create_app's provider guard imports it).


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Test 1 — HyDE kill-switch returns hyde_applied=false
# ---------------------------------------------------------------------------

def test_hyde_kill_switch_returns_hyde_applied_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.hyde.enabled=False (kill-switch) suppresses HyDE even when requested.

    POST /search with hyde=True while config.hyde.enabled=False.
    RequestHyDE.resolve applies nothing because config.enabled is False.
    The response must carry hyde_applied=False and a non-empty results list.
    """
    doc = tmp_path / "hyde_test.md"
    doc.write_text("# HyDE Test\n\nHypothetical document embedding test content.\n" * 4)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Default: config.hyde.enabled is False — the kill-switch is active.
        assert not cfg.hyde.enabled, "expected hyde.enabled=False by default"

        col = "hyde-killswitch"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "hypothetical document", "hyde": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 with hyde kill-switch, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["results"], "expected non-empty results from kill-switch search"
        assert data["hyde_applied"] is False, (
            f"expected hyde_applied=false when kill-switch is active, got: {data['hyde_applied']}"
        )


# ---------------------------------------------------------------------------
# Test 2 — Multi-collection standard fan-out with rag_fusion=True in body
# ---------------------------------------------------------------------------

def test_multi_collection_fan_out_with_rag_fusion_body_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search {rag_fusion: true, collections: ['a','b']} returns 200.

    With config.rag_fusion.enabled=False (default kill-switch), the pipeline
    ignores the rag_fusion request flag and uses the standard fan-out path.
    Both collections must contribute results; rag_fusion_applied must be False.

    Note: each test document is small enough to produce exactly one chunk,
    guaranteeing one result per collection in the top-5 return set.
    """
    doc_a = tmp_path / "rf_corpus_a.md"
    doc_a.write_text(
        "# Retrieval Augmented Generation\n\nRAG Fusion corpus alpha document.\n" * 6
    )
    doc_b = tmp_path / "rf_corpus_b.md"
    doc_b.write_text(
        "# Information Retrieval\n\nRAG Fusion corpus beta document.\n" * 6
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # rag_fusion.enabled stays False (default) — the standard fan-out path is used.
        assert not cfg.rag_fusion.enabled, "expected rag_fusion.enabled=False by default"

        col_a = "rf-alpha"
        col_b = "rf-beta"

        ingest_file_via_path(client, col_a, str(doc_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc_b), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "retrieval augmented generation corpus",
                "rag_fusion": True,
                "top_k": 10,
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 from multi-collection search with rag_fusion=True, "
            f"got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        items = data["results"]
        assert items, "expected non-empty results from multi-collection search"

        # rag_fusion_applied must be False — config.rag_fusion.enabled=False is the kill-switch.
        assert data["rag_fusion_applied"] is False, (
            f"expected rag_fusion_applied=False when kill-switch active, got: {data['rag_fusion_applied']}"
        )

        # Results from both collections must appear (one chunk per doc, so both fit in top-5).
        seen_collections = {item["collection"] for item in items}
        assert col_a in seen_collections, (
            f"collection '{col_a}' absent from results; seen: {seen_collections}"
        )
        assert col_b in seen_collections, (
            f"collection '{col_b}' absent from results; seen: {seen_collections}"
        )

        # No duplicate chunk_id across the merged result set.
        chunk_ids = [item["chunk_id"] for item in items]
        assert len(chunk_ids) == len(set(chunk_ids)), (
            f"duplicate chunk_id found in merged results: {chunk_ids}"
        )


# ---------------------------------------------------------------------------
# Test 3 — HyDE dependency error maps to a 4xx with the fixed detail
# ---------------------------------------------------------------------------

def test_hyde_dependency_absent_returns_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``HyDEDependencyError`` out of the generator is answered 422 with the fixed ``HYDE_DEPENDENCY_DETAIL``.

    The shared-client ``HyDEGenerator`` reports a missing provider package as a call failure (a fallback), so this
    test serves the error through a generator stub: it pins the route's mapping of the exception, which BE-13
    decides whether to keep. ``config.hyde.enabled`` must be True so ``RequestHyDE.resolve`` calls the generator.
    """
    from tests._hyde_stubs import rest_hyde_generating

    doc = tmp_path / "hyde_dep_absent.md"
    doc.write_text("# Dependency Test\n\nTest content for HyDE dependency error.\n" * 4)

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        col = "hyde-dep-absent"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        with rest_hyde_generating(client.app, side_effect=HyDEDependencyError("hyde extra missing")):
            resp = client.post(
                "/search",
                json={"collection": col, "query": "dependency test", "hyde": True},
                headers=_auth(api_key),
            )
        assert resp.status_code == 422, (
            f"expected 422 when anthropic is absent for HyDE, "
            f"got {resp.status_code}: {resp.text}"
        )
        # The fixed detail names the extra; the install command goes to the server log only.
        assert resp.json() == {"detail": HYDE_DEPENDENCY_DETAIL}


# ---------------------------------------------------------------------------
# Test 4 — RAG Fusion dependency absent returns 4xx (single-collection path)
# ---------------------------------------------------------------------------

def test_rag_fusion_dependency_absent_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``RAGFusionDependencyError`` out of the generator (single-collection) is answered 422 with the fixed detail.

    POST /search with rag_fusion=True must return 422 with a clear message.
    The error is caught by the route handler (pipeline.search() path), which returns a 422 JSONResponse.

    Note: config.rag_fusion.enabled must be True so the pipeline invokes generate_variants().
    """
    doc = tmp_path / "rag_dep_absent.md"
    doc.write_text(
        "# RAG Fusion Dependency Test\n\nTest content for RAG Fusion dependency error.\n" * 4
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Enable RAG Fusion so the pipeline invokes generate_variants().
        cfg.rag_fusion.enabled = True

        col = "rag-dep-absent"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # The shared-client generator reports a missing package as a call failure, so serve the error through the
        # generator: this pins the route's mapping of the exception (BE-13 decides whether it stays reachable).
        generator = client.app.state.rag_fusion_generator
        monkeypatch.setattr(
            generator, "generate_variants", AsyncMock(side_effect=RAGFusionDependencyError("rag_fusion extra missing"))
        )

        resp = client.post(
            "/search",
            json={"collection": col, "query": "dependency test", "rag_fusion": True},
            headers=_auth(api_key),
        )
        # The route catches RAGFusionDependencyError and returns 422.
        assert resp.status_code == 422, (
            f"expected 422 when anthropic is absent for RAG Fusion, "
            f"got {resp.status_code}: {resp.text}"
        )
        # The fixed detail names the extra; the install command goes to the server log only.
        assert resp.json() == {"detail": RAG_FUSION_DEPENDENCY_DETAIL}


# ---------------------------------------------------------------------------
# Test 5 — RAG Fusion dependency absent returns 4xx (multi-collection path)
# ---------------------------------------------------------------------------

def test_rag_fusion_dependency_absent_returns_error_multi_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``RAGFusionDependencyError`` out of the generator on the multi-collection path is answered 422.

    POST /search with rag_fusion=True and collections=[...] must return 422 with the fixed detail.
    The error is caught by the route handler (pipeline.search_many() path), which returns a 422 JSONResponse.

    This covers the search_many() code path separately from the single-collection
    search() path tested in test_rag_fusion_dependency_absent_returns_error.
    """
    doc_a = tmp_path / "rag_mc_dep_a.md"
    doc_a.write_text(
        "# RAG Fusion Multi-Collection Dep Test A\n\nAlpha content.\n" * 4
    )
    doc_b = tmp_path / "rag_mc_dep_b.md"
    doc_b.write_text(
        "# RAG Fusion Multi-Collection Dep Test B\n\nBeta content.\n" * 4
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Enable RAG Fusion so search_many() invokes generate_variants().
        cfg.rag_fusion.enabled = True

        col_a = "rag-mc-dep-a"
        col_b = "rag-mc-dep-b"
        ingest_file_via_path(client, col_a, str(doc_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc_b), api_key=api_key)

        # The shared-client generator reports a missing package as a call failure, so serve the error through the
        # generator: this pins the transport's mapping of the exception (BE-13 decides whether it stays reachable).
        generator = client.app.state.rag_fusion_generator
        monkeypatch.setattr(
            generator, "generate_variants", AsyncMock(side_effect=RAGFusionDependencyError("rag_fusion extra missing"))
        )

        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "dependency test multi-collection",
                "rag_fusion": True,
            },
            headers=_auth(api_key),
        )
        # The route catches RAGFusionDependencyError from search_many() and returns 422.
        assert resp.status_code == 422, (
            f"expected 422 when anthropic is absent for RAG Fusion (multi-collection), "
            f"got {resp.status_code}: {resp.text}"
        )
        # The fixed detail names the extra; the install command goes to the server log only.
        assert resp.json() == {"detail": RAG_FUSION_DEPENDENCY_DETAIL}


# ---------------------------------------------------------------------------
# Test 6 — BE-11: HyDE through the shared LLM client, over HTTP
# ---------------------------------------------------------------------------

_HYDE_MODEL = "hyde-model"
_HYPOTHETICAL_DOCUMENT = "A hypothetical passage about HyDE retrieval."
_HYDE_QUERY = "what is HyDE"


def _hyde_on_fake_ollama_toml(base_url: str) -> str:
    return (
        "[hyde]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_HYDE_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        "ollama_context_tokens = 8192\n"
    )


def test_hyde_search_through_shared_client_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A HyDE search answers ``hyde_applied: true`` with the hypothetical document the shared client returned.

    The real app composes ``app.state.hyde_client`` from ``[hyde]`` and hands it to the real ``HyDEGenerator``; the
    loopback fake Ollama server sees exactly one chat call carrying the HyDE template around the query, the
    configured ``num_ctx`` and the 200-token answer ceiling.
    """
    from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies

    keep_loopback_out_of_proxies(monkeypatch)
    doc = tmp_path / "hyde_shared_client.md"
    doc.write_text(
        "# Hypothetical Document Embeddings\n\n"
        "HyDE generates a passage that would answer the query, then embeds it.\n" * 6
    )

    with FakeModelServer(reply_text=_HYPOTHETICAL_DOCUMENT) as server, make_real_app(
        tmp_path, monkeypatch, toml_content=_hyde_on_fake_ollama_toml(server.base_url)
    ) as (client, cfg, api_key):
        assert cfg.hyde.enabled
        col = "hyde-shared-client"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": _HYDE_QUERY, "hyde": True},
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, f"expected 200 from HyDE search, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["hyde_applied"] is True
        assert data["results"], "the search must answer with the hypothesis vector"
        [chat] = server.requests_to("/api/chat")
        assert chat.body is not None
        [message] = chat.body["messages"]
        assert message["role"] == "user"
        assert "---\nwhat is HyDE\n---" in message["content"]
        assert chat.body["options"]["num_ctx"] == 8192
        assert chat.body["options"]["num_predict"] == 200


def test_hyde_provider_down_over_http_falls_back_to_the_plain_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing HyDE provider leaves the search answering with ``hyde_applied: false`` and one query-free WARNING."""
    import logging

    from tests._fake_model_server import keep_loopback_out_of_proxies, unreachable_port

    keep_loopback_out_of_proxies(monkeypatch)
    doc = tmp_path / "hyde_provider_down.md"
    doc.write_text("# Fallback\n\nThe search must still answer when HyDE cannot.\n" * 6)

    with unreachable_port() as down_port, make_real_app(
        tmp_path, monkeypatch, toml_content=_hyde_on_fake_ollama_toml(f"http://127.0.0.1:{down_port}")
    ) as (client, _cfg, api_key), caplog.at_level(logging.INFO):
        col = "hyde-provider-down"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)
        responses = [
            client.post(
                "/search",
                json={"collection": col, "query": f"{_HYDE_QUERY} {attempt}", "hyde": True},
                headers=_auth(api_key),
            )
            for attempt in range(3)
        ]

    for resp in responses:
        assert resp.status_code == 200, resp.text
        assert resp.json()["hyde_applied"] is False
        assert resp.json()["results"]
    hyde_warnings = [
        r for r in caplog.records if r.levelno >= logging.WARNING and r.name == "archon_search.hyde"
    ]
    assert len(hyde_warnings) == 1
    assert _HYDE_QUERY not in " ".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 7 — BE-12: RAG Fusion provider down, over HTTP
# ---------------------------------------------------------------------------

_RAG_FUSION_QUERY = "decompose this unique rag question"


def test_rag_fusion_provider_down_over_http_falls_back_to_the_single_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing RAG Fusion provider leaves the search answering with ``rag_fusion_applied: false`` and one query-free WARNING."""
    import logging

    from tests._fake_model_server import keep_loopback_out_of_proxies, unreachable_port

    keep_loopback_out_of_proxies(monkeypatch)
    doc = tmp_path / "rag_fusion_provider_down.md"
    doc.write_text("# Fallback\n\nThe search must still answer when RAG Fusion cannot.\n" * 6)
    toml = (
        "[rag_fusion]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        'model = "rag-model"\n'
    )

    with unreachable_port() as down_port, make_real_app(
        tmp_path, monkeypatch, toml_content=toml + f'ollama_base_url = "http://127.0.0.1:{down_port}"\n'
    ) as (client, _cfg, api_key), caplog.at_level(logging.INFO):
        col = "rag-fusion-provider-down"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)
        responses = [
            client.post(
                "/search",
                json={"collection": col, "query": f"{_RAG_FUSION_QUERY} {attempt}", "rag_fusion": True},
                headers=_auth(api_key),
            )
            for attempt in range(3)
        ]

    for resp in responses:
        assert resp.status_code == 200, resp.text
        assert resp.json()["rag_fusion_applied"] is False
        assert resp.json()["rag_fusion_attempted"] is True
        assert resp.json()["results"]
    rag_warnings = [
        r for r in caplog.records if r.levelno >= logging.WARNING and r.name == "archon_search.rag_fusion"
    ]
    assert len(rag_warnings) == 1
    assert _RAG_FUSION_QUERY not in " ".join(r.getMessage() for r in caplog.records)
