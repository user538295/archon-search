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

from unittest.mock import MagicMock

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
        """When tree_sitter_python is absent, _get_grammar('.py') returns None and logs WARNING."""
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

    def test_grammar_warning_logged_once(self, monkeypatch, caplog):
        """WARNING is emitted exactly once for a missing grammar, even on repeated calls."""
        import logging
        import sys

        import archon_search.code_enricher as ce

        monkeypatch.setitem(sys.modules, "tree_sitter_python", None)

        with caplog.at_level(logging.WARNING, logger="archon_search.code_enricher"):
            ce._get_grammar(".py")
            ce._get_grammar(".py")  # second call should hit cache, no new log

        warning_msgs = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "tree-sitter grammar not available for .py" in r.message
        ]
        assert len(warning_msgs) == 1

    def test_codeParsersMissing_logsWarningOnce(self, monkeypatch, caplog):
        """The missing-parser WARNING logs exactly once across two distinct files
        sharing the same extension — not once per file.

        This is a stronger version of test_grammar_warning_logged_once: that test
        only calls _get_grammar(".py") twice for the SAME extension in the same
        context, which doesn't prove "not per file". This test simulates two
        distinct source files (different content/paths) both hitting the
        missing-grammar path via CodeEnricher.prepare(), proving the warning is
        keyed on extension, not on file identity.
        """
        import logging
        import sys
        from pathlib import Path

        import archon_search.code_enricher as ce
        from archon_search.code_enricher import CodeEnricher

        monkeypatch.setitem(sys.modules, "tree_sitter_python", None)

        file_a = Path("/repo/module_a.py")
        file_b = Path("/repo/module_b.py")
        source_a = "def foo():\n    pass\n"
        source_b = "class Bar:\n    pass\n"

        with caplog.at_level(logging.WARNING, logger="archon_search.code_enricher"):
            enricher_a = CodeEnricher()
            enricher_a.prepare(source_a, ".py", file_a, None)

            enricher_b = CodeEnricher()
            enricher_b.prepare(source_b, ".py", file_b, None)

        warning_msgs = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "tree-sitter grammar not available for .py" in r.message
        ]
        assert len(warning_msgs) == 1
        assert ce.has_missing_code_parsers() is True

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


# ---------------------------------------------------------------------------
# Task 4.1 — Python fixture contracts
# ---------------------------------------------------------------------------


class TestFixtureContracts:
    """Contracts that the fixture files must satisfy."""

    PYTHON_FIXTURE = "tests/fixtures/code/python/sample.py"

    def _read_python_fixture(self):
        from pathlib import Path

        return Path(self.PYTHON_FIXTURE).read_text()

    def test_python_fixture_has_outer_class(self):
        """Python fixture must contain 'class Outer'."""
        content = self._read_python_fixture()
        assert "class Outer" in content

    def test_python_fixture_has_nested_inner(self):
        """Python fixture must contain 'class Inner'."""
        content = self._read_python_fixture()
        assert "class Inner" in content

    def test_python_fixture_has_decorated_fn(self):
        """Python fixture must contain '@some_decorator'."""
        content = self._read_python_fixture()
        assert "@some_decorator" in content

    def test_python_fixture_has_top_fn(self):
        """Python fixture must contain 'def top_fn'."""
        content = self._read_python_fixture()
        assert "def top_fn" in content

    def test_python_fixture_is_valid_python(self):
        """Python fixture must be valid Python (ast.parse must not raise)."""
        import ast

        content = self._read_python_fixture()
        ast.parse(content)  # raises SyntaxError if invalid

    # --- TypeScript fixture contracts (Task 4.2) ---

    TS_FIXTURE = "tests/fixtures/code/typescript/sample.ts"

    def _read_ts_fixture(self):
        from pathlib import Path

        return Path(self.TS_FIXTURE).read_text()

    def test_ts_fixture_has_top_fn(self):
        """TypeScript fixture must contain 'topFn'."""
        content = self._read_ts_fixture()
        assert "topFn" in content

    def test_ts_fixture_has_class(self):
        """TypeScript fixture must contain 'class MyClass'."""
        content = self._read_ts_fixture()
        assert "class MyClass" in content

    def test_ts_fixture_has_arrow_fn(self):
        """TypeScript fixture must contain 'arrowFn'."""
        content = self._read_ts_fixture()
        assert "arrowFn" in content


