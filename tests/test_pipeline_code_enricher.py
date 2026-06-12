"""Tests for Task 7.1 — CodeEnricher dispatch in pipeline.py.

Verifies:
- Python files ingested via pipeline carry symbol metadata (_symbol_type, etc.)
- Markdown files are unaffected (no _symbol_type)
- Graceful handling of scope builder crashes (monkeypatched _build_scope_table)
- _module_path is derived correctly when collection_root is provided
- ingest_directory forwards collection_root to every ingest_file call
- Default collection_root=None produces stem-only _module_path
- Missing grammar degrades gracefully (chunks stored, no _symbol_type, _module_path present)

NOT marked @pytest.mark.integration — these use inline fixtures and monkeypatched
subsystems, not live infrastructure. See Task 7.1 spec note.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from archon_search.embedder import Embedder
from archon_search.reranker import Reranker


# ---------------------------------------------------------------------------
# Shared test infrastructure
# ---------------------------------------------------------------------------

class _MockEmbedderBackend:
    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]


class _MockRerankerBackend:
    is_warm: bool = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


def _make_pipeline(store):  # type: ignore[no-untyped-def]
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    return SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


async def _collect_metadata_from_store(store, col: str, doc_id: str) -> list[dict]:
    """Return the parsed metadata dict for every chunk of *doc_id*."""
    db = store._require_connected()
    table = await db.open_table(col)
    rows = await table.query().where(f"doc_id = '{doc_id}'").to_list()
    assert rows, f"no rows for doc_id={doc_id!r}"
    results = []
    for row in rows:
        raw = row.get("metadata", "{}")
        if isinstance(raw, str):
            results.append(json.loads(raw))
        else:
            results.append(dict(raw))
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_python_file_stores_symbol_metadata(
    connected_store, col_name, tmp_path: Path
) -> None:
    """Python files produce chunks with _symbol_type in metadata."""
    pipeline = _make_pipeline(connected_store)

    py_file = tmp_path / "sample.py"
    py_file.write_text(
        "def top_fn():\n"
        "    return 42\n\n"
        "class MyClass:\n"
        "    def my_method(self):\n"
        "        return 'hello'\n" * 5
    )

    result = await pipeline.ingest_file(py_file, col_name, embedder=pipeline._global_embedder)
    assert result.status == "ok", f"ingest failed: {result.error}"

    all_meta = await _collect_metadata_from_store(connected_store, col_name, result.doc_id)
    assert all_meta, "expected at least one chunk"

    # At least one chunk should carry _symbol_type
    has_symbol_type = any("_symbol_type" in m for m in all_meta)
    assert has_symbol_type, (
        f"expected _symbol_type in at least one chunk metadata; got: {all_meta}"
    )


@pytest.mark.asyncio
async def test_ingest_markdown_file_unaffected(
    connected_store, col_name, tmp_path: Path
) -> None:
    """Markdown files use MarkdownEnricher; no _symbol_type key in any chunk."""
    pipeline = _make_pipeline(connected_store)

    md_file = tmp_path / "doc.md"
    md_file.write_text(
        "# Introduction\n\nThis is a document.\n\n" * 10
    )

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
    assert result.status == "ok", f"ingest failed: {result.error}"

    all_meta = await _collect_metadata_from_store(connected_store, col_name, result.doc_id)
    for meta in all_meta:
        assert "_symbol_type" not in meta, (
            f"_symbol_type should not appear in markdown chunk metadata: {meta}"
        )


@pytest.mark.asyncio
async def test_ingest_python_file_graceful_on_scope_table_crash(
    connected_store, col_name, tmp_path: Path, caplog
) -> None:
    """Catastrophic scope builder crash does not abort ingest.

    Note: tree-sitter does NOT raise on broken syntax. We use unittest.mock.patch
    to simulate a catastrophic scope-builder failure, patching within the coroutine
    scope to avoid interference with module-scoped fixtures.
    """
    from unittest.mock import patch

    pipeline = _make_pipeline(connected_store)

    py_file = tmp_path / "crash_test.py"
    py_file.write_text(
        "def foo():\n    return 1\n\n" * 5
    )

    def _raise(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated scope builder crash")

    caplog.set_level(logging.WARNING, logger="archon_search")

    with patch("archon_search.code_enricher._build_scope_table", side_effect=_raise):
        result = await pipeline.ingest_file(py_file, col_name, embedder=pipeline._global_embedder)

    assert result.status == "ok", "ingest must succeed even when scope builder crashes"

    all_meta = await _collect_metadata_from_store(connected_store, col_name, result.doc_id)
    assert all_meta, "expected at least one chunk"

    for meta in all_meta:
        assert "_symbol_type" not in meta, "_symbol_type should be absent after scope crash"
        assert "_module_path" in meta, "_module_path should be present even after scope crash"

    warning_found = any(
        "tree-sitter parse failed" in r.message for r in caplog.records
    )
    assert warning_found, "expected WARNING log for scope builder crash"


@pytest.mark.asyncio
async def test_ingest_python_file_module_path(
    connected_store, col_name, tmp_path: Path
) -> None:
    """_module_path matches expected dotted path when collection_root is provided."""
    pipeline = _make_pipeline(connected_store)

    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir()
    py_file = pkg_dir / "mymod.py"
    py_file.write_text(
        "def foo():\n    return 1\n\n" * 5
    )

    result = await pipeline.ingest_file(
        py_file, col_name, embedder=pipeline._global_embedder,
        collection_root=tmp_path
    )
    assert result.status == "ok", f"ingest failed: {result.error}"

    all_meta = await _collect_metadata_from_store(connected_store, col_name, result.doc_id)
    assert all_meta, "expected at least one chunk"

    for meta in all_meta:
        assert meta.get("_module_path") == "mypkg.mymod", (
            f"expected _module_path='mypkg.mymod', got: {meta.get('_module_path')!r}"
        )


@pytest.mark.asyncio
async def test_ingest_directory_forwards_collection_root(
    connected_store, col_name, tmp_path: Path
) -> None:
    """collection_root is forwarded from ingest_directory to each ingest_file call."""
    pipeline = _make_pipeline(connected_store)

    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    py_file = pkg_dir / "mod.py"
    py_file.write_text(
        "def foo():\n    return 1\n\n" * 5
    )

    results = await pipeline.ingest_directory(
        tmp_path, col_name,
        embedder=pipeline._global_embedder,
        collection_root=tmp_path,
        rebuild_fts=False,
    )
    assert any(r.status == "ok" for r in results), "expected at least one successful ingest"

    ok_result = next(r for r in results if r.status == "ok")
    all_meta = await _collect_metadata_from_store(connected_store, col_name, ok_result.doc_id)
    assert all_meta, "expected at least one chunk"

    for meta in all_meta:
        assert meta.get("_module_path") == "pkg.mod", (
            f"expected _module_path='pkg.mod', got: {meta.get('_module_path')!r}"
        )


@pytest.mark.asyncio
async def test_ingest_directory_default_collection_root_is_none(
    connected_store, col_name, tmp_path: Path
) -> None:
    """Default collection_root=None produces stem-only _module_path (not dotted)."""
    pipeline = _make_pipeline(connected_store)

    py_file = tmp_path / "mod.py"
    py_file.write_text(
        "def foo():\n    return 1\n\n" * 5
    )

    # Do NOT pass collection_root — verify default is None (stem-only fallback)
    results = await pipeline.ingest_directory(
        tmp_path, col_name,
        embedder=pipeline._global_embedder,
        rebuild_fts=False,
    )
    assert any(r.status == "ok" for r in results), "expected at least one successful ingest"

    ok_result = next(r for r in results if r.status == "ok")
    all_meta = await _collect_metadata_from_store(connected_store, col_name, ok_result.doc_id)
    assert all_meta, "expected at least one chunk"

    for meta in all_meta:
        module_path = meta.get("_module_path", "")
        # stem-only means "mod", not "pkg.mod" or any dotted path
        assert "." not in module_path or module_path == "", (
            f"expected stem-only _module_path (no dots), got: {module_path!r}"
        )


@pytest.mark.asyncio
async def test_ingest_code_file_graceful_on_missing_grammar(
    connected_store, col_name, tmp_path: Path
) -> None:
    """Missing grammar degrades gracefully: chunks stored, no _symbol_type, _module_path present."""
    from unittest.mock import patch

    pipeline = _make_pipeline(connected_store)

    py_file = tmp_path / "no_grammar.py"
    py_file.write_text(
        "def foo():\n    return 1\n\n" * 5
    )

    # Patch _get_grammar to return None for any extension
    with patch("archon_search.code_enricher._get_grammar", return_value=None):
        result = await pipeline.ingest_file(py_file, col_name, embedder=pipeline._global_embedder)

    assert result.status == "ok", "ingest must succeed even when grammar is missing"

    all_meta = await _collect_metadata_from_store(connected_store, col_name, result.doc_id)
    assert all_meta, "expected at least one chunk"

    for meta in all_meta:
        assert "_symbol_type" not in meta, "_symbol_type should be absent when grammar is missing"
        assert "_module_path" in meta, "_module_path should be present even without grammar"
