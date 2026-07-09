"""Integration test for BE-6 — ASTChunker end-to-end ingest (E2g task 5.1).

Ingests a real Python file through the real SearchPipeline (real SearchStore,
real LanceDB in tmp_path) and verifies chunk boundaries align to tree-sitter
scope boundaries from the shared ScopeTable, with enrichment metadata still
correctly attached to each chunk.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")

pytestmark = pytest.mark.integration


class _MockEmbedderBackend:
    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _MockRerankerBackend:
    is_warm: bool = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


async def _make_pipeline(tmp_path, monkeypatch):
    """Real SearchStore + SearchPipeline with a small ASTChunker budget, so the
    two scopes in the test source cannot merge into a single chunk.
    """
    from archon_search.chunker import ASTChunker, DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    store = SearchStore(str(tmp_path / "db"))
    await store.connect()

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        ast_chunker=ASTChunker(chunk_size=5),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    return store, pipeline


async def _collect_chunks(store, col: str, doc_id: str) -> list[dict]:
    """Return {text, metadata} for every chunk of *doc_id*, ordered by chunk_id."""
    db = store._require_connected()
    table = await db.open_table(col)
    rows = await table.query().where(f"doc_id = '{doc_id}'").to_list()
    assert rows, f"no rows for doc_id={doc_id!r}"
    results = []
    for row in rows:
        raw = row.get("metadata", "{}")
        meta = json.loads(raw) if isinstance(raw, str) else dict(raw)
        results.append({"chunk_id": row["chunk_id"], "text": row["text"], "metadata": meta})
    results.sort(key=lambda r: r["chunk_id"])
    return results


async def test_codeFileIngest_usesAstChunkBoundaries(tmp_path: Path, monkeypatch) -> None:
    """Ingesting a real Python file produces chunks aligned to tree-sitter scopes,
    with symbol enrichment metadata still correctly attached to each chunk.
    """
    store, pipeline = await _make_pipeline(tmp_path, monkeypatch)
    try:
        col = "code-col"
        await store.ensure_collection(col, embedding_dim=4)

        source_dir = tmp_path / "src"
        source_dir.mkdir()
        py_file = source_dir / "sample.py"
        source = (
            "def top_fn():\n"
            "    return 1\n"
            "\n"
            "\n"
            "class Widget:\n"
            "    def render(self):\n"
            "        return 'ok'\n"
        )
        py_file.write_text(source)

        result = await pipeline.ingest_file(
            py_file, col, embedder=pipeline._global_embedder, collection_root=source_dir
        )
        assert result.status == "ok", f"ingest failed: {result.error}"

        chunks = await _collect_chunks(store, col, result.doc_id)
        assert chunks

        # The function boundary must be a chunk boundary: no single chunk's
        # text contains BOTH top_fn's body AND the Widget class definition —
        # proves AST-aligned splitting occurred at the scope boundary (the
        # configured budget, chunk_size=5, is intentionally small enough that
        # the two scopes cannot merge into one chunk).
        for c in chunks:
            assert not ("def top_fn" in c["text"] and "class Widget" in c["text"]), (
                f"a chunk spans both top_fn and Widget — boundary not respected: {c['text']!r}"
            )

        # A chunk starting exactly at the class boundary must carry correct
        # enrichment metadata — proves the enricher resolved against the SAME
        # scope_table the AST chunker consumed, end-to-end through ingest.
        widget_chunk = next((c for c in chunks if c["text"].startswith("class Widget:")), None)
        assert widget_chunk is not None, (
            f"expected a chunk starting exactly at 'class Widget:'; got texts={[c['text'] for c in chunks]}"
        )
        assert widget_chunk["metadata"].get("_symbol_type") == "class"
        assert widget_chunk["metadata"].get("_containing_class") == "Widget"

        # top_fn's chunk must independently carry function-level metadata.
        top_fn_chunk = next((c for c in chunks if "def top_fn" in c["text"]), None)
        assert top_fn_chunk is not None
        assert top_fn_chunk["metadata"].get("_symbol_type") == "function"
        assert top_fn_chunk["metadata"].get("_containing_function") == "top_fn"
    finally:
        await store.disconnect()
