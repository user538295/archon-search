"""Task 1.6 — HyDE and RAG-Fusion HTTP integration.

Exercises the full HTTP layer for:
- HyDE kill-switch (config.enabled=False) suppresses HyDE even when requested
- Multi-collection standard fan-out with rag_fusion=True in body but disabled in config
- HyDE dependency absent returns 4xx with clear message
- RAG Fusion dependency absent returns 4xx with clear message (single-collection path)
- RAG Fusion dependency absent returns 4xx with clear message (multi-collection path)
- BE-1 refactor: HyDE behaviour unchanged after AnthropicQueryExpansionProvider extraction

Run with:
    uv run pytest tests/integration/test_http_hyde_rag_fusion.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


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
    resolve_hyde_vector returns (None, False) because config.enabled is False.
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
# Test 3 — HyDE dependency absent returns 4xx with clear error
# ---------------------------------------------------------------------------

def test_hyde_dependency_absent_returns_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HyDEGenerator._anthropic_available=False simulates missing dependency.

    POST /search with hyde=True must return 422 with an error message that
    references the missing dependency. The route handler converts the RuntimeError
    from HyDEGenerator.generate() into a 422 JSONResponse.

    Note: config.hyde.enabled must be True so resolve_hyde_vector actually
    calls generator.generate(). The generator's _anthropic_available flag
    is patched via monkeypatch.setattr on the app.state instance after startup.
    """
    doc = tmp_path / "hyde_dep_absent.md"
    doc.write_text("# Dependency Test\n\nTest content for HyDE dependency error.\n" * 4)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Enable HyDE so resolve_hyde_vector proceeds to call generator.generate().
        cfg.hyde.enabled = True

        col = "hyde-dep-absent"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Patch via monkeypatch.setattr so any rename of the attribute causes a clear error.
        generator = client.app.state.hyde_generator
        monkeypatch.setattr(generator, "_provider_available", False)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "dependency test", "hyde": True},
            headers=_auth(api_key),
        )
        # The route catches RuntimeError from generate() and returns 422.
        assert resp.status_code == 422, (
            f"expected 422 when anthropic is absent for HyDE, "
            f"got {resp.status_code}: {resp.text}"
        )
        detail = resp.json().get("detail", "")
        # The error message must reference the missing dependency.
        assert "archon-search[hyde]" in detail or "hyde" in detail.lower(), (
            f"expected error message referencing HyDE dependency, got: {detail!r}"
        )


# ---------------------------------------------------------------------------
# Test 4 — RAG Fusion dependency absent returns 4xx (single-collection path)
# ---------------------------------------------------------------------------

def test_rag_fusion_dependency_absent_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAGFusionGenerator._anthropic_available=False simulates missing dep (single-collection).

    POST /search with rag_fusion=True must return 422 with a clear message.
    RAGFusionDependencyError is raised by generate_variants() and caught by
    the route handler (pipeline.search() path), which returns a 422 JSONResponse.

    Note: config.rag_fusion.enabled must be True so the pipeline invokes
    generate_variants(). The generator's _anthropic_available flag is patched
    via monkeypatch.setattr on the app.state instance after startup.
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

        # Patch via monkeypatch.setattr so any rename of the attribute causes a clear error.
        generator = client.app.state.rag_fusion_generator
        monkeypatch.setattr(generator, "_provider_available", False)

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
        detail = resp.json().get("detail", "")
        # The error message must reference the missing dependency.
        assert "rag_fusion" in detail.lower() or "archon-search[rag" in detail, (
            f"expected error message referencing RAG Fusion dependency, got: {detail!r}"
        )


# ---------------------------------------------------------------------------
# Test 5 — RAG Fusion dependency absent returns 4xx (multi-collection path)
# ---------------------------------------------------------------------------

def test_rag_fusion_dependency_absent_returns_error_multi_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAGFusionGenerator._anthropic_available=False simulates missing dep (multi-collection).

    POST /search with rag_fusion=True and collections=[...] must return 422.
    RAGFusionDependencyError is raised by generate_variants() and caught by
    the route handler (pipeline.search_many() path), which returns a 422 JSONResponse.

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

        # Patch via monkeypatch.setattr so any rename of the attribute causes a clear error.
        generator = client.app.state.rag_fusion_generator
        monkeypatch.setattr(generator, "_provider_available", False)

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
        detail = resp.json().get("detail", "")
        # The error message must reference the missing dependency.
        assert "rag_fusion" in detail.lower() or "archon-search[rag" in detail, (
            f"expected error message referencing RAG Fusion dependency, got: {detail!r}"
        )