# ---------------------------------------------------------------------------
# Task 5.1 — Scope table builder: Python
# ---------------------------------------------------------------------------


class TestBuildScopeTablePython:
    """Tests for _build_scope_table with the Python fixture."""

    @pytest.fixture(autouse=True)
    def _load_grammar(self):
        from archon_search.code_enricher import _get_grammar

        self.lang = _get_grammar(".py")
        if self.lang is None:
            pytest.skip("tree-sitter-python grammar not installed")

    @pytest.fixture(autouse=True)
    def _load_source(self):
        from pathlib import Path

        self.source = Path("tests/fixtures/code/python/sample.py").read_text()

    def _build(self):
        from archon_search.code_enricher import _build_scope_table

        return _build_scope_table(self.source, self.lang, ".py")

    def test_top_fn_entry(self):
        """A ScopeEntry with symbol_type='function' and fn_name='top_fn' must be present."""
        scope_table = self._build()
        assert any(
            e.symbol_type == "function" and e.fn_name == "top_fn"
            for e in scope_table
        )

    def test_outer_method_is_method(self):
        """outer_method must be classified as 'method' with class_name='Outer'."""
        scope_table = self._build()
        assert any(
            e.symbol_type == "method"
            and e.fn_name == "outer_method"
            and e.class_name == "Outer"
            for e in scope_table
        )

    def test_inner_method_class_is_inner(self):
        """inner_method must have class_name='Inner' (innermost class wins)."""
        scope_table = self._build()
        assert any(
            e.fn_name == "inner_method" and e.class_name == "Inner"
            for e in scope_table
        )

    def test_outer_class_entry(self):
        """A ScopeEntry with symbol_type='class' and class_name='Outer' must be present."""
        scope_table = self._build()
        assert any(
            e.symbol_type == "class" and e.class_name == "Outer"
            for e in scope_table
        )

    def test_decorated_fn_start_includes_decorator(self):
        """decorated_fn entry start offset must be <= offset of the @some_decorator line."""
        scope_table = self._build()
        expected_decorator_start = self.source.index("@some_decorator")
        decorated_entry = next(
            (e for e in scope_table if e.fn_name == "decorated_fn"), None
        )
        assert decorated_entry is not None, "decorated_fn entry not found in scope table"
        assert decorated_entry.start <= expected_decorator_start

    def test_non_ascii_offsets(self):
        """Scope entry start must be the character offset, not the byte offset."""
        from archon_search.code_enricher import _build_scope_table

        source = "# café\ndef top_fn(): pass"
        scope_table = _build_scope_table(source, self.lang, ".py")
        assert len(scope_table) > 0
        entry = next((e for e in scope_table if e.fn_name == "top_fn"), None)
        assert entry is not None
        # Character offset of 'def' is 7; byte offset would be 8 (é is 2 UTF-8 bytes)
        expected_char_offset = source.index("def")
        assert entry.start == expected_char_offset
        assert entry.start == 7  # char offset
        assert entry.start != 8  # not byte offset

    def test_scope_table_sorted(self):
        """Scope table entries must be in ascending start order."""
        scope_table = self._build()
        starts = [e.start for e in scope_table]
        assert starts == sorted(starts)


# ---------------------------------------------------------------------------
# Task 5.1 — Scope table builder: TypeScript
# ---------------------------------------------------------------------------


