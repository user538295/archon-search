"""Task 6.1 — FTS delete with no phantom hits.

Verifies that after a document is deleted (via pipeline.delete_document or MCP
delete_document), the deleted text no longer appears in FTS search results.
Also verifies that pipeline.ingest_directory calls optimize_fts (not
rebuild_fts_index) when the FTS index already exists.

Tests:
    test_ingest_directory_optimize_fts_called_not_rebuild_when_index_exists
    test_mcp_delete_document_no_phantom_hits_in_subsequent_search
    test_e2e_delete_document_via_mcp_no_phantom_hits
    test_e2e_reingest_via_ingest_file_old_content_absent

Run with:
    uv run pytest tests/integration/test_fts_delete_no_phantom.py -v
"""
from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.integration.conftest import (
    ingest_file_via_path,
    make_real_app,
    make_real_pipeline,
    search,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# FastMCP stub — same pattern as test_mcp_error_paths.py.
# Must be installed before archon_search.server.mcp is imported.
# ---------------------------------------------------------------------------

if "fastmcp" not in sys.modules:
    try:
        # Prefer the real ``fastmcp`` package (3.4.x) whose FastMCP exposes
        # ``http_app()``. The low-level ``mcp.server.fastmcp`` class only has the
        # removed ``streamable_http_app()`` and would poison sys.modules for any
        # sibling test that builds a real MCP HTTP app via ``http_app(path='/')``.
        import fastmcp as _real_fastmcp_pkg  # type: ignore[import]

        sys.modules["fastmcp"] = _real_fastmcp_pkg  # type: ignore[assignment]
    except ImportError:
        _stub_fastmcp = types.ModuleType("fastmcp")
        _stub_fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _stub_fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _stub_fastmcp

import importlib as _importlib

_importlib.import_module("archon_search.server.mcp")


# ---------------------------------------------------------------------------
# Helper — build a minimal MCP app wired to a real pipeline
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, name: str) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            return func

        return decorator

    def custom_route(self, path: str, methods: list[str] | None = None) -> Any:
        def decorator(func: Any) -> Any:
            return func

        return decorator


class _FakeFastMCP:
    def __new__(cls, name: str, **kwargs: Any) -> _FakeApp:  # type: ignore[misc]
        return _FakeApp(name)


def _make_mcp_app(pipeline: Any) -> _FakeApp:
    """Wire a real pipeline into a stub FastMCP app."""
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        return mcp_module.create_app(  # type: ignore[call-arg]
            pipeline,
            "default",
            writer=None,
            config=None,
        )


def _doc_id(path: Path) -> str:
    """Compute doc_id the same way pipeline.ingest_file does."""
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Test 1 — ingest_directory calls optimize_fts, not rebuild_fts_index
# ---------------------------------------------------------------------------


async def test_ingest_directory_optimize_fts_called_not_rebuild_when_index_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pipeline.ingest_directory must call optimize_fts (not rebuild_fts_index) when FTS index exists.

    Verifies the C6 O(delta) path: Plan A uses optimize_fts for incremental
    updates; rebuild_fts_index is the fallback for Plan B or failures.

    A spy is placed on store.optimize_fts and store.rebuild_fts_index after the
    initial FTS index is built by the first ingest_file call. The second call
    to ingest_directory must use the cheaper optimize path.
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    collection = "fts-optimize-spy-col"
    EMBEDDING_DIM = 4
    await store.ensure_collection(collection, EMBEDDING_DIM)

    try:
        # Seed one file and build the FTS index via a direct ingest_file call
        seed_file = tmp_path / "seed.txt"
        seed_file.write_text("seeding the fts index with initial content here", encoding="utf-8")
        result = await pipeline.ingest_file(
            seed_file, collection, embedder=pipeline._global_embedder
        )
        assert result.status == "ok" and result.chunks_created > 0, (
            f"Seed ingest failed: {result}"
        )

        # Create a sub-directory with new files for ingest_directory
        sub_dir = tmp_path / "batch"
        sub_dir.mkdir()
        (sub_dir / "doc_a.txt").write_text(
            "document alpha content for ingest_directory test", encoding="utf-8"
        )
        (sub_dir / "doc_b.txt").write_text(
            "document beta content for ingest_directory test", encoding="utf-8"
        )

        # Spy on optimize_fts and rebuild_fts_index
        optimize_calls: list[str] = []
        rebuild_calls: list[str] = []

        original_optimize = store.optimize_fts
        original_rebuild = store.rebuild_fts_index

        async def spy_optimize(col: str) -> None:
            optimize_calls.append(col)
            return await original_optimize(col)

        async def spy_rebuild(col: str, *, language: str = "") -> None:
            rebuild_calls.append(col)
            return await original_rebuild(col, language=language)

        store.optimize_fts = spy_optimize  # type: ignore[method-assign]
        store.rebuild_fts_index = spy_rebuild  # type: ignore[method-assign]

        try:
            results = await pipeline.ingest_directory(
                sub_dir, collection, embedder=pipeline._global_embedder
            )
        finally:
            # Restore originals so teardown works correctly
            store.optimize_fts = original_optimize  # type: ignore[method-assign]
            store.rebuild_fts_index = original_rebuild  # type: ignore[method-assign]

        assert any(r.status == "ok" for r in results), (
            f"Expected at least one successful ingest; got {[r.status for r in results]!r}"
        )

        # Plan A: optimize_fts must be called EXACTLY ONCE for the whole batch
        # (O(delta) property — not once per file), rebuild_fts_index must not be called.
        assert len(optimize_calls) == 1, (
            "Expected store.optimize_fts to be called exactly once by ingest_directory "
            f"(O(delta) batch path, Plan A), but call count was {len(optimize_calls)}. "
            f"rebuild_fts_index called: {rebuild_calls!r}"
        )
        assert not rebuild_calls, (
            f"rebuild_fts_index must NOT be called when FTS index exists (Plan A); "
            f"but it was called for: {rebuild_calls!r}"
        )
        assert optimize_calls[0] == collection, (
            f"optimize_fts called for unexpected collection: {optimize_calls!r}"
        )
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# Test 2 — MCP delete_document removes entry from FTS
# ---------------------------------------------------------------------------


