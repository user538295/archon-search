"""End-to-end tests for C3c code symbol context enrichment.

These tests ingest real fixture files through the full pipeline into a real
(in-memory/tmp) LanceDB store, then retrieve and assert on stored metadata.

Scope: 10 use cases covering Python/TypeScript symbol types, module path
derivation, markdown pass-through, and graceful degradation.

Run:
    uv run pytest tests/test_e2e_code_enricher.py -m integration -v
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from archon_search.chunker import ASTChunker, DocumentChunker
from archon_search.embedder import Embedder
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline
from archon_search.reranker import Reranker

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "code"
_PY_FIXTURE = _FIXTURES / "python" / "sample.py"
_TS_FIXTURE = _FIXTURES / "typescript" / "sample.ts"

# chunk_size=7 (whitespace tokens) is the minimum that reliably lands at
# least one chunk boundary inside each named scope in both sample fixtures.
# Verified against the stub _FakeRecursiveChunker used by conftest.py.
_CHUNK_SIZE = 7


# ---------------------------------------------------------------------------
# Tree-sitter availability guards
# ---------------------------------------------------------------------------

_py_grammar_available = pytest.importorskip  # alias for readability; used inline below


def _skip_if_no_python_grammar() -> None:
    """Skip the calling test if tree-sitter-python is not installed."""
    pytest.importorskip("tree_sitter_python")


def _skip_if_no_ts_grammar() -> None:
    """Skip the calling test if tree-sitter-typescript is not installed."""
    pytest.importorskip("tree_sitter_typescript")


# ---------------------------------------------------------------------------
# Shared pipeline factory
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


def _make_pipeline(store) -> SearchPipeline:  # type: ignore[no-untyped-def]
    # BE-6: code files now dispatch through ASTChunker (not DocumentChunker) in
    # ingest_file(). Give it the same small chunk_size=7 budget so boundaries
    # still land at least one chunk inside each named scope in the fixtures —
    # otherwise the whole small fixture merges into a single module-level chunk.
    return SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=_CHUNK_SIZE),
        ast_chunker=ASTChunker(chunk_size=_CHUNK_SIZE),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


# ---------------------------------------------------------------------------
# Helper: load all chunk metadata rows for a doc from the store
# ---------------------------------------------------------------------------


async def _load_chunks(store, col: str, doc_id: str) -> list[dict]:
    """Return list of (metadata_dict, start_offset) for every chunk of doc_id."""
    db = store._require_connected()
    table = await db.open_table(col)
    rows = await table.query().where(f"doc_id = '{doc_id}'").to_list()
    assert rows, f"no chunks found for doc_id={doc_id!r} in collection {col!r}"
    result = []
    for row in rows:
        raw = row.get("metadata", "{}")
        meta = json.loads(raw) if isinstance(raw, str) else dict(raw)
        result.append(meta)
    return result


def _chunk_at_offset(chunks: list[dict], *, start_gte: int, start_lt: int) -> dict | None:
    """Find the first chunk whose _chunk_start_offset is in [start_gte, start_lt).

    Because metadata only stores the five enrichment fields (not start_offset),
    we rely on the store row ordering matching the ingestion order.

    Since start_offset is not stored in metadata, we accept any chunk from the
    range-of-interest by matching against the scope boundaries we derived
    from the fixture analysis.
    """
    return None  # unused — see per-test strategy below


# ---------------------------------------------------------------------------
# Use case 1: Python function chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_function_chunk(connected_store, col_name) -> None:
    """Chunk inside top_fn gets _symbol_type='function', _containing_function='top_fn'."""
    _skip_if_no_python_grammar()

    pipeline = _make_pipeline(connected_store)
    result = await pipeline.ingest_file(
        _PY_FIXTURE, col_name, embedder=pipeline._global_embedder
    )
    assert result.status == "ok", f"ingest failed: {result.error}"
    assert result.chunks_created > 0

    all_meta = await _load_chunks(connected_store, col_name, result.doc_id)

    # Find a chunk annotated as a function named top_fn
    top_fn_chunks = [
        m for m in all_meta if m.get("_containing_function") == "top_fn"
    ]
    assert top_fn_chunks, (
        f"expected at least one chunk with _containing_function='top_fn'; "
        f"got metadata: {all_meta}"
    )
    for m in top_fn_chunks:
        assert m["_symbol_type"] == "function", m
        assert m["_containing_class"] == "", m
        assert m.get("_module_path"), "_module_path must be non-empty"
        assert m["_symbol_subtype"] == "python-function", m


# ---------------------------------------------------------------------------
# Use case 2: Python method chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_method_chunk(connected_store, col_name) -> None:
    """Chunk inside outer_method of Outer → method, fn=outer_method, cls=Outer."""
    _skip_if_no_python_grammar()

    pipeline = _make_pipeline(connected_store)
    result = await pipeline.ingest_file(
        _PY_FIXTURE, col_name, embedder=pipeline._global_embedder
    )
    assert result.status == "ok", f"ingest failed: {result.error}"

    all_meta = await _load_chunks(connected_store, col_name, result.doc_id)

    outer_method_chunks = [
        m for m in all_meta if m.get("_containing_function") == "outer_method"
    ]
    assert outer_method_chunks, (
        f"expected chunk with _containing_function='outer_method'; got: {all_meta}"
    )
    for m in outer_method_chunks:
        assert m["_symbol_type"] == "method", m
        assert m["_containing_class"] == "Outer", m
        assert m["_symbol_subtype"] == "python-method", m


# ---------------------------------------------------------------------------
# Use case 3: Python nested class method (innermost class wins)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_nested_class_method(connected_store, col_name) -> None:
    """Chunk inside inner_method of Inner → _containing_class='Inner' (innermost wins)."""
    _skip_if_no_python_grammar()

    pipeline = _make_pipeline(connected_store)
    result = await pipeline.ingest_file(
        _PY_FIXTURE, col_name, embedder=pipeline._global_embedder
    )
    assert result.status == "ok", f"ingest failed: {result.error}"

    all_meta = await _load_chunks(connected_store, col_name, result.doc_id)

    inner_method_chunks = [
        m for m in all_meta if m.get("_containing_function") == "inner_method"
    ]
    assert inner_method_chunks, (
        f"expected chunk with _containing_function='inner_method'; got: {all_meta}"
    )
    for m in inner_method_chunks:
        assert m["_containing_class"] == "Inner", (
            f"innermost class must win; expected 'Inner', got: {m['_containing_class']!r}"
        )
        assert m["_symbol_type"] == "method", m


# ---------------------------------------------------------------------------
# Use case 4: Decorated function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_decorated_function(connected_store, col_name) -> None:
    """Chunk inside decorated_fn → _containing_function='decorated_fn'."""
    _skip_if_no_python_grammar()

    pipeline = _make_pipeline(connected_store)
    result = await pipeline.ingest_file(
        _PY_FIXTURE, col_name, embedder=pipeline._global_embedder
    )
    assert result.status == "ok", f"ingest failed: {result.error}"

    all_meta = await _load_chunks(connected_store, col_name, result.doc_id)

    decorated_chunks = [
        m for m in all_meta if m.get("_containing_function") == "decorated_fn"
    ]
    assert decorated_chunks, (
        f"expected chunk with _containing_function='decorated_fn'; got: {all_meta}"
    )
    for m in decorated_chunks:
        assert m["_symbol_type"] == "function", m
        assert m["_symbol_subtype"] == "python-function", m


# ---------------------------------------------------------------------------
# Use case 5: Module-level code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_module_level_chunk(connected_store, col_name) -> None:
    """Module-level gap chunk → _symbol_type='module', empty fn/class."""
    _skip_if_no_python_grammar()

    pipeline = _make_pipeline(connected_store)
    result = await pipeline.ingest_file(
        _PY_FIXTURE, col_name, embedder=pipeline._global_embedder
    )
    assert result.status == "ok", f"ingest failed: {result.error}"

    all_meta = await _load_chunks(connected_store, col_name, result.doc_id)

    module_chunks = [m for m in all_meta if m.get("_symbol_type") == "module"]
    assert module_chunks, (
        f"expected at least one chunk with _symbol_type='module'; got: {all_meta}"
    )
    for m in module_chunks:
        assert m["_containing_function"] == "", m
        assert m["_containing_class"] == "", m


# ---------------------------------------------------------------------------
# Use case 6: TypeScript function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typescript_function_chunk(connected_store, col_name) -> None:
    """Chunk inside topFn → _symbol_type='function', _symbol_subtype='typescript-function'."""
    _skip_if_no_ts_grammar()

    pipeline = _make_pipeline(connected_store)
    result = await pipeline.ingest_file(
        _TS_FIXTURE, col_name, embedder=pipeline._global_embedder
    )
    assert result.status == "ok", f"ingest failed: {result.error}"

    all_meta = await _load_chunks(connected_store, col_name, result.doc_id)

    topfn_chunks = [
        m for m in all_meta if m.get("_containing_function") == "topFn"
    ]
    assert topfn_chunks, (
        f"expected chunk with _containing_function='topFn'; got: {all_meta}"
    )
    for m in topfn_chunks:
        assert m["_symbol_type"] == "function", m
        assert m["_symbol_subtype"] == "typescript-function", m


# ---------------------------------------------------------------------------
# Use case 7: TypeScript method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typescript_method_chunk(connected_store, col_name) -> None:
    """Chunk inside myMethod of MyClass → _symbol_type='method', _containing_class='MyClass'."""
    _skip_if_no_ts_grammar()

    pipeline = _make_pipeline(connected_store)
    result = await pipeline.ingest_file(
        _TS_FIXTURE, col_name, embedder=pipeline._global_embedder
    )
    assert result.status == "ok", f"ingest failed: {result.error}"

    all_meta = await _load_chunks(connected_store, col_name, result.doc_id)

    mymethod_chunks = [
        m for m in all_meta if m.get("_containing_function") == "myMethod"
    ]
    assert mymethod_chunks, (
        f"expected chunk with _containing_function='myMethod'; got: {all_meta}"
    )
    for m in mymethod_chunks:
        assert m["_symbol_type"] == "method", m
        assert m["_containing_class"] == "MyClass", m
        assert m["_symbol_subtype"] == "typescript-method", m


# ---------------------------------------------------------------------------
# Use case 8: module_path with collection_root
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_module_path_with_collection_root(connected_store, col_name) -> None:
    """_module_path is a dotted path relative to collection_root, not just the stem."""
    _skip_if_no_python_grammar()

    pipeline = _make_pipeline(connected_store)
    fixtures_root = _FIXTURES.parent  # tests/fixtures/
    result = await pipeline.ingest_file(
        _PY_FIXTURE,
        col_name,
        embedder=pipeline._global_embedder,
        collection_root=fixtures_root,
    )
    assert result.status == "ok", f"ingest failed: {result.error}"

    all_meta = await _load_chunks(connected_store, col_name, result.doc_id)
    assert all_meta

    for m in all_meta:
        module_path = m.get("_module_path", "")
        assert "." in module_path, (
            f"expected dotted _module_path with collection_root; got: {module_path!r}"
        )
        # The path relative to tests/fixtures/ is code/python/sample → "code.python.sample"
        assert module_path == "code.python.sample", (
            f"expected 'code.python.sample', got: {module_path!r}"
        )


# ---------------------------------------------------------------------------
# Use case 9: Markdown file unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_markdown_file_unaffected(connected_store, col_name, tmp_path: Path) -> None:
    """Markdown file ingested through the same pipeline produces NO _symbol_type in any chunk."""
    pipeline = _make_pipeline(connected_store)

    md_file = tmp_path / "guide.md"
    md_file.write_text(
        "# Overview\n\nThis section explains the system.\n\n"
        "## Details\n\nMore context here.\n\n" * 8
    )

    result = await pipeline.ingest_file(
        md_file, col_name, embedder=pipeline._global_embedder
    )
    assert result.status == "ok", f"ingest failed: {result.error}"
    assert result.chunks_created > 0

    all_meta = await _load_chunks(connected_store, col_name, result.doc_id)

    for m in all_meta:
        assert "_symbol_type" not in m, (
            f"_symbol_type must not appear in markdown chunk metadata; got: {m}"
        )
        assert "_containing_function" not in m, m
        assert "_containing_class" not in m, m


# ---------------------------------------------------------------------------
# Use case 10: Graceful degradation — monkeypatched _get_grammar returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graceful_degradation_missing_grammar(
    connected_store, col_name
) -> None:
    """When _get_grammar returns None, ingest succeeds; chunks have _module_path but no _symbol_type."""
    pipeline = _make_pipeline(connected_store)

    with patch("archon_search.code_enricher._get_grammar", return_value=None):
        result = await pipeline.ingest_file(
            _PY_FIXTURE, col_name, embedder=pipeline._global_embedder
        )

    assert result.status == "ok", f"ingest must succeed even with missing grammar: {result.error}"
    assert result.chunks_created > 0

    all_meta = await _load_chunks(connected_store, col_name, result.doc_id)

    for m in all_meta:
        assert "_symbol_type" not in m, (
            f"_symbol_type must be absent when grammar is missing; got: {m}"
        )
        assert "_module_path" in m, (
            f"_module_path must be present even without grammar; got: {m}"
        )