class TestBuildScopeTableTypeScript:
    """Tests for _build_scope_table with the TypeScript fixture."""

    @pytest.fixture(autouse=True)
    def _load_grammar(self):
        from archon_search.code_enricher import _get_grammar

        self.lang = _get_grammar(".ts")
        if self.lang is None:
            pytest.skip("tree-sitter-typescript grammar not installed")

    @pytest.fixture(autouse=True)
    def _load_source(self):
        from pathlib import Path

        self.source = Path("tests/fixtures/code/typescript/sample.ts").read_text()

    def _build(self):
        from archon_search.code_enricher import _build_scope_table

        return _build_scope_table(self.source, self.lang, ".ts")

    def test_ts_top_fn(self):
        """topFn scope entry must be present with symbol_type='function'."""
        scope_table = self._build()
        assert any(
            e.symbol_type == "function" and e.fn_name == "topFn"
            for e in scope_table
        )

    def test_ts_class_method(self):
        """myMethod entry must have symbol_type='method' and class_name='MyClass'."""
        scope_table = self._build()
        assert any(
            e.symbol_type == "method"
            and e.fn_name == "myMethod"
            and e.class_name == "MyClass"
            for e in scope_table
        )

    def test_ts_arrow_fn_not_captured_as_scope(self):
        """Arrow functions must NOT appear as scope entries (no fn_name='arrowFn')."""
        scope_table = self._build()
        assert not any(
            e.fn_name == "arrowFn"
            for e in scope_table
        )


# ---------------------------------------------------------------------------
# Task 6.1 — CodeEnricher.prepare()
# ---------------------------------------------------------------------------


