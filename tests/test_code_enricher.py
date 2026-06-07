"""Tests for archon_search.code_enricher — C3c code symbol context enrichment.

Tests are organized by task/phase matching the C3c plan:
- Task 1.2: ScopeEntry, ScopeTable, CODE_EXTENSIONS
- Task 2.1: _module_path helper (class TestModulePath)
- Task 3.1: Grammar registry (class TestGrammarRegistry)
- Task 4.1/4.2: Fixture contracts (class TestFixtureContracts)
- Task 5.1: Scope table builder (TestBuildScopeTablePython, TestBuildScopeTableTypeScript)
- Task 6.2: _resolve_scope (TestResolveScope), _lang_label (TestLangLabel), enrich_chunk (TestEnrichChunk)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Task 1.2 — ScopeEntry, ScopeTable, CODE_EXTENSIONS
# ---------------------------------------------------------------------------


def test_scope_entry_fields():
    """ScopeEntry must be accessible by all five named fields."""
    from archon_search.code_enricher import ScopeEntry

    entry = ScopeEntry(start=10, end=50, symbol_type="function", fn_name="foo", class_name="")
    assert entry.start == 10
    assert entry.end == 50
    assert entry.symbol_type == "function"
    assert entry.fn_name == "foo"
    assert entry.class_name == ""


def test_code_extensions_contains_mandatory():
    """Both .py and .ts must be in CODE_EXTENSIONS (mandatory languages)."""
    from archon_search.code_enricher import CODE_EXTENSIONS

    assert ".py" in CODE_EXTENSIONS
    assert ".ts" in CODE_EXTENSIONS


def test_code_extensions_does_not_contain_markdown():
    """.md, .txt, .json must NOT be in CODE_EXTENSIONS."""
    from archon_search.code_enricher import CODE_EXTENSIONS

    assert ".md" not in CODE_EXTENSIONS
    assert ".txt" not in CODE_EXTENSIONS
    assert ".json" not in CODE_EXTENSIONS
