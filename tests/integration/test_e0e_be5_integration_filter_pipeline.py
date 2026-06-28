"""tests/integration/test_e0e_be5_integration_filter_pipeline.py

BE-5 integration tests: filter + multi-collection search with real pipeline (no HTTP layer).

Plan task: BE-5 — Add integration test exercising filter + multi-collection search with real
pipeline.

These tests call ``SearchPipeline.search_many()`` directly with a real LanceDB store, so they
validate that filters are correctly threaded through the Use Cases and Interface Adapters layers
independently of the Presentation (HTTP/MCP) layer.

Run with:
    uv run pytest tests/integration/test_e0e_be5_integration_filter_pipeline.py -v
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper
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
# Test 1 — file_type filter: only the matching leg contributes results
# ---------------------------------------------------------------------------


def test_search_many_filter_multi_collection_real_pipeline(tmp_path: Path) -> None:
    """search_many() with file_type filter returns results from the matching leg only.

    Setup:
    - col-be5-md has a .md file
    - col-be5-py has a .py file

    When searching across both collections with filters={file_type="md"}, only the
    .md leg must contribute results. The .py leg must be excluded by the per-leg filter.
    """
    from archon_search.filters import SearchFilters

    async def _run():
        pipeline, _ = await _make_real_pipeline(tmp_path)

        md_path = tmp_path / "readme.md"
        md_path.write_text(
            "# README\n\nThis is a markdown documentation file for the project.\n" * 5
        )
        await pipeline.ingest_file(
            md_path, collection="col-be5-md", embedder=pipeline._global_embedder
        )

        py_path = tmp_path / "app.py"
        py_path.write_text(
            "# Application\ndef run():\n    '''Run the documentation app.'''\n    pass\n" * 5
        )
        await pipeline.ingest_file(
            py_path, collection="col-be5-py", embedder=pipeline._global_embedder
        )

        filters = SearchFilters(file_type="md")
        return await pipeline.search_many(
            "documentation markdown project",
            ["col-be5-md", "col-be5-py"],
            filters=filters,
        )

    result = asyncio.run(_run())

    # The .md leg must have contributed results
    assert result.results, "expected non-empty results from .md collection leg"

    # Every returned result must come from the .md collection
    for r in result.results:
        assert r.file_type == "md", (
            f"expected only .md results, got file_type={r.file_type!r} "
            f"(source_path={r.source_path!r})"
        )
        assert r.collection == "col-be5-md", (
            f"result from wrong collection: expected 'col-be5-md', got {r.collection!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 — source_path_glob filter: non-matching paths removed per-leg
# ---------------------------------------------------------------------------


def test_search_many_glob_filter_multi_collection_pipeline(tmp_path: Path) -> None:
    """search_many() with source_path_glob removes non-matching paths across two real collections.

    Setup:
    - col-be5-glob-a has a .md file (matches *.md glob)
    - col-be5-glob-b has a .txt file (does NOT match *.md glob)

    When searching across both collections with filters={source_path_glob="*.md"}, only
    the .md file must appear in results; the .txt file must be excluded by the per-leg
    Python fnmatch post-filter.
    """
    from archon_search.filters import SearchFilters

    async def _run():
        pipeline, _ = await _make_real_pipeline(tmp_path)

        md_path = tmp_path / "guide.md"
        md_path.write_text(
            "# Guide\n\nComprehensive overview and introduction to the project.\n" * 5
        )
        await pipeline.ingest_file(
            md_path, collection="col-be5-glob-a", embedder=pipeline._global_embedder
        )

        txt_path = tmp_path / "notes.txt"
        txt_path.write_text(
            "Project notes. Overview and introduction to documentation.\n" * 5
        )
        await pipeline.ingest_file(
            txt_path, collection="col-be5-glob-b", embedder=pipeline._global_embedder
        )

        filters = SearchFilters(source_path_glob="*.md")
        return await pipeline.search_many(
            "overview introduction project guide",
            ["col-be5-glob-a", "col-be5-glob-b"],
            filters=filters,
        )

    result = asyncio.run(_run())

    # Every result must match the glob — no .txt paths allowed
    assert result.results, "expected the .md file to survive the *.md glob filter"
    for r in result.results:
        assert r.source_path.endswith(".md"), (
            f"expected only .md paths after glob filter, "
            f"got source_path={r.source_path!r}"
        )
        assert r.collection == "col-be5-glob-a", (
            f"expected only col-be5-glob-a results after glob filter, "
            f"got collection={r.collection!r}"
        )