class TestCodeEnricherPrepare:
    """Tests for CodeEnricher.prepare()."""

    PYTHON_SOURCE = None

    @pytest.fixture(autouse=True)
    def _load_python_source(self):
        from pathlib import Path

        TestCodeEnricherPrepare.PYTHON_SOURCE = Path(
            "tests/fixtures/code/python/sample.py"
        ).read_text()

    @pytest.fixture(autouse=True)
    def _clear_grammar_state(self, monkeypatch):
        """Isolate grammar cache between tests."""
        import archon_search.code_enricher as ce

        monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
        monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())
        monkeypatch.setattr(ce, "_parse_failure_count", {})

    def test_prepare_returns_scope_table_for_valid_python(self):
        """Valid Python source with grammar available must return non-empty ScopeTable."""
        from pathlib import Path

        from archon_search.code_enricher import CodeEnricher, _get_grammar

        if _get_grammar(".py") is None:
            pytest.skip("tree-sitter-python grammar not installed")

        enricher = CodeEnricher()
        result = enricher.prepare(
            self.PYTHON_SOURCE, ".py", Path("/repo/pkg/mod.py"), Path("/repo")
        )
        assert isinstance(result, list)
        assert len(result) > 0

    def test_prepare_returns_empty_for_missing_grammar(self, monkeypatch):
        """When grammar is unavailable, prepare() must return [] without raising."""
        from pathlib import Path

        import archon_search.code_enricher as ce
        from archon_search.code_enricher import CodeEnricher

        # Force _get_grammar to always return None
        monkeypatch.setattr(ce, "_get_grammar", lambda ext: None)

        enricher = CodeEnricher()
        result = enricher.prepare(
            self.PYTHON_SOURCE, ".py", Path("/repo/pkg/mod.py"), Path("/repo")
        )
        assert result == []

    def test_prepare_returns_empty_on_parse_failure(self, monkeypatch):
        """Catastrophic scope-builder failure must return [] (WARNING logged)."""
        from pathlib import Path

        import archon_search.code_enricher as ce
        from archon_search.code_enricher import CodeEnricher, _get_grammar

        if _get_grammar(".py") is None:
            pytest.skip("tree-sitter-python grammar not installed")

        monkeypatch.setattr(ce, "_build_scope_table", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("mock crash")))

        enricher = CodeEnricher()
        result = enricher.prepare(
            self.PYTHON_SOURCE, ".py", Path("/repo/pkg/mod.py"), Path("/repo")
        )
        assert result == []

    def test_prepare_sets_module_path(self):
        """prepare() must store _module_path_value for consumption by enrich_chunk()."""
        from pathlib import Path

        from archon_search.code_enricher import CodeEnricher

        enricher = CodeEnricher()
        enricher.prepare(
            self.PYTHON_SOURCE, ".py", Path("/repo/pkg/mod.py"), Path("/repo")
        )
        assert enricher._module_path_value == "pkg.mod"

    def test_warning_logged_on_parse_failure(self, monkeypatch, caplog):
        """A WARNING must be emitted when _build_scope_table raises."""
        import logging
        from pathlib import Path

        import archon_search.code_enricher as ce
        from archon_search.code_enricher import CodeEnricher, _get_grammar

        if _get_grammar(".py") is None:
            pytest.skip("tree-sitter-python grammar not installed")

        monkeypatch.setattr(
            ce, "_build_scope_table",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash")),
        )

        enricher = CodeEnricher()
        with caplog.at_level(logging.WARNING, logger="archon_search.code_enricher"):
            enricher.prepare(
                self.PYTHON_SOURCE, ".py", Path("/repo/pkg/mod.py"), Path("/repo")
            )

        assert any(
            r.levelno == logging.WARNING and "tree-sitter parse failed" in r.message
            for r in caplog.records
        )

    def test_prepare_sets_ext(self, monkeypatch):
        """prepare() must store _ext on the instance for use by enrich_chunk()."""
        from pathlib import Path

        import archon_search.code_enricher as ce
        from archon_search.code_enricher import CodeEnricher

        # Grammar not needed — _ext is set before the grammar check
        monkeypatch.setattr(ce, "_get_grammar", lambda ext: None)

        enricher = CodeEnricher()
        enricher.prepare(
            self.PYTHON_SOURCE, ".py", Path("/repo/pkg/mod.py"), Path("/repo")
        )
        assert enricher._ext == ".py"

    def test_prepare_warning_cap_downgraded_to_debug_after_k10(self, monkeypatch, caplog):
        """After K=10 parse failures for an extension, WARNINGs must be downgraded to DEBUG."""
        import logging
        from pathlib import Path

        import archon_search.code_enricher as ce
        from archon_search.code_enricher import CodeEnricher, _get_grammar

        if _get_grammar(".py") is None:
            pytest.skip("tree-sitter-python grammar not installed")

        monkeypatch.setattr(
            ce, "_build_scope_table",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash")),
        )
        # Pre-load the count to K=10 so the next call tips over the cap
        monkeypatch.setattr(ce, "_parse_failure_count", {".py": 10})

        enricher = CodeEnricher()
        with caplog.at_level(logging.DEBUG, logger="archon_search.code_enricher"):
            enricher.prepare(
                self.PYTHON_SOURCE, ".py", Path("/repo/pkg/mod.py"), Path("/repo")
            )

        # At count=11 (one above cap), must be DEBUG not WARNING
        assert not any(
            r.levelno == logging.WARNING and "tree-sitter parse failed" in r.message
            for r in caplog.records
        ), "Expected WARNING to be downgraded to DEBUG after K=10"
        assert any(
            r.levelno == logging.DEBUG and "tree-sitter parse failed" in r.message
            for r in caplog.records
        ), "Expected DEBUG log for failure after K=10 cap"

    def test_prepare_handles_tree_sitter_error_nodes(self, caplog):
        """Broken syntax (ERROR nodes) must not abort scope-table construction or log WARNING."""
        import logging
        from pathlib import Path

        from archon_search.code_enricher import CodeEnricher, _get_grammar

        if _get_grammar(".py") is None:
            pytest.skip("tree-sitter-python grammar not installed")

        broken_source = "def foo(x,: pass\ndef bar(): return 1"
        enricher = CodeEnricher()

        with caplog.at_level(logging.DEBUG, logger="archon_search.code_enricher"):
            result = enricher.prepare(
                broken_source, ".py", Path("/repo/pkg/mod.py"), Path("/repo")
            )

        # Must not raise and must return a list
        assert isinstance(result, list)
        # No WARNING must be emitted — ERROR nodes are handled gracefully, not as a crash
        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert warnings == [], f"Unexpected WARNINGs: {[r.message for r in warnings]}"


# ---------------------------------------------------------------------------
# Task 6.2 — _resolve_scope, _lang_label, enrich_chunk
# ---------------------------------------------------------------------------


