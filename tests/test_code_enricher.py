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

import pytest

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


# ---------------------------------------------------------------------------
# Task 2.1 — _module_path helper
# ---------------------------------------------------------------------------


class TestModulePath:
    """Tests for the _module_path(file_path, collection_root) -> str helper."""

    def test_regular_python(self):
        """/repo/archon_search/store.py with root /repo -> 'archon_search.store'."""
        from pathlib import Path

        from archon_search.code_enricher import _module_path

        result = _module_path(Path("/repo/archon_search/store.py"), Path("/repo"))
        assert result == "archon_search.store"

    def test_init_py(self):
        """/repo/archon_search/jobs/__init__.py with root /repo -> 'archon_search.jobs'."""
        from pathlib import Path

        from archon_search.code_enricher import _module_path

        result = _module_path(Path("/repo/archon_search/jobs/__init__.py"), Path("/repo"))
        assert result == "archon_search.jobs"

    def test_dts(self):
        """/repo/types/lib.d.ts with root /repo -> 'types.lib.d' (one extension stripped)."""
        from pathlib import Path

        from archon_search.code_enricher import _module_path

        result = _module_path(Path("/repo/types/lib.d.ts"), Path("/repo"))
        assert result == "types.lib.d"

    def test_hyphenated_python(self):
        """/repo/my-pkg/mod.py with root /repo -> 'my_pkg.mod' (hyphens to underscores for .py)."""
        from pathlib import Path

        from archon_search.code_enricher import _module_path

        result = _module_path(Path("/repo/my-pkg/mod.py"), Path("/repo"))
        assert result == "my_pkg.mod"

    def test_hyphenated_non_python(self):
        """/repo/my-pkg/mod.ts with root /repo -> 'my-pkg.mod' (no substitution for non-.py)."""
        from pathlib import Path

        from archon_search.code_enricher import _module_path

        result = _module_path(Path("/repo/my-pkg/mod.ts"), Path("/repo"))
        assert result == "my-pkg.mod"

    def test_no_collection_root(self):
        """Path('/tmp/foo.py') with None collection_root -> 'foo' (stem only)."""
        from pathlib import Path

        from archon_search.code_enricher import _module_path

        result = _module_path(Path("/tmp/foo.py"), None)
        assert result == "foo"

    def test_index_ts(self):
        """/repo/web/api/index.ts with root /repo -> 'web.api' (index dropped for .ts)."""
        from pathlib import Path

        from archon_search.code_enricher import _module_path

        result = _module_path(Path("/repo/web/api/index.ts"), Path("/repo"))
        assert result == "web.api"

    def test_index_js(self):
        """/repo/web/api/index.js with root /repo -> 'web.api' (index dropped for .js)."""
        from pathlib import Path

        from archon_search.code_enricher import _module_path

        result = _module_path(Path("/repo/web/api/index.js"), Path("/repo"))
        assert result == "web.api"

    def test_index_dts(self):
        """/repo/web/index.d.ts with root /repo -> 'web.index.d'.

        Step 7 does NOT fire because after with_suffix(""), the last segment
        is "index.d" not "index".
        """
        from pathlib import Path

        from archon_search.code_enricher import _module_path

        result = _module_path(Path("/repo/web/index.d.ts"), Path("/repo"))
        assert result == "web.index.d"

    def test_init_at_root(self):
        """_module_path(Path('/repo/__init__.py'), Path('/repo')) -> ''.

        The algorithm: rel = __init__.py -> with_suffix('') = __init__
        -> parts = ('__init__',) -> joined = '__init__' -> step 5 drops -> ''.
        This is the correct value for a root package init file.
        """
        from pathlib import Path

        from archon_search.code_enricher import _module_path

        result = _module_path(Path("/repo/__init__.py"), Path("/repo"))
        assert result == ""


# ---------------------------------------------------------------------------
# Task 3.1 — Grammar registry with lazy loading and graceful degradation
# ---------------------------------------------------------------------------


class TestGrammarRegistry:
    """Tests for _get_grammar(ext) -> Language | None."""

    @pytest.fixture(autouse=True)
    def _clear_grammar_state(self, monkeypatch):
        """Isolate each test by clearing the module-level grammar cache and logged set."""
        import archon_search.code_enricher as ce

        # Patch both singletons with fresh dicts/sets for the duration of the test
        monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
        monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())

    def test_grammar_returns_none_for_unknown_ext(self):
        """_get_grammar('.xyz') must return None without raising."""
        from archon_search.code_enricher import _get_grammar

        result = _get_grammar(".xyz")
        assert result is None

    def test_grammar_returns_none_when_import_fails(self, monkeypatch, caplog):
        """When tree_sitter_python is absent, _get_grammar('.py') returns None and logs INFO."""
        import logging
        import sys

        import archon_search.code_enricher as ce

        # Make tree_sitter_python appear missing
        monkeypatch.setitem(sys.modules, "tree_sitter_python", None)

        with caplog.at_level(logging.INFO, logger="archon_search.code_enricher"):
            result = ce._get_grammar(".py")

        assert result is None
        assert any(
            "tree-sitter grammar not available for .py" in r.message
            for r in caplog.records
        )

    def test_grammar_info_logged_once(self, monkeypatch, caplog):
        """INFO is emitted exactly once for a missing grammar, even on repeated calls."""
        import logging
        import sys

        import archon_search.code_enricher as ce

        monkeypatch.setitem(sys.modules, "tree_sitter_python", None)

        with caplog.at_level(logging.INFO, logger="archon_search.code_enricher"):
            ce._get_grammar(".py")
            ce._get_grammar(".py")  # second call should hit cache, no new log

        info_msgs = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO
            and "tree-sitter grammar not available for .py" in r.message
        ]
        assert len(info_msgs) == 1

    def test_grammar_result_cached(self, monkeypatch):
        """Calling _get_grammar twice returns the same object from cache (no re-import)."""
        import archon_search.code_enricher as ce

        first = ce._get_grammar(".py")
        # Verify the result is in the cache
        assert ".py" in ce._GRAMMAR_CACHE
        assert ce._GRAMMAR_CACHE[".py"] is first

        second = ce._get_grammar(".py")
        # Should return cached value without re-computing
        assert second is first
