"""packages/archon-search/tests/test_chunker.py — unit tests for DocumentChunker."""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search._types import ChunkRecord
from archon_search.chunker import DocumentChunker


_LONG_TEXT = " ".join([f"This is sentence number {i} in a long document." for i in range(200)])

# Required kwargs for chunker.chunk() after Task 3.2; tests that don't care
# about these values reuse this dict to stay focused on what they're pinning.
_DEFAULT_KW = {
    "file_type": "md",
    "updated_at": "2026-05-21T00:00:00+00:00",
    "ingested_by": "cli",
}


def test_chunker_returns_chunk_records() -> None:
    """Short markdown text → list of ChunkRecord."""
    chunker = DocumentChunker()
    records = chunker.chunk("# Hello\n\nThis is a short document.", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert isinstance(records, list)
    assert all(isinstance(r, ChunkRecord) for r in records)


def test_chunker_returns_empty_placeholder_chunk_id() -> None:
    """chunk_id must be empty string in chunker output (pipeline assigns sequential IDs)."""
    chunker = DocumentChunker()
    records = chunker.chunk("Hello world.", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert all(r.chunk_id == "" for r in records), "chunk_id must be empty placeholder"


def test_chunker_all_records_have_doc_id() -> None:
    """Every chunk carries the provided doc_id."""
    chunker = DocumentChunker()
    records = chunker.chunk("Hello world. More text here.", "my-doc-id", "/tmp/test.md", **_DEFAULT_KW)
    assert all(r.doc_id == "my-doc-id" for r in records)


def test_chunker_vector_field_is_empty() -> None:
    """vector == [] before pipeline fills it."""
    chunker = DocumentChunker()
    records = chunker.chunk("Hello world.", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert all(r.vector == [] for r in records)


def test_chunker_empty_text_returns_empty_list() -> None:
    """Empty text → empty list, no crash."""
    chunker = DocumentChunker()
    records = chunker.chunk("", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert records == []


def test_chunker_whitespace_only_returns_empty_list() -> None:
    """Whitespace-only text → empty list (no garbage chunks in vector store)."""
    chunker = DocumentChunker()
    assert chunker.chunk("   \n\t  ", "doc1", "/tmp/doc1.md", **_DEFAULT_KW) == []


def test_chunker_long_text_produces_multiple_chunks() -> None:
    """5000-char text → multiple chunks."""
    chunker = DocumentChunker(chunk_size=128)
    records = chunker.chunk(_LONG_TEXT, "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert len(records) > 1


def test_chunker_non_empty_text_in_chunks() -> None:
    """Every produced chunk has non-empty text."""
    chunker = DocumentChunker()
    records = chunker.chunk("Hello world. This is a test.", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert all(len(r.text) > 0 for r in records)


def test_chunker_respects_chunk_size() -> None:
    """Long text chunked with chunk_size=64 — no chunk exceeds 64 * 1.2 tokens."""
    from chonkie import RecursiveChunker  # access chunks directly for token_count
    raw_chunker = RecursiveChunker(tokenizer="gpt2", chunk_size=64)
    raw_chunks = raw_chunker.chunk(_LONG_TEXT)
    max_tokens = int(64 * 1.2)
    for chunk in raw_chunks:
        assert chunk.token_count <= max_tokens, f"Chunk token_count {chunk.token_count} exceeds {max_tokens}"


def test_chunker_source_path_preserved() -> None:
    """source_path from argument is set on every chunk."""
    chunker = DocumentChunker()
    records = chunker.chunk("Hello world.", "doc1", "/some/special/path.md", **_DEFAULT_KW)
    assert all(r.source_path == "/some/special/path.md" for r in records)


def test_chunker_indexed_at_is_iso8601() -> None:
    """indexed_at uses fixed-width UTC format (microseconds + Z suffix) for date-filter compatibility."""
    import re

    chunker = DocumentChunker()
    records = chunker.chunk("Hello world.", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    # Must match the fixed-width Z format expected by store._FIXED_WIDTH_PATTERN:
    # YYYY-MM-DDTHH:MM:SS.ffffffZ  (no +00:00 suffix)
    fixed_width_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
    for r in records:
        assert fixed_width_pattern.match(r.indexed_at), (
            f"indexed_at must be fixed-width UTC (microseconds+Z), got: {r.indexed_at!r}"
        )


# ---------------------------------------------------------------------------
# Task 3.2 — new required keyword-only metadata args
# ---------------------------------------------------------------------------


def test_chunk_propagates_file_type() -> None:
    chunker = DocumentChunker()
    records = chunker.chunk(
        "Hello world.", "doc1", "/tmp/doc1.md",
        file_type="md", updated_at="2026-05-21T00:00:00+00:00", ingested_by="cli",
    )
    assert records
    assert all(r.file_type == "md" for r in records)


def test_chunk_propagates_updated_at() -> None:
    chunker = DocumentChunker()
    ts = "2026-05-21T12:34:56+00:00"
    records = chunker.chunk(
        "Hello world.", "doc1", "/tmp/doc1.md",
        file_type="md", updated_at=ts, ingested_by="cli",
    )
    assert records
    assert all(r.updated_at == ts for r in records)


@pytest.mark.parametrize("ingested_by", ["cli", "http", "watcher", "reindex"])
def test_chunk_propagates_ingested_by(ingested_by: str) -> None:
    chunker = DocumentChunker()
    records = chunker.chunk(
        "Hello world.", "doc1", "/tmp/doc1.md",
        file_type="md", updated_at="2026-05-21T00:00:00+00:00",
        ingested_by=ingested_by,  # type: ignore[arg-type]
    )
    assert records
    assert all(r.ingested_by == ingested_by for r in records)


def test_chunk_file_type_lowercase_md() -> None:
    chunker = DocumentChunker()
    records = chunker.chunk(
        "Hello world.", "doc1", "/tmp/doc1.md",
        file_type="md", updated_at="", ingested_by="cli",
    )
    assert all(r.file_type == "md" for r in records)


def test_chunk_file_type_empty_for_no_extension() -> None:
    chunker = DocumentChunker()
    records = chunker.chunk(
        "Hello world.", "doc1", "/tmp/Makefile",
        file_type="", updated_at="", ingested_by="cli",
    )
    assert all(r.file_type == "" for r in records)


def test_chunk_requires_keyword_args() -> None:
    """Positional supply of the new args must raise TypeError."""
    chunker = DocumentChunker()
    with pytest.raises(TypeError):
        chunker.chunk(  # type: ignore[call-arg,misc]
            "Hello world.", "doc1", "/tmp/doc1.md",
            "md", "2026-05-21T00:00:00+00:00", "cli",
        )


# ---------------------------------------------------------------------------
# Task 6.1 — language kwarg propagation
# ---------------------------------------------------------------------------


def test_chunk_language_propagated() -> None:
    """language='fr' is set on every returned ChunkRecord."""
    chunker = DocumentChunker()
    records = chunker.chunk("Bonjour le monde.", "doc1", "/tmp/doc1.md", language="fr", **_DEFAULT_KW)
    assert records
    assert all(r.language == "fr" for r in records)


def test_chunk_language_defaults_to_empty() -> None:
    """Omitting language kwarg results in language='' on every ChunkRecord."""
    chunker = DocumentChunker()
    records = chunker.chunk("Hello world.", "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert records
    assert all(r.language == "" for r in records)


def test_chunk_language_unknown() -> None:
    """language='unknown' is set on every returned ChunkRecord."""
    chunker = DocumentChunker()
    records = chunker.chunk("Some ambiguous text.", "doc1", "/tmp/doc1.md", language="unknown", **_DEFAULT_KW)
    assert records
    assert all(r.language == "unknown" for r in records)


# ---------------------------------------------------------------------------
# Task 1.2 — character offsets propagated from chonkie to ChunkRecord
# ---------------------------------------------------------------------------


def test_chunk_offsets_populated() -> None:
    """Every returned record has start_offset >= 0 and end_offset > start_offset."""
    chunker = DocumentChunker()
    text = "Hello world. This is a test sentence for offset verification."
    records = chunker.chunk(text, "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert records, "expected at least one chunk"
    for r in records:
        assert r.start_offset >= 0, f"start_offset should be >= 0, got {r.start_offset}"
        assert r.end_offset > r.start_offset, (
            f"end_offset ({r.end_offset}) should be > start_offset ({r.start_offset})"
        )


def test_chunk_offset_text_slice_matches() -> None:
    """text[record.start_offset:record.end_offset] == record.text for all non-empty chunks."""
    chunker = DocumentChunker(chunk_size=64)
    text = _LONG_TEXT
    records = chunker.chunk(text, "doc1", "/tmp/doc1.md", **_DEFAULT_KW)
    assert records, "expected at least one chunk"
    for r in records:
        if r.text:
            sliced = text[r.start_offset : r.end_offset]
            assert sliced == r.text, (
                f"text slice [{r.start_offset}:{r.end_offset}] = {sliced!r} "
                f"does not match chunk.text = {r.text!r}"
            )


# ---------------------------------------------------------------------------
# BE-6 — ASTChunker (E2g task 5.1)
# ---------------------------------------------------------------------------


def test_astChunker_splitsOnFunctionBoundary() -> None:
    """A function boundary becomes a chunk boundary — no chunk straddles two scopes."""
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")

    from archon_search.chunker import ASTChunker
    from archon_search.code_enricher import CodeEnricher

    source = (
        "def foo():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def bar():\n"
        "    return 2\n"
    )
    scope_table = CodeEnricher().prepare(source, ".py", Path("/tmp/mod.py"), None)
    assert scope_table, "tree-sitter grammar must be available for this test"
    bar_scope = next(e for e in scope_table if e.fn_name == "bar")

    chunker = ASTChunker(chunk_size=3)
    records = chunker.chunk(
        source, "doc1", "/tmp/mod.py", scope_table=scope_table, **_DEFAULT_KW
    )
    assert records
    assert any(r.start_offset == bar_scope.start for r in records), (
        "expected a chunk to start exactly at the second function's boundary"
    )


def test_astChunker_mergesSmallScopesToBudget() -> None:
    """Small adjacent top-level scopes merge into one chunk under a generous budget."""
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")

    from archon_search.chunker import ASTChunker
    from archon_search.code_enricher import CodeEnricher

    source = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    scope_table = CodeEnricher().prepare(source, ".py", Path("/tmp/mod.py"), None)
    assert scope_table, "tree-sitter grammar must be available for this test"

    chunker = ASTChunker(chunk_size=512)
    records = chunker.chunk(
        source, "doc1", "/tmp/mod.py", scope_table=scope_table, **_DEFAULT_KW
    )
    assert len(records) == 1, "both tiny functions should merge into a single chunk"
    assert "def a" in records[0].text
    assert "def b" in records[0].text


def test_astChunker_fallsBackWhenTreeSitterAbsent() -> None:
    """An empty scope_table (tree-sitter unavailable/parse failed) falls back to token chunking."""
    from archon_search.chunker import ASTChunker

    chunker = ASTChunker(chunk_size=64)
    records = chunker.chunk(
        "def foo():\n    return 1\n",
        "doc1",
        "/tmp/mod.py",
        scope_table=[],
        **_DEFAULT_KW,
    )
    assert records
    assert all(isinstance(r, ChunkRecord) for r in records)
    assert all(r.chunk_id == "" for r in records)
    assert all(r.vector == [] for r in records)


def test_astChunker_mergeStopsAtBudgetCeiling() -> None:
    """Merge loop merges under budget but flushes/starts a new segment on overflow.

    Three small top-level functions with a chunk_size small enough that not
    all three fit in one chunk, but large enough that at least two merge —
    proves both halves of the merge loop: merge-when-under-budget and
    flush-on-would-exceed-budget.
    """
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")

    from archon_search.chunker import ASTChunker
    from archon_search.code_enricher import CodeEnricher

    source = (
        "def a():\n    return 1\n\n\n"
        "def b():\n    return 2\n\n\n"
        "def c():\n    return 3\n"
    )
    scope_table = CodeEnricher().prepare(source, ".py", Path("/tmp/mod.py"), None)
    assert scope_table, "tree-sitter grammar must be available for this test"

    chunker = ASTChunker(chunk_size=8)
    records = chunker.chunk(
        source, "doc1", "/tmp/mod.py", scope_table=scope_table, **_DEFAULT_KW
    )

    assert len(records) > 1, "expected the overflow-flush branch to fire (not all merged into one)"
    assert any(
        sum(1 for marker in ("def a", "def b", "def c") if marker in r.text) > 1
        for r in records
    ), "expected at least one chunk to contain more than one function (merge did occur)"


def test_astChunker_oversizedScope_subSplitsWithCorrectOffsets() -> None:
    """A single scope whose body alone exceeds the budget sub-splits into multiple chunks.

    Every returned record's start/end offsets must slice out exactly its own
    text from the ORIGINAL source — proves the `b_start + sub.start_index`
    re-offsetting math in the sub-split branch is correct.
    """
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")

    from archon_search.chunker import ASTChunker
    from archon_search.code_enricher import CodeEnricher

    body_lines = "\n".join(f"    x{i} = {i}" for i in range(80))
    source = f"def big():\n{body_lines}\n    return x0\n"
    scope_table = CodeEnricher().prepare(source, ".py", Path("/tmp/mod.py"), None)
    assert scope_table, "tree-sitter grammar must be available for this test"

    chunker = ASTChunker(chunk_size=10)
    records = chunker.chunk(
        source, "doc1", "/tmp/mod.py", scope_table=scope_table, **_DEFAULT_KW
    )

    assert len(records) > 1, "expected the oversized single-scope sub-split branch to fire"
    for r in records:
        sliced = source[r.start_offset:r.end_offset]
        assert sliced == r.text, (
            f"text slice [{r.start_offset}:{r.end_offset}] = {sliced!r} "
            f"does not match chunk.text = {r.text!r}"
        )


def test_astChunker_typeScriptSplitsOnFunctionBoundary() -> None:
    """A non-Python language (TypeScript) also produces a boundary-aligned split."""
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_typescript")

    from archon_search.chunker import ASTChunker
    from archon_search.code_enricher import CodeEnricher

    source = (
        "function topFn() {\n"
        "    return 1;\n"
        "}\n"
        "\n"
        "function otherFn() {\n"
        "    return 2;\n"
        "}\n"
    )
    scope_table = CodeEnricher().prepare(source, ".ts", Path("/tmp/mod.ts"), None)
    assert scope_table, "tree-sitter-typescript grammar must be available for this test"
    other_scope = next(e for e in scope_table if e.fn_name == "otherFn")

    chunker = ASTChunker(chunk_size=3)
    records = chunker.chunk(
        source, "doc1", "/tmp/mod.ts", scope_table=scope_table, **_DEFAULT_KW
    )
    assert records
    assert any(r.start_offset == other_scope.start for r in records), (
        "expected a chunk to start exactly at the second function's boundary"
    )