class TestResolveScope:
    """Tests for _resolve_scope(offset, scope_table) -> ScopeEntry | None.

    All tests use synthetic scope tables — no fixture file dependency.
    """

    def _make_entry(self, start, end, symbol_type="function", fn_name="fn", class_name=""):
        from archon_search.code_enricher import ScopeEntry

        return ScopeEntry(
            start=start,
            end=end,
            symbol_type=symbol_type,
            fn_name=fn_name,
            class_name=class_name,
        )

    def _resolve(self, offset, scope_table):
        from archon_search.code_enricher import _resolve_scope

        return _resolve_scope(offset, scope_table)

    def test_offset_inside_scope(self):
        """Offset 25 inside scope [10, 50) must return that entry."""
        entry = self._make_entry(10, 50)
        assert self._resolve(25, [entry]) is entry

    def test_offset_exactly_at_start(self):
        """Offset exactly at start (10) must return the entry (inclusive)."""
        entry = self._make_entry(10, 50)
        assert self._resolve(10, [entry]) is entry

    def test_offset_at_end_exclusive_no_next_scope(self):
        """Offset at end boundary (50) for a single scope [10, 50) must return None."""
        entry = self._make_entry(10, 50)
        assert self._resolve(50, [entry]) is None

    def test_offset_at_end_exclusive_with_adjacent_scope(self):
        """Offset 50 must match the second scope [50, 90), not the first [10, 50)."""
        e1 = self._make_entry(10, 50, fn_name="first")
        e2 = self._make_entry(50, 90, fn_name="second")
        result = self._resolve(50, [e1, e2])
        assert result is e2

    def test_offset_before_all_scopes(self):
        """Offset 0 before first scope starting at 10 must return None."""
        entry = self._make_entry(10, 50)
        assert self._resolve(0, [entry]) is None

    def test_offset_after_all_scopes(self):
        """Offset 100 after last scope ending at 80 must return None."""
        entry = self._make_entry(10, 80)
        assert self._resolve(100, [entry]) is None

    def test_nested_scopes_innermost_wins(self):
        """When outer [0,100) and inner [20,60) both contain offset 30, inner wins."""
        from archon_search.code_enricher import ScopeEntry

        outer = ScopeEntry(0, 100, "class", "", "MyClass")
        inner = ScopeEntry(20, 60, "method", "myMethod", "MyClass")
        # Sorted (start ASC, end DESC): outer at idx 0, inner at idx 1
        scope_table = sorted([outer, inner], key=lambda e: (e.start, -e.end))
        result = self._resolve(30, scope_table)
        assert result is inner

    def test_module_gap_between_scopes(self):
        """Offset 30 in the gap between [0,20) and [40,60) must return None."""
        e1 = self._make_entry(0, 20, fn_name="first")
        e2 = self._make_entry(40, 60, fn_name="second")
        assert self._resolve(30, [e1, e2]) is None

    def test_single_entry_scope_table(self):
        """Degenerate case: single entry; offset inside it returns it."""
        entry = self._make_entry(5, 100)
        assert self._resolve(50, [entry]) is entry

    def test_same_start_tiebreaker(self):
        """Two scopes with same start: inner (smallest end) must be returned.

        ScopeEntry(start=10, end=100, class) and ScopeEntry(start=10, end=50, method).
        For offset 25, the method (end=50) should be returned (innermost scope).
        Sorted (start ASC, end DESC): class first, method second.
        Backward walk hits method first.
        """
        from archon_search.code_enricher import ScopeEntry

        outer = ScopeEntry(10, 100, "class", "", "MyClass")
        inner = ScopeEntry(10, 50, "method", "myMethod", "MyClass")
        # Correct sort order: (10, -100), (10, -50) → outer first, inner second
        scope_table = sorted([outer, inner], key=lambda e: (e.start, -e.end))
        assert scope_table[0] is outer
        assert scope_table[1] is inner
        result = self._resolve(25, scope_table)
        assert result is inner