async def test_mcp_delete_document_no_phantom_hits_in_subsequent_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP delete_document followed by store.hybrid_search returns zero results.

    Verifies that optimize_fts (called by delete_document) actually removes the
    deleted doc's text from the FTS index so subsequent searches find nothing.
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    collection = "fts-mcp-delete-col"
    EMBEDDING_DIM = 4
    await store.ensure_collection(collection, EMBEDDING_DIM)

    unique_term = "phantomkrypton42xyzwq"

    try:
        # Ingest one document with a unique term
        doc_file = tmp_path / "doc_to_delete.txt"
        doc_file.write_text(
            f"This document contains the unique term {unique_term} only here.",
            encoding="utf-8",
        )
        result = await pipeline.ingest_file(
            doc_file, collection, embedder=pipeline._global_embedder
        )
        assert result.status == "ok" and result.chunks_created > 0, (
            f"Ingest failed: {result}"
        )

        doc_id = _doc_id(doc_file)

        # Verify the FTS index was actually built after ingest.
        # optimize_fts() raises FTSIndexNotFoundError only when no FTS index exists;
        # succeeding here confirms the index is present and the phantom test is meaningful.
        from archon_search.store import FTSIndexNotFoundError  # noqa: PLC0415

        try:
            await store.optimize_fts(collection)
        except FTSIndexNotFoundError:
            pytest.fail(
                f"FTS index was not built for collection {collection!r} after ingest. "
                "The phantom test would pass vacuously without an FTS index."
            )

        # Confirm the FTS index has the unique term before deletion.
        # hybrid_search uses both vector and FTS — we already confirmed FTS index exists above,
        # so a hit here could be from either path; after deletion the check catches FTS phantoms.
        query_vec = [0.1, 0.2, 0.3, 0.4]
        hits_before = await store.hybrid_search(collection, query_vec, unique_term, 10)
        assert any(unique_term in (h.text or "") for h in hits_before), (
            f"Expected to find {unique_term!r} before delete; hits: {[h.text for h in hits_before]!r}"
        )

        # Delete via MCP tool
        app = _make_mcp_app(pipeline)
        result_dict = await app.tools["delete_document"](
            doc_id=doc_id,
            collection=collection,
        )

        assert isinstance(result_dict, dict), f"Expected dict result, got {type(result_dict)}"
        assert "error" not in result_dict, (
            f"MCP delete_document returned an error: {result_dict}"
        )
        assert result_dict.get("deleted", 0) > 0, (
            f"Expected deleted > 0; got {result_dict}"
        )

        # After deletion, FTS search must return zero hits for the unique term
        hits_after = await store.hybrid_search(collection, query_vec, unique_term, 10)
        phantom_hits = [h for h in hits_after if unique_term in (h.text or "")]
        assert not phantom_hits, (
            f"Phantom FTS hits found after delete for term {unique_term!r}: "
            f"{[h.text for h in phantom_hits]!r}"
        )
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# Test 3 — E2E: ingest → search → MCP delete → search returns zero
# ---------------------------------------------------------------------------