# ---------------------------------------------------------------------------
# Test 6 — BE-1: HyDE behaviour unchanged after AnthropicQueryExpansionProvider extraction
# ---------------------------------------------------------------------------

def test_hyde_anthropic_behaviour_unchanged_after_refactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BE-1 regression: HyDE returns hyde_applied=True after adapter extraction.

    After the BE-1 refactor, HyDEGenerator delegates text generation to
    AnthropicQueryExpansionProvider.  This test verifies that:
    1. The provider's generate_hypothetical_doc is called and returns text.
    2. HyDEGenerator embeds the text and returns a vector.
    3. resolve_hyde_vector returns hyde_applied=True.
    4. The HTTP response carries hyde_applied=True.

    The provider's _client is replaced with a mock AFTER app startup (inside the
    with-block) so the session fixture's RuntimeError guard for AsyncAnthropic()
    does not interfere — we replace the already-constructed client object.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    doc = tmp_path / "hyde_refactor_test.md"
    doc.write_text(
        "# Hypothetical Document Embeddings\n\n"
        "HyDE generates a passage that would answer the query, then embeds it.\n" * 6
    )

    with make_real_app(tmp_path, monkeypatch, hyde_enabled=True) as (client, cfg, api_key):
        assert cfg.hyde.enabled, "expected hyde.enabled=True for this test"

        col = "hyde-refactor"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Inject a mocked client into the already-constructed provider so
        # generate_hypothetical_doc returns a fixed hypothesis text.
        generator = client.app.state.hyde_generator
        provider = generator._provider

        mock_content = SimpleNamespace(text="A hypothetical passage about HyDE retrieval.")
        mock_response = SimpleNamespace(content=[mock_content])
        mock_messages = MagicMock()
        mock_messages.create = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.messages = mock_messages

        # Replace the provider's _client with our mock.
        monkeypatch.setattr(provider, "_client", mock_client)
        monkeypatch.setattr(provider, "_anthropic_available", True)
        # Set API key so the key guard in the provider passes.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-integration-key")

        resp = client.post(
            "/search",
            json={"collection": col, "query": "what is HyDE", "hyde": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 from HyDE search, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["hyde_applied"] is True, (
            f"expected hyde_applied=True after BE-1 refactor, got: {data['hyde_applied']}"
        )


# ---------------------------------------------------------------------------
# Test 7 — BE-1: RAG Fusion behaviour unchanged after AnthropicQueryExpansionProvider extraction
# ---------------------------------------------------------------------------

def test_rag_fusion_anthropic_behaviour_unchanged_after_refactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BE-1 regression: RAG Fusion returns rag_fusion_applied=True after adapter extraction.

    After the BE-1 refactor, RAGFusionGenerator delegates text generation to
    AnthropicQueryExpansionProvider.  This test verifies that:
    1. The provider's decompose_query is called and returns variants.
    2. RAGFusionGenerator uses the variants for multi-query search.
    3. The HTTP response carries rag_fusion_applied=True.

    The provider's _client is replaced with a mock AFTER app startup (inside the
    with-block) so the session fixture's RuntimeError guard does not interfere.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    doc = tmp_path / "rag_fusion_refactor_test.md"
    doc.write_text(
        "# RAG Fusion Test\n\nRAG Fusion decomposes queries into variants.\n" * 6
    )

    with make_real_app(tmp_path, monkeypatch, rag_fusion_enabled=True) as (client, cfg, api_key):
        col = "rag-fusion-refactor"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        generator = client.app.state.rag_fusion_generator
        provider = generator._provider

        # Return 2 variants from the provider
        mock_content = SimpleNamespace(text="variant one query\nvariant two query")
        mock_response = SimpleNamespace(content=[mock_content])
        mock_messages = MagicMock()
        mock_messages.create = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.messages = mock_messages

        monkeypatch.setattr(provider, "_client", mock_client)
        monkeypatch.setattr(provider, "_anthropic_available", True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-integration-key")

        resp = client.post(
            "/search",
            json={"collection": col, "query": "rag fusion test", "rag_fusion": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 from RAG Fusion search, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["rag_fusion_applied"] is True, (
            f"expected rag_fusion_applied=True after BE-1 refactor, got: {data['rag_fusion_applied']}"
        )
