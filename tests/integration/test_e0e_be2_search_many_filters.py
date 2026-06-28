"""tests/integration/test_e0e_be2_search_many_filters.py

BE-2 integration test: search_many() with `filters` parameter against a real pipeline.

Plan task: BE-2 — Add `filters` param to `search_many()` and thread through ALL call sites.

Run with:
    uv run pytest tests/integration/test_e0e_be2_search_many_filters.py -v
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_real_pipeline(tmp_path: Path):
    """Build a real SearchStore + SearchPipeline backed by LanceDB in tmp_path."""
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    class _StubEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    class _StubRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    store = SearchStore(str(tmp_path / "db"))
    await store.connect()

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_StubEmbedderBackend()),
        reranker=Reranker(_StubRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=20,
        top_k_return=10,
    )
    return pipeline, store


# ---------------------------------------------------------------------------
# Test: file_type filter with real pipeline (two collections)
# ---------------------------------------------------------------------------

def test_search_many_file_type_filter_applied_per_leg(tmp_path: Path) -> None:
    """Real pipeline + real LanceDB store: file_type filter returns results from matching leg only.

    Collection A has .md files; Collection B has .py files.
    A filter for .md must return only results from collection A.
    """
    from archon_search.filters import SearchFilters

    async def _run() -> object:
        pipeline, store = await _make_real_pipeline(tmp_path)

        # Ingest a .md file into col-a (collections created automatically on ingest)
        md_path = tmp_path / "guide.md"
        md_path.write_text(
            "# Setup Guide\n\nThis is a markdown guide for setup and configuration.\n" * 5
        )
        await pipeline.ingest_file(md_path, collection="col-a", embedder=pipeline._global_embedder)

        # Ingest a .py file into col-b
        py_path = tmp_path / "main.py"
        py_path.write_text(
            "# Python module\ndef main():\n    pass\n\n# setup configuration here\n" * 5
        )
        await pipeline.ingest_file(py_path, collection="col-b", embedder=pipeline._global_embedder)

        # Search with file_type filter for markdown files
        filters = SearchFilters(file_type="md")
        return await pipeline.search_many(
            "setup configuration guide", ["col-a", "col-b"], filters=filters
        )

    result = asyncio.run(_run())

    # Only col-a has .md files; results must be from col-a only
    assert result.results, "expected non-empty results"
    result_collections = {r.collection for r in result.results}
    assert "col-a" in result_collections, "col-a results expected"
    assert "col-b" not in result_collections, "col-b has .py files, must be excluded by filter"


# ---------------------------------------------------------------------------
# Test: source_path_glob filter with real pipeline
# ---------------------------------------------------------------------------

def test_search_many_filter_glob_real_pipeline(tmp_path: Path) -> None:
    """Real pipeline + real LanceDB store: source_path_glob filter removes non-matching paths.

    Both collections have files, but glob *.md matches only those in col-glob-a.
    """
    from archon_search.filters import SearchFilters

    async def _run() -> object:
        pipeline, store = await _make_real_pipeline(tmp_path)

        md_path = tmp_path / "readme.md"
        md_path.write_text(
            "# README\n\nThis document explains the project overview and introduction.\n" * 5
        )
        await pipeline.ingest_file(md_path, collection="glob-a", embedder=pipeline._global_embedder)

        txt_path = tmp_path / "notes.txt"
        txt_path.write_text(
            "Project notes. Overview and introduction to the documentation.\n" * 5
        )
        await pipeline.ingest_file(txt_path, collection="glob-b", embedder=pipeline._global_embedder)

        # Filter by glob pattern for .md files only
        filters = SearchFilters(source_path_glob="*.md")
        return await pipeline.search_many(
            "project overview introduction", ["glob-a", "glob-b"], filters=filters
        )

    result = asyncio.run(_run())

    # Non-.md paths must be absent
    for r in result.results:
        assert r.source_path.endswith(".md"), (
            f"expected only .md results, got source_path={r.source_path!r}"
        )