class TestLangLabel:
    """Tests for _lang_label(ext) -> str."""

    def _label(self, ext):
        from archon_search.code_enricher import _lang_label

        return _lang_label(ext)

    def test_lang_label_python(self):
        assert self._label(".py") == "python"

    def test_lang_label_typescript(self):
        assert self._label(".ts") == "typescript"

    def test_lang_label_javascript(self):
        assert self._label(".js") == "javascript"

    def test_lang_label_go(self):
        assert self._label(".go") == "go"

    def test_lang_label_rust(self):
        assert self._label(".rs") == "rust"

    def test_lang_label_java(self):
        assert self._label(".java") == "java"

    def test_lang_label_bash(self):
        """sh extension must map to 'bash', not 'sh'."""
        assert self._label(".sh") == "bash"

    def test_lang_label_unknown_ext(self):
        """Unknown extension must raise KeyError (documented behavior)."""
        with pytest.raises(KeyError):
            self._label(".xyz")


class TestEnrichChunk:
    """Tests for CodeEnricher.enrich_chunk().

    Offsets are computed dynamically from fixture content to stay resilient
    to fixture edits.
    """

    PYTHON_FIXTURE_PATH = "tests/fixtures/code/python/sample.py"
    TS_FIXTURE_PATH = "tests/fixtures/code/typescript/sample.ts"

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        from pathlib import Path

        import archon_search.code_enricher as ce

        # Clear grammar state
        monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
        monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())
        monkeypatch.setattr(ce, "_parse_failure_count", {})

        self.py_source = Path(self.PYTHON_FIXTURE_PATH).read_text()
        self.ts_source = Path(self.TS_FIXTURE_PATH).read_text()

    def _make_chunk(self, start, end=-1):
        """Create a minimal ChunkRecord with the given start/end offsets."""
        from archon_search._types import ChunkRecord

        c = ChunkRecord(
            doc_id="test",
            chunk_id="test-000000",
            text="",
            vector=[],
            source_path="/repo/pkg/mod.py",
            indexed_at="2024-01-01T00:00:00.000000Z",
            start_offset=start,
            end_offset=end if end >= 0 else start + 1,
        )
        return c

    def _prepare_py_enricher(self):
        from pathlib import Path

        from archon_search.code_enricher import CodeEnricher, _get_grammar

        if _get_grammar(".py") is None:
            pytest.skip("tree-sitter-python grammar not installed")

        enricher = CodeEnricher()
        scope_table = enricher.prepare(
            self.py_source, ".py", Path("/repo/pkg/mod.py"), Path("/repo")
        )
        return enricher, scope_table

    def _prepare_ts_enricher(self):
        from pathlib import Path

        from archon_search.code_enricher import CodeEnricher, _get_grammar

        if _get_grammar(".ts") is None:
            pytest.skip("tree-sitter-typescript grammar not installed")

        enricher = CodeEnricher()
        scope_table = enricher.prepare(
            self.ts_source, ".ts", Path("/repo/pkg/mod.ts"), Path("/repo")
        )
        return enricher, scope_table

    def test_enrich_top_fn_chunk(self):
        """Chunk inside top_fn body must have symbol_type='function', fn_name='top_fn'."""
        enricher, scope_table = self._prepare_py_enricher()
        body_offset = self.py_source.index("return 42")
        chunk = self._make_chunk(body_offset)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert result["_symbol_type"] == "function"
        assert result["_containing_function"] == "top_fn"
        assert result["_containing_class"] == ""

    def test_enrich_outer_method_chunk(self):
        """Chunk inside outer_method must be method with class_name='Outer'."""
        enricher, scope_table = self._prepare_py_enricher()
        body_offset = self.py_source.index("return self.class_attr")
        chunk = self._make_chunk(body_offset)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert result["_symbol_type"] == "method"
        assert result["_containing_function"] == "outer_method"
        assert result["_containing_class"] == "Outer"

    def test_enrich_class_body_chunk(self):
        """Chunk in Outer class body (before first method) must have symbol_type='class'."""
        enricher, scope_table = self._prepare_py_enricher()
        # class_attr is defined between 'class Outer:' and 'def outer_method'
        body_offset = self.py_source.index('class_attr = "outer"')
        chunk = self._make_chunk(body_offset)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert result["_symbol_type"] == "class"
        assert result["_containing_class"] == "Outer"
        assert result["_containing_function"] == ""

    def test_enrich_module_level_chunk(self):
        """Chunk in module-level gap (non-empty scope_table) must have symbol_type='module'."""
        enricher, scope_table = self._prepare_py_enricher()
        # MODULE_CONSTANT is defined after the Outer class — module-level code
        body_offset = self.py_source.index("MODULE_CONSTANT")
        chunk = self._make_chunk(body_offset)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert result["_symbol_type"] == "module"
        assert result["_containing_function"] == ""
        assert result["_containing_class"] == ""
        # Must return all 5 keys (distinguishes from empty-scope-table path)
        assert len(result) == 5

    def test_enrich_inner_method_innermost_class_wins(self):
        """Chunk in Inner.inner_method must have class_name='Inner'."""
        enricher, scope_table = self._prepare_py_enricher()
        body_offset = self.py_source.index('return "inner"')
        chunk = self._make_chunk(body_offset)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert result["_containing_class"] == "Inner"
        assert result["_containing_function"] == "inner_method"

    def test_enrich_decorator_chunk(self):
        """Chunk at decorator line must be attributed to decorated_fn."""
        enricher, scope_table = self._prepare_py_enricher()
        decorator_offset = self.py_source.index("@some_decorator\ndef decorated_fn")
        chunk = self._make_chunk(decorator_offset)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert result["_containing_function"] == "decorated_fn"

    def test_enrich_ts_arrow_fn(self):
        """Chunk inside arrow function body must fall through to module-level scope."""
        enricher, scope_table = self._prepare_ts_enricher()
        # 'console.log' is inside the arrow function body — falls to module-level
        body_offset = self.ts_source.index('console.log("arrow")')
        chunk = self._make_chunk(body_offset)
        result = enricher.enrich_chunk(chunk, scope_table)
        # Arrow functions are not captured; chunk resolves to module-level
        assert result["_symbol_type"] == "module"

    def test_enrich_module_path_present(self):
        """Every chunk result must include a '_module_path' key."""
        enricher, scope_table = self._prepare_py_enricher()
        body_offset = self.py_source.index("return 42")
        chunk = self._make_chunk(body_offset)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert "_module_path" in result
        assert result["_module_path"] == "pkg.mod"

    def test_enrich_symbol_subtype_python(self):
        """Python chunk must have _symbol_subtype matching 'python-{symbol_type}'."""
        enricher, scope_table = self._prepare_py_enricher()
        body_offset = self.py_source.index("return 42")
        chunk = self._make_chunk(body_offset)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert result["_symbol_subtype"] == f"python-{result['_symbol_type']}"

    def test_enrich_symbol_subtype_typescript(self):
        """TypeScript chunk must have _symbol_subtype starting with 'typescript-'."""
        enricher, scope_table = self._prepare_ts_enricher()
        body_offset = self.ts_source.index("return x * 2")
        chunk = self._make_chunk(body_offset)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert result["_symbol_subtype"].startswith("typescript-")

    def test_enrich_empty_scope_table_returns_module_path_only(self, monkeypatch):
        """When scope_table is empty (e.g. missing grammar), only _module_path is returned."""
        from pathlib import Path

        import archon_search.code_enricher as ce
        from archon_search.code_enricher import CodeEnricher

        # Force grammar to return None so prepare() returns []
        monkeypatch.setattr(ce, "_get_grammar", lambda ext: None)

        enricher = CodeEnricher()
        scope_table = enricher.prepare(
            self.py_source, ".py", Path("/repo/pkg/mod.py"), Path("/repo")
        )
        assert scope_table == []
        chunk = self._make_chunk(0)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert "_module_path" in result
        assert "_symbol_type" not in result
        assert result["_module_path"] == "pkg.mod"

    def test_enrich_empty_scope_table_no_module_path_returns_empty_dict(self, monkeypatch):
        """When both scope_table and _module_path_value are empty, {} is returned."""
        from pathlib import Path

        import archon_search.code_enricher as ce
        from archon_search.code_enricher import CodeEnricher

        # Force grammar to return None (prepare returns []) and module path is empty
        # (root __init__.py with collection_root produces empty module path)
        monkeypatch.setattr(ce, "_get_grammar", lambda ext: None)

        enricher = CodeEnricher()
        # Using root __init__.py gives empty _module_path_value (see test_init_at_root)
        scope_table = enricher.prepare(
            "# root init\n",
            ".py",
            Path("/repo/__init__.py"),
            Path("/repo"),
        )
        assert scope_table == []
        assert enricher._module_path_value == ""
        chunk = self._make_chunk(0)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert result == {}

    def test_enrich_chunk_negative_offset_treated_as_module_level(self):
        """Chunk with start_offset=-1 must resolve to module-level (no exception)."""
        enricher, scope_table = self._prepare_py_enricher()
        assert len(scope_table) > 0  # ensure scope_table is non-empty
        chunk = self._make_chunk(-1)
        result = enricher.enrich_chunk(chunk, scope_table)
        assert "_symbol_type" in result
        assert result["_symbol_type"] == "module"

    def test_enrich_chunk_unknown_ext_lang_label_fallback(self):
        """When _ext is unknown, _lang_label raises KeyError and fallback ext.lstrip('.') is used."""
        from pathlib import Path

        import archon_search.code_enricher as ce
        from archon_search.code_enricher import CodeEnricher, ScopeEntry

        # Build an enricher with an unknown extension manually
        enricher = CodeEnricher()
        enricher._ext = ".xyz"
        enricher._module_path_value = "pkg.mod"

        # Provide a minimal non-empty scope table so enrich_chunk enters the resolve path
        scope_table = [ScopeEntry(start=0, end=100, symbol_type="function", fn_name="foo", class_name="")]
        chunk = self._make_chunk(10)
        result = enricher.enrich_chunk(chunk, scope_table)

        # Fallback: language = ".xyz".lstrip(".") = "xyz"
        assert result["_symbol_subtype"] == "xyz-function"
        assert result["_symbol_type"] == "function"
        assert result["_containing_function"] == "foo"

    def test_codeEnricher_reusesSharedScopeTable(self, monkeypatch):
        """BE-6: ASTChunker and CodeEnricher.enrich_chunk() consume the SAME
        ScopeTable object built by one prepare() call — one shared parse pass,
        not two. Every chunk ASTChunker produces must resolve correctly against
        that same scope_table, AND ASTChunker.chunk() must not trigger a second
        internal parse (_build_scope_table) — proven via a call-count spy.
        """
        import archon_search.code_enricher as ce
        from archon_search.chunker import ASTChunker

        # The only expected parse: this call to _prepare_py_enricher() (via
        # CodeEnricher.prepare()), executed BEFORE the spy is installed below.
        enricher, scope_table = self._prepare_py_enricher()
        assert scope_table, "tree-sitter grammar must be available for this test"

        real_build_scope_table = ce._build_scope_table
        spy = MagicMock(side_effect=real_build_scope_table)
        monkeypatch.setattr(ce, "_build_scope_table", spy)

        chunker = ASTChunker(chunk_size=5)
        records = chunker.chunk(
            self.py_source,
            "doc1",
            "/repo/pkg/mod.py",
            scope_table=scope_table,
            file_type="py",
            updated_at="2024-01-01T00:00:00.000000Z",
            ingested_by="cli",
        )
        assert records
        spy.assert_not_called()  # no hidden second parse inside ASTChunker.chunk()

        # A chunk starting exactly at top_fn's scope boundary must resolve via
        # the SAME scope_table object the chunker consumed.
        top_fn_scope = next(e for e in scope_table if e.fn_name == "top_fn")
        target = next(r for r in records if r.start_offset == top_fn_scope.start)
        result = enricher.enrich_chunk(target, scope_table)
        assert result["_symbol_type"] == "function"
        assert result["_containing_function"] == "top_fn"
