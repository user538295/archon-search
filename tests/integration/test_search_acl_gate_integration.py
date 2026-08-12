"""Integration tests for acl_context / acl_gate on POST /search (g15 BE-5)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import SearchResult
from archon_search.pipeline import SearchPipelineResult

from tests.integration.conftest import make_real_app, make_real_pipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    *,
    acl: list[str] | None = None,
    acl_source: str | None = None,
    acl_sidecar_path: str | None = None,
    acl_warning: list[str] | None = None,
) -> SearchResult:
    return SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="integration test chunk",
        score=0.9,
        source_path="/path/to/doc.md",
        acl=acl,
        acl_source=acl_source,
        acl_sidecar_path=acl_sidecar_path,
        acl_warning=acl_warning or [],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_search_acl_context_false_no_gate(tmp_path, monkeypatch) -> None:
    """POST /search without acl_context returns results without acl_gate (S5 / C1)."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        result = _make_result(acl=["default"], acl_source="frontmatter")
        mock_pipeline = MagicMock()
        mock_pipeline.get_collection_meta = AsyncMock(
            return_value=MagicMock(active_embedding_model=cfg.embedding_model)
        )
        mock_pipeline.search = AsyncMock(
            return_value=SearchPipelineResult(results=[result], acl_filtered=False)
        )
        mock_pipeline.warmup_models = AsyncMock()
        client.app.state.pipeline = mock_pipeline

        resp = client.post(
            "/search",
            json={"collection": "docs", "query": "hello"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["results"], "expected at least one result"
        for r in data["results"]:
            assert r.get("acl_gate") is None, (
                "acl_gate must be null when acl_context is False (C1: backward-compatible additive)"
            )


@pytest.mark.integration
def test_search_acl_context_true_has_gate(tmp_path, monkeypatch) -> None:
    """POST /search with acl_context=true returns acl_gate on every result (S13, C3)."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        result = _make_result(
            acl=["ns-a", "ns-b"],
            acl_source="sidecar",
            acl_sidecar_path="/path/to/file.acl",
            acl_warning=["sidecar warning"],
        )
        mock_pipeline = MagicMock()
        mock_pipeline.get_collection_meta = AsyncMock(
            return_value=MagicMock(active_embedding_model=cfg.embedding_model)
        )
        mock_pipeline.search = AsyncMock(
            return_value=SearchPipelineResult(results=[result], acl_filtered=False)
        )
        mock_pipeline.warmup_models = AsyncMock()
        client.app.state.pipeline = mock_pipeline

        resp = client.post(
            "/search",
            json={"collection": "docs", "query": "hello", "acl_context": True},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["results"], "expected at least one result"
        for r in data["results"]:
            assert "acl_gate" in r, "acl_gate key must be present"
            gate = r["acl_gate"]
            assert gate is not None, "acl_gate must not be null when acl_context=true"
            assert "allowed_principals" in gate
            assert "source" in gate
            assert "sidecar_path" in gate
            assert "warnings" in gate
            # C3: warnings is always a list, never null
            assert isinstance(gate["warnings"], list)
            assert gate["allowed_principals"] == ["ns-a", "ns-b"]
            assert gate["source"] == "sidecar"
            assert gate["sidecar_path"] == "/path/to/file.acl"
            assert gate["warnings"] == ["sidecar warning"]


@pytest.mark.integration
def test_acl_context_and_include_metadata_independent(tmp_path, monkeypatch) -> None:
    """acl_context=true + include_metadata=false still returns acl_gate; metadata is absent (S13)."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        result = _make_result(
            acl=["ns-a"],
            acl_source="collection_default",
            acl_sidecar_path=None,
            acl_warning=[],
        )
        # Add metadata to verify it's stripped
        result.metadata = {"author": "alice", "category": "docs"}

        mock_pipeline = MagicMock()
        mock_pipeline.get_collection_meta = AsyncMock(
            return_value=MagicMock(active_embedding_model=cfg.embedding_model)
        )
        mock_pipeline.search = AsyncMock(
            return_value=SearchPipelineResult(results=[result], acl_filtered=False)
        )
        mock_pipeline.warmup_models = AsyncMock()
        client.app.state.pipeline = mock_pipeline

        resp = client.post(
            "/search",
            json={
                "collection": "docs",
                "query": "hello",
                "acl_context": True,
                # No filters.include_metadata=true → metadata is stripped
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["results"], "expected at least one result"
        r = data["results"][0]

        # acl_gate is present despite include_metadata being absent
        assert r.get("acl_gate") is not None, "acl_gate must be non-null when acl_context=true"
        gate = r["acl_gate"]
        assert gate["source"] == "collection_default"
        assert isinstance(gate["warnings"], list)

        # metadata is empty (include_metadata defaults to false)
        assert r.get("metadata") == {} or r.get("metadata") is None, (
            "metadata must be empty when include_metadata is not requested"
        )


# ---------------------------------------------------------------------------
# S7: real-pipeline ACL exclusion — chunk restricted to "ns-only" absent for
# a caller in namespace "default"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_excluded_chunks_absent_with_acl_context(
    tmp_path, monkeypatch
) -> None:
    """S7: chunks the caller cannot see are absent from results even when acl_context=true.

    Uses a real SearchPipeline so that apply_acl_filter is exercised end-to-end.
    Ingests a document restricted to namespace "ns-only" (via YAML frontmatter _acl).
    Searches as namespace "default" — the document must not appear in results.
    """
    from pathlib import Path

    from archon_search.filters import SearchFilters

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    collection = "col_s7_acl_gate"
    embedding_dim = 4
    await store.ensure_collection(collection, embedding_dim)

    try:
        # Write a document with frontmatter ACL restricting access to "ns-only"
        doc = tmp_path / "restricted.md"
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
        assert ingest_result.chunks_created > 0, "Expected at least one chunk to be created"

        # Search as "default" namespace — ACL restricts to "ns-only", so no results
        search_result = await pipeline.search(
            "restricted",
            collection,
            namespace="default",
            embedder=pipeline._global_embedder,
            filters=SearchFilters(),
        )
        assert search_result.results == [], (
            f"Expected empty results for namespace 'default' (ACL restricts to 'ns-only'), "
            f"but got: {[r.source_path for r in search_result.results]!r}"
        )
        assert search_result.acl_filtered is True, (
            "acl_filtered must be True when chunks were dropped by ACL"
        )
    finally:
        await store.disconnect()