def test_e2e_delete_document_via_mcp_no_phantom_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full e2e: POST /ingest → verify in /search → MCP delete → /search returns zero.

    Uses TestClient for HTTP ingest and search, and the MCP tool for deletion
    (document deletion is only exposed via MCP, not via REST).
    """
    unique_term = "e2ephantomblizzard99xyz"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        collection = "fts-e2e-mcp-delete-col"

        # Write a file and ingest it via HTTP
        doc_file = tmp_path / "e2e_doc.txt"
        doc_file.write_text(
            f"End to end test document with unique term {unique_term} in content.",
            encoding="utf-8",
        )
        ingest_file_via_path(client, collection, str(doc_file), api_key=api_key)

        # Verify it appears in search
        items_before = search(client, collection, unique_term, api_key=api_key)
        assert items_before, (
            f"Expected search results for {unique_term!r} before delete; got none"
        )
        found_doc_ids = {item["doc_id"] for item in items_before}
        doc_id = _doc_id(doc_file)
        assert doc_id in found_doc_ids, (
            f"Expected doc_id {doc_id!r} in search results before delete; "
            f"found doc_ids: {found_doc_ids!r}"
        )

        # Delete via MCP tool using the real pipeline from the running app
        pipeline = client.app.state.pipeline
        app = _make_mcp_app(pipeline)

        import asyncio

        result_dict = asyncio.run(
            app.tools["delete_document"](doc_id=doc_id, collection=collection)
        )
        assert isinstance(result_dict, dict), f"Expected dict, got {type(result_dict)}"
        assert result_dict.get("deleted", 0) > 0, (
            f"Expected deleted > 0 after MCP delete; got {result_dict}"
        )

        # After MCP deletion, /search must return zero hits for the unique term
        items_after = search(client, collection, unique_term, api_key=api_key)
        phantom = [item for item in items_after if item.get("doc_id") == doc_id]
        assert not phantom, (
            f"Phantom hits found after MCP delete for doc_id {doc_id!r}: {phantom!r}"
        )


# ---------------------------------------------------------------------------
# Test 4 — E2E: re-ingest updated file removes stale FTS entries
# ---------------------------------------------------------------------------


def test_e2e_reingest_via_ingest_file_old_content_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /ingest file with text A. Modify to text B. POST /ingest again.

    Assert /search for text A returns zero results; /search for text B returns
    results. Verifies incremental FTS update removes stale content on re-ingest.
    """
    unique_term_a = "reingestableoakwood77abc"
    unique_term_b = "reingestablepinewood77xyz"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        collection = "fts-reingest-col"

        # Write initial content with term A and ingest
        doc_file = tmp_path / "reingest_doc.txt"
        doc_file.write_text(
            f"Initial content with unique term {unique_term_a} here for re-ingest test.",
            encoding="utf-8",
        )
        ingest_file_via_path(client, collection, str(doc_file), api_key=api_key)

        # Confirm term A is searchable before modification
        items_a_before = search(client, collection, unique_term_a, api_key=api_key)
        assert items_a_before, (
            f"Expected results for {unique_term_a!r} before modification; got none"
        )

        # Overwrite the file with new content (term B only)
        doc_file.write_text(
            f"Updated content with unique term {unique_term_b} here after modification.",
            encoding="utf-8",
        )
        ingest_file_via_path(client, collection, str(doc_file), api_key=api_key)

        # After re-ingest: term A must NOT appear in the text of any result.
        # The stub embedder returns the same vector for all queries, so a vector-only
        # match may still surface the updated document via score. The stale-FTS check
        # is: no result whose text contains unique_term_a (the old content). If FTS
        # cleanup worked, the only hits are vector-score matches whose text contains
        # unique_term_b (new content), not unique_term_a.
        items_a_after = search(client, collection, unique_term_a, api_key=api_key)
        phantom_a = [item for item in items_a_after if unique_term_a in (item.get("text") or "")]
        assert not phantom_a, (
            f"Phantom FTS hits (old text) for term {unique_term_a!r} after re-ingest: {phantom_a!r}"
        )

        # After re-ingest: term B MUST appear (new content indexed)
        items_b_after = search(client, collection, unique_term_b, api_key=api_key)
        assert items_b_after, (
            f"Expected results for new term {unique_term_b!r} after re-ingest; got none"
        )
