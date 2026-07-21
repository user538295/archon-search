"""Integration tests for acl_gate on POST /explain (g15 BE-7).

Covers:
- S6: acl_gate is always present on ExplainResult with no flag required
- S6: ExplainNearMiss does not carry acl_gate
- S7a: chunks the caller cannot see are absent from ExplainResult and ExplainNearMiss
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.pipeline import ExplainPipelineResult

from tests.integration.conftest import make_real_app, make_real_pipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _score_breakdown() -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=1,
        vector_score=0.74,
        vector_score_kind="distance",
        fts_rank=2,
        fts_score=3.1,
        fts_score_kind="bm25",
        rrf_score=0.04,
        reranker_score=None,
    )


def _make_candidate(
    doc_id: str = "doc1",
    *,
    acl: list[str] | None = None,
    acl_source: str | None = None,
    acl_sidecar_path: str | None = None,
    acl_warning: list[str] | None = None,
) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text="chunk text",
        source_path=f"/tmp/{doc_id}.md",
        score_breakdown=_score_breakdown(),
        collection="docs",
        acl=acl,
        acl_source=acl_source,
        acl_sidecar_path=acl_sidecar_path,
        acl_warning=acl_warning or [],
    )


# ---------------------------------------------------------------------------
# S6: acl_gate always present on ExplainResult; absent from ExplainNearMiss
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_explain_acl_gate_unconditional(tmp_path, monkeypatch) -> None:
    """POST /explain returns acl_gate on every ExplainResult with no flag required (S6, C2).

    ExplainNearMiss items must not carry acl_gate.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        top_candidate = _make_candidate(
            "top1",
            acl=["default"],
            acl_source="frontmatter",
            acl_sidecar_path=None,
            acl_warning=[],
        )
        near_candidate = _make_candidate(
            "near1",
            acl=None,
            acl_source="collection_default",
            acl_sidecar_path=None,
            acl_warning=[],
        )
        pipeline_result = ExplainPipelineResult(
            top_results=[top_candidate],
            near_misses=[near_candidate],
            acl_filtered=False,
        )

        mock_pipeline = MagicMock()
        mock_pipeline.get_collection_meta = AsyncMock(
            return_value=MagicMock(active_embedding_model=cfg.embedding_model)
        )
        mock_pipeline.explain = AsyncMock(return_value=pipeline_result)
        client.app.state.pipeline = mock_pipeline

        resp = client.post(
            "/explain",
            json={"collection": "docs", "query": "hello"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Every ExplainResult must carry acl_gate
        assert data["results"], "expected at least one result"
        for r in data["results"]:
            assert "acl_gate" in r, "acl_gate key missing from ExplainResult"
            gate = r["acl_gate"]
            assert gate is not None, "acl_gate must not be null on ExplainResult"
            assert "allowed_principals" in gate
            assert "source" in gate
            assert "sidecar_path" in gate
            assert "warnings" in gate
            assert isinstance(gate["warnings"], list), "warnings must be a list"

        # ExplainNearMiss must not carry acl_gate
        for nm in data["near_misses"]:
            assert "acl_gate" not in nm, (
                "ExplainNearMiss must not carry acl_gate (near-misses are ranking rejects)"
            )


@pytest.mark.integration
def test_explain_acl_gate_all_field_values(tmp_path, monkeypatch) -> None:
    """acl_gate fields match candidate provenance on ExplainResult (S6, C3)."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        candidate = _make_candidate(
            "doc-sidecar",
            acl=["ns-a", "ns-b"],
            acl_source="sidecar",
            acl_sidecar_path="docs/doc-sidecar.md.acl",
            acl_warning=["truncation warning"],
        )
        pipeline_result = ExplainPipelineResult(
            top_results=[candidate],
            near_misses=[],
            acl_filtered=False,
        )

        mock_pipeline = MagicMock()
        mock_pipeline.get_collection_meta = AsyncMock(
            return_value=MagicMock(active_embedding_model=cfg.embedding_model)
        )
        mock_pipeline.explain = AsyncMock(return_value=pipeline_result)
        client.app.state.pipeline = mock_pipeline

        resp = client.post(
            "/explain",
            json={"collection": "docs", "query": "hello"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["results"], "expected at least one result"
        gate = data["results"][0]["acl_gate"]

        assert gate["allowed_principals"] == ["ns-a", "ns-b"]
        assert gate["source"] == "sidecar"
        assert gate["sidecar_path"] == "docs/doc-sidecar.md.acl"
        assert gate["warnings"] == ["truncation warning"]


# ---------------------------------------------------------------------------
# S7a: excluded chunks absent from ExplainResult and ExplainNearMiss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_explain_excluded_chunks_absent(tmp_path, monkeypatch) -> None:
    """S7a: chunks the caller's namespace cannot see are absent from POST /explain results.

    Uses a real SearchPipeline so that apply_acl_filter is exercised.
    Ingests a document restricted to namespace "ns-only", then calls explain
    as namespace "default" — the document must not appear in results or near_misses.
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    collection = "col_s7a_explain_acl"
    embedding_dim = 4
    await store.ensure_collection(collection, embedding_dim)

    try:
        # Write a document restricted to namespace "ns-only" via frontmatter ACL
        doc = tmp_path / "restricted_explain.md"
        doc.write_text(
            "---\n_acl:\n  - ns-only\n---\nThis content is restricted to ns-only.\n",
            encoding="utf-8",
        )

        ingest_result = await pipeline.ingest_file(
            doc,
            collection,
            embedder=pipeline._global_embedder,
        )
        assert ingest_result.status == "ok", f"Ingest failed: {ingest_result}"
        assert ingest_result.chunks_created > 0

        # Call explain as "default" namespace — ACL restricts to "ns-only"
        explain_result = await pipeline.explain(
            "restricted content",
            collection,
            namespace="default",
            embedder=pipeline._global_embedder,
        )

        # Both top_results and near_misses must exclude the restricted chunk
        all_doc_ids = [c.doc_id for c in explain_result.top_results + explain_result.near_misses]
        assert not explain_result.top_results, (
            f"ExplainResult must be empty for namespace 'default' (ACL restricts to 'ns-only'), "
            f"got: {all_doc_ids!r}"
        )
        assert not explain_result.near_misses, (
            f"near_misses must also be empty for namespace 'default' (ACL restricts to 'ns-only'), "
            f"got: {[c.doc_id for c in explain_result.near_misses]!r}"
        )
        assert explain_result.acl_filtered is True, (
            "acl_filtered must be True when chunks were dropped by ACL"
        )
    finally:
        await store.disconnect()
