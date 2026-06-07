# C3c — Code Symbol Context Enrichment
**Purpose**: Enrich every code-file chunk with symbol-level metadata (`_symbol_type`, `_containing_function`, `_containing_class`, `_module_path`, `_symbol_subtype`) derived from tree-sitter AST parse trees at ingest time.
**Audience**: Backend developer implementing the feature.
**Status**: To Do

---

## Background
Code chunks carry no structural provenance. A chunk containing `return self._cache.get(key)` gives no signal about containment — method vs. function vs. module-level code, or which class it belongs to. This makes code queries rely entirely on FTS token matching, which is imprecise across large codebases.

C3c adds a `CodeEnricher` parallel to `MarkdownEnricher` (C3a). It follows the same `prepare()` / `enrich_chunk()` two-pass protocol and is dispatched by file extension in `pipeline.py`. The prerequisite offset fields (`start_offset` / `end_offset` on `ChunkRecord`) were delivered in C3a.

---

## Goal
Every chunk produced from a code file (`.py`, `.ts`, `.js`, `.go`, `.rs`, `.java`, `.sh`) carries five symbol-level metadata fields resolved from a tree-sitter parse of the source file. Python and TypeScript are mandatory first-class languages with full fixture coverage. Other languages install via the optional `[code]` dep group and exercise degradation paths. Parse failures and missing grammars degrade gracefully — no ingest abort, one log entry per file/language.

---

## Scope

### In Scope
- `CodeEnricher` class in `archon_search/code_enricher.py` implementing `prepare()` / `enrich_chunk()`
- Fields: `_symbol_type`, `_containing_function`, `_containing_class`, `_module_path`, `_symbol_subtype`
- Language v1 matrix: **Python** (mandatory), **TypeScript** (mandatory)
- Optional dep group `[code]` in `pyproject.toml` — tree-sitter core + grammar packages for all 7 code extensions; versions pinned for ABI compatibility
- Pipeline dispatch in `pipeline.py`: code extensions → `CodeEnricher`, everything else → existing path
- `_module_path` derivation: 7-step algorithm from the brief
- Decorator attribution: use `decorated_definition` node boundaries directly in Python (already wraps decorators); extend scope start to earliest `decorator` child for TypeScript
- Graceful degradation: missing grammar → one-time INFO log per process; parse failure → WARNING per file, capped at K=10 per job then downgraded to DEBUG
- Unit tests against dedicated fixture files (`tests/fixtures/code/python/sample.py`, `tests/fixtures/code/typescript/sample.ts`)
- Eval queries in `tests/eval/queries.jsonl` + `tests/eval/labels.jsonl` covering code corpus

### Out of Scope
- Symbol graph / call graph (cross-symbol references)
- Cross-file symbol resolution
- Typed `SearchFilters` for symbol fields
- Auto-installing grammars at runtime
- Non-plain-extension code-like formats (Jupyter notebooks)
- Go, Rust, Java, JS, sh fixture files (degradation-only in v1)
- Macro-level symbol-aware eval gating (eval harness is metadata-blind by design)

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See Task 8.1 — Final verification & documentation update.

---

## What does NOT change
- `MarkdownEnricher` protocol and all text/docling-format enrichment paths
- `ChunkRecord` fields (only `metadata` dict is extended with new underscore-prefixed keys)
- `_heading` / `_section_path` metadata keys for **non-code files** (C3a output unchanged for `.md`, `.txt`, etc.)
- `tests/eval/README.md` threshold-lowering policy and harness determinism guarantees
- Store schema: metadata is a free-form `dict[str, str]`; no new columns

---

## Known limitations / accepted trade-offs
- `_module_path` stored per-chunk despite being file-level (duplication accepted; future iteration may lift to a document-level column)
- `.d.ts` resolves `types/lib.d.ts` → `types.lib.d` (intentional; one extension stripped)
- Per-process per-extension WARNING cap (K=10 per extension for the process lifetime) means once 10 parse failures occur for a given extension, all subsequent failures for that extension are silently downgraded to DEBUG — including future ingest jobs. A server restart resets the counter.
- v1 scope table assigns chunk to scope containing its *start offset* when a chunk spans a boundary (consistent with C3a heading convention)
- Anonymous functions (lambdas, arrow functions assigned to a const) are treated as enclosing-scope code; named closures captured only if grammar exposes them
- Code files (`.py`, `.ts`, etc.) no longer receive `_heading` / `_section_path` metadata; these keys will be absent from code chunk metadata (previously they were present as empty strings via MarkdownEnricher). Downstream consumers using `chunk.metadata.get("_heading")` are unaffected; those using `"_heading" in chunk.metadata` must be updated if they target code files.
- CLI callers (`archon_search/cli/`) do not currently pass `collection_root` to `ingest_directory`; they will produce stem-only `_module_path` values. This includes the `collection reindex` command (`cli/collection.py:341`, `cli/collection.py:357`) which has `source_path` available but is intentionally kept `None` for v1 to avoid a larger CLI refactor. A future CLI `--collection-root` flag can propagate the root to all CLI paths.

---

## Architecture

### New module: `archon_search/code_enricher.py`
Isolated from `enricher.py` to keep the optional tree-sitter import contained.

**Types:**
```python
ScopeEntry = namedtuple("ScopeEntry", ["start", "end", "symbol_type", "fn_name", "class_name"])
# start, end: character offsets in source text (converted from tree-sitter byte offsets)
# symbol_type: "function" | "method" | "class" | "module"
# fn_name: str — innermost function/method name, "" if none
# class_name: str — innermost class name, "" if none

ScopeTable = list[ScopeEntry]  # sorted by (start ASC, end DESC) — innermost scope last at equal starts
```

**Grammar registry** (module-level, lazy-loaded):
```python
_GRAMMAR_CACHE: dict[str, Any | None]  # ext → Language object or None
_GRAMMAR_LOGGED: set[str]              # extensions for which one-time INFO was emitted
```

**Public class:**
```python
class CodeEnricher:
    _module_path_value: str  # set during prepare(), consumed by enrich_chunk()
    _ext: str                # set during prepare(), used by enrich_chunk() for _lang_label()
    # NOTE: CodeEnricher instances are NOT reusable across files. A new instance must be
    # created per ingest_file() call. _module_path_value and _ext are set during prepare()
    # and consumed by enrich_chunk(); calling enrich_chunk() before prepare() on the same
    # instance will return stale or empty data.

    def prepare(
        self,
        text: str,
        ext: str,
        file_path: Path,
        collection_root: Path | None,
    ) -> ScopeTable:
        """Parse source with tree-sitter. Stores module path as instance state.
        Returns empty ScopeTable on catastrophic scope-builder failure (WARNING logged).
        Note: tree-sitter does NOT raise on broken syntax; ERROR nodes are processed normally.
        """

    def enrich_chunk(
        self,
        chunk: "ChunkRecord",
        scope_table: ScopeTable,
    ) -> dict[str, str]:
        """Resolve innermost scope by chunk.start_offset.
        Returns 5-field metadata dict on success; returns {"_module_path": ...} if scope_table is
        empty but module path is known; returns {} only if scope_table is empty and no module path.
        """
```

**Helpers (module-private):**
```python
def _module_path(file_path: Path, collection_root: Path | None) -> str
def _get_grammar(ext: str) -> Any | None          # returns tree_sitter.Language or None
def _build_scope_table(source: str, lang: Any, ext: str) -> ScopeTable
def _resolve_scope(offset: int, scope_table: ScopeTable) -> ScopeEntry | None
```

**Code extension set (used for pipeline dispatch):**
```python
CODE_EXTENSIONS: frozenset[str] = frozenset({".py", ".ts", ".js", ".go", ".rs", ".java", ".sh"})
```

### Modified: `archon_search/pipeline.py`
`ingest_file()` — extend enricher dispatch block (lines ~260–299):
- Check `suffix` (file extension) at ingest time
- If `suffix in CODE_EXTENSIONS`: instantiate `CodeEnricher`, call `prepare(text, ext, path, collection_root)`
- Otherwise: existing MarkdownEnricher path unchanged
- The `enrich_chunk()` per-chunk loop is already in place; add the parallel call for the code enricher

### Optional deps: `pyproject.toml`
New group (versions pinned at implementation time for ABI compatibility):
```toml
[project.optional-dependencies]
code = [
    "tree-sitter>=X.Y,<X.Z",
    "tree-sitter-python>=X.Y",
    "tree-sitter-typescript>=X.Y",
    "tree-sitter-javascript>=X.Y",
    "tree-sitter-go>=X.Y",
    "tree-sitter-rust>=X.Y",
    "tree-sitter-java>=X.Y",
    "tree-sitter-bash>=X.Y",
]
```

### Test fixtures
- `tests/fixtures/code/python/sample.py` — per brief: module docstring, `top_fn`, class `Outer` with `outer_method`, nested class `Inner` with `inner_method`, module-level statement, `@decorated_fn`
- `tests/fixtures/code/typescript/sample.ts` — top-level function, class with method, exported const arrow function

---

## Task breakdown

### Phase 1 — Dependencies & core types
> **Releasable**: after Task 1.2; the module exists and is importable but does nothing yet.

#### Task 1.1 — Add `[code]` optional dep group to `pyproject.toml`
- [x] **File**: `pyproject.toml`
- **Depends on**: nothing
- **Description**:
  - Add `code = [...]` entry under `[project.optional-dependencies]` following the pattern of `multilingual = ["fasttext-wheel>=0.9.2"]`
  - Include tree-sitter core + grammar packages for all 7 code extensions (Python, TypeScript, JavaScript, Go, Rust, Java, Bash)
  - Pin all versions with `>=X.Y,<X.Z` bounds; find the latest stable compatible set at implementation time (ABI coupling: core and all grammar packages must share the same major ABI version — verify with `python -c "import tree_sitter; import tree_sitter_python"`)
  - Do NOT add to `[dependency-groups]` dev tools — this is a runtime optional feature
- **Releasable**: `uv sync --extra code` installs all packages without errors
- **Tests (TDD)** — `tests/test_code_enricher_deps.py`:
  - Unit: `test_code_enricher_importable_without_tree_sitter` — monkeypatch tree-sitter packages as absent in `sys.modules`; assert `from archon_search.code_enricher import CodeEnricher, CODE_EXTENSIONS` succeeds without raising `ImportError`. This verifies the "no module-level tree-sitter import" design constraint.
  - Unit: `test_code_optional_group_in_pyproject` — (kept as a sanity check) parse `pyproject.toml` via `tomllib` and assert `code` key exists in `project.optional-dependencies` with at least `tree-sitter`, `tree-sitter-python`, `tree-sitter-typescript`.
  - Checkpoint: `uv run pytest tests/test_code_enricher_deps.py -v`

#### Task 1.2 — Define `ScopeEntry`, `ScopeTable`, `CODE_EXTENSIONS` in `code_enricher.py`
- [ ] **File**: `archon_search/code_enricher.py` (new file)
- **Depends on**: nothing
- **Description**:
  - `ScopeEntry = namedtuple("ScopeEntry", ["start", "end", "symbol_type", "fn_name", "class_name"])` where `start`/`end` are `int`, `symbol_type` is one of `"function"`, `"method"`, `"class"`, `"module"`, and name fields are `str`
  - `ScopeTable = list[ScopeEntry]`
  - `CODE_EXTENSIONS: frozenset[str] = frozenset({".py", ".ts", ".js", ".go", ".rs", ".java", ".sh"})`
  - No tree-sitter import at module level — defer to `_get_grammar()` to keep the module importable without `[code]` installed
- **Releasable**: module is importable; types are usable in tests
- **Tests (TDD)** — `tests/test_code_enricher.py`:
  - Unit: `test_scope_entry_fields` — construct a `ScopeEntry` and assert all five fields accessible by name
  - Unit: `test_code_extensions_contains_mandatory` — assert `.py` and `.ts` are in `CODE_EXTENSIONS`
  - Unit: `test_code_extensions_does_not_contain_markdown` — assert `.md`, `.txt`, `.json` are NOT in `CODE_EXTENSIONS`
  - Checkpoint: `uv run pytest tests/test_code_enricher.py::test_scope_entry_fields tests/test_code_enricher.py::test_code_extensions_contains_mandatory tests/test_code_enricher.py::test_code_extensions_does_not_contain_markdown -v`

---

### Phase 2 — `_module_path` derivation
> **Releasable**: after Task 2.1; the helper is fully tested independently.

#### Task 2.1 — Implement `_module_path(file_path, collection_root) -> str`
- [ ] **File**: `archon_search/code_enricher.py`
- **Depends on**: Task 1.2
- **Description**:
  - Signature: `def _module_path(file_path: Path, collection_root: Path | None) -> str`
  - **Setup**: `ext = file_path.suffix.lower()` (derived from `file_path`; used in steps 6 and 7 below)
  - Algorithm (applied in order):
    1. If `collection_root` is None: return `file_path.stem` (fallback — no dots)
    2. Compute `rel = file_path.relative_to(collection_root)` (POSIX path)
    3. Strip one extension only: `parts = rel.with_suffix("").parts`
    4. Replace `/` separators by joining `parts` with `.`
    5. If last segment is `__init__`: drop it (Python package root)
    6. For Python files only (ext `.py`): replace hyphens with underscores in each path segment
    7. If last segment is `index` and ext is `.ts` or `.js`: drop it (Node convention)
  - Returns the final dotted string
  - Edge: if step 5 or 7 leaves an empty string (single-segment `__init__.py` at root), return the parent segment or empty string — document with a comment
- **Releasable**: function is callable and tested; drives `CodeEnricher.prepare()` in Task 5.1
- **Tests (TDD)** — `tests/test_code_enricher.py` class `TestModulePath`:
  - Unit: `test_regular_python` — `/repo/archon_search/store.py` with root `/repo` → `"archon_search.store"`
  - Unit: `test_init_py` — `/repo/archon_search/jobs/__init__.py` with root `/repo` → `"archon_search.jobs"`
  - Unit: `test_dts` — `/repo/types/lib.d.ts` with root `/repo` → `"types.lib.d"`
  - Unit: `test_hyphenated_python` — `/repo/my-pkg/mod.py` with root `/repo` → `"my_pkg.mod"`
  - Unit: `test_hyphenated_non_python` — `/repo/my-pkg/mod.ts` with root `/repo` → `"my-pkg.mod"` (no substitution)
  - Unit: `test_no_collection_root` — `Path("/tmp/foo.py")` with `None` → `"foo"`
  - Unit: `test_index_ts` — `/repo/web/api/index.ts` with root `/repo` → `"web.api"`
  - Unit: `test_index_js` — `/repo/web/api/index.js` with root `/repo` → `"web.api"`
  - Unit: `test_index_dts` — `/repo/web/index.d.ts` with root `/repo` → `"web.index.d"`. Verifies that step 7 does NOT fire for `.d.ts` files because after `with_suffix("")`, the last path segment is `"index.d"`, not `"index"`.
  - Unit: `test_init_at_root` — `_module_path(Path("/repo/__init__.py"), Path("/repo"))` → `""`. The algorithm: `rel = __init__.py` → `with_suffix("") = __init__` → `parts = ("__init__",)` → joined = `"__init__"` → step 5 drops → `""`. This is the correct value for a root package init file; document with a comment in the implementation.
  - Checkpoint: `uv run pytest tests/test_code_enricher.py::TestModulePath -v`

---

### Phase 3 — Grammar registry
> **Releasable**: after Task 3.1; degradation paths are exercised before any real grammar code.

#### Task 3.1 — Grammar registry with lazy loading and graceful degradation
- [ ] **File**: `archon_search/code_enricher.py`
- **Depends on**: Task 1.2
- **Description**:
  - Module-level: `_GRAMMAR_CACHE: dict[str, Any | None] = {}` and `_GRAMMAR_LOGGED: set[str] = set()`
  - `def _get_grammar(ext: str) -> Any | None`:
    - If `ext` in `_GRAMMAR_CACHE`: return cached value (may be `None`)
    - Attempt to import the appropriate package and create a `Language` object:
      - `.py` → `import tree_sitter_python; Language(tree_sitter_python.language())`
      - `.ts` → `import tree_sitter_typescript; Language(tree_sitter_typescript.language_typescript())`
      - `.js` → `import tree_sitter_javascript; Language(tree_sitter_javascript.language())`
      - `.go` → `import tree_sitter_go; Language(tree_sitter_go.language())`
      - `.rs` → `import tree_sitter_rust; Language(tree_sitter_rust.language())`
      - `.java` → `import tree_sitter_java; Language(tree_sitter_java.language())`
      - `.sh` → `import tree_sitter_bash; Language(tree_sitter_bash.language())`
    - On `ImportError` or any exception: cache `None`; if `ext` not yet in `_GRAMMAR_LOGGED`, emit `logger.info("tree-sitter grammar not available for %s; code enrichment skipped", ext)` and add to `_GRAMMAR_LOGGED`
    - Store result in `_GRAMMAR_CACHE[ext]` and return it
  - Import `tree_sitter.Language` inside the function to avoid module-level dependency
- **Releasable**: `_get_grammar(".py")` returns a Language or None without raising
- **Tests (TDD)** — `tests/test_code_enricher.py` class `TestGrammarRegistry`:
  - Unit: `test_grammar_returns_none_for_unknown_ext` — `_get_grammar(".xyz")` → `None`; no exception
  - Unit: `test_grammar_returns_none_when_import_fails` — `monkeypatch.setitem(sys.modules, "tree_sitter_python", None)`, clear cache; `_get_grammar(".py")` → `None`; assert INFO logged once
  - Unit: `test_grammar_info_logged_once` — call `_get_grammar(".py")` twice with mocked missing import; assert INFO emitted exactly once
  - Unit: `test_grammar_result_cached` — call twice with no monkeypatch; assert second call doesn't re-import (spy on `importlib.import_module` or verify cache state)
  - Checkpoint: `uv run pytest tests/test_code_enricher.py::TestGrammarRegistry -v`

---

### Phase 4 — Test fixtures
> **Releasable**: after Task 4.2; fixtures are available for all scope builder tests.

#### Task 4.1 — Create `tests/fixtures/code/python/sample.py`
- [ ] **File**: `tests/fixtures/code/python/sample.py`
- **Depends on**: nothing
- **Description**:
  - Content (exact layout required for deterministic offset-based tests):
    - Module docstring
    - Top-level function `top_fn` with a body statement
    - Class `Outer` with:
      - Class-level attribute (so a "class body" chunk exists between the `class Outer:` line and the first method)
      - Method `outer_method`
      - Nested class `Inner` with method `inner_method`
    - Module-level statement after `Outer` (so a "module" chunk is available)
    - Decorated function `@some_decorator\ndef decorated_fn(): ...`
  - Keep the file short (~40–60 lines) so character offsets stay small and deterministic
  - No `__init__.py` or import statements needed — keep it self-contained
- **Releasable**: file exists and is readable by tests
- **Tests (TDD)** — `tests/test_code_enricher.py` class `TestFixtureContracts`:
  - Unit: `test_python_fixture_has_outer_class` — assert `"class Outer"` in content of `tests/fixtures/code/python/sample.py`
  - Unit: `test_python_fixture_has_nested_inner` — assert `"class Inner"` in content
  - Unit: `test_python_fixture_has_decorated_fn` — assert `"@some_decorator"` in content
  - Unit: `test_python_fixture_has_top_fn` — assert `"def top_fn"` in content
  - Unit: `test_python_fixture_is_valid_python` — assert `ast.parse()` succeeds without exception
- **Checkpoint**: `python -c "import ast; ast.parse(open('tests/fixtures/code/python/sample.py').read()); print('OK')"`

#### Task 4.2 — Create `tests/fixtures/code/typescript/sample.ts`
- [ ] **File**: `tests/fixtures/code/typescript/sample.ts`
- **Depends on**: nothing
- **Description**:
  - Content:
    - Top-level exported function `topFn`
    - Class `MyClass` with a method `myMethod`
    - Exported const arrow function assigned to a name: `export const arrowFn = () => { ... }`
  - Keep short (~25–35 lines)
- **Releasable**: file exists and is readable by tests
- **Tests (TDD)** — `tests/test_code_enricher.py` class `TestFixtureContracts` (continued):
  - Unit: `test_ts_fixture_has_top_fn` — assert `"topFn"` in content of `tests/fixtures/code/typescript/sample.ts`
  - Unit: `test_ts_fixture_has_class` — assert `"class MyClass"` in content
  - Unit: `test_ts_fixture_has_arrow_fn` — assert `"arrowFn"` in content
- **Checkpoint**: (TypeScript syntax check if `tsc` available; otherwise visual review)

---

### Phase 5 — Scope table builder
> **Releasable**: after Task 5.2; `_build_scope_table` is fully tested for Python and TypeScript.

#### Task 5.1 — Implement `_build_scope_table(source, lang, ext) -> ScopeTable`
- [ ] **File**: `archon_search/code_enricher.py`
- **Depends on**: Task 3.1, Task 4.1, Task 4.2
- **Description**:
  - Signature: `def _build_scope_table(source: str, lang: Any, ext: str) -> ScopeTable`
  - Parse `source` with `tree_sitter.Parser(lang).parse(source.encode())` → AST root
  - **Offset unit**: tree-sitter returns `node.start_byte`/`node.end_byte` as byte positions in the UTF-8-encoded buffer. `ChunkRecord.start_offset` is a character position. `_build_scope_table` must convert byte offsets to character offsets. Build a byte-to-char offset map once before walking the AST: iterate over `source` characters, accumulating UTF-8 byte widths to build a `dict[int, int]` from byte offset to char offset. Use this map when constructing each `ScopeEntry`. **Important**: ensure the map includes the EOF sentinel: after the loop, add `byte_to_char[byte_pos] = len(source)` where `byte_pos` is the total byte length. Tree-sitter's root node `end_byte` equals the total byte length; without the sentinel, a `KeyError` will occur when the scope for the last node in the file is constructed.
  - Walk AST recursively; for each relevant node type, construct a `ScopeEntry`:
    - `function_definition` (Python) / `function_declaration` (TS/JS): `symbol_type="function"`; if parent is class body → `symbol_type="method"`, `class_name=<parent class name>`
    - `class_definition` / `class_declaration`: `symbol_type="class"`
    - Nodes outside any function or class → `symbol_type="module"`
    - Nested scopes: inner wins — when walking the tree, build the containing context stack
  - **Decorator attribution**: In tree-sitter-python, the `decorated_definition` node ALREADY wraps both the decorator and the function/class. When a `decorated_definition` node is encountered: emit a SINGLE `ScopeEntry` using the `decorated_definition` node's start/end bounds, but extract `fn_name` and `class_name` (and `symbol_type`) from the inner `function_definition` or `class_definition` child node. Do NOT emit a separate entry for the inner child. This prevents duplicate/overlapping scope entries for decorated functions. For TypeScript, decorator nodes are children of the class/method declaration; extend the scope `start` to the earliest `decorator` child node's `start_byte`.
  - **Innermost scope wins**: use a depth-first walk; when resolving for a given offset, pick the deepest matching scope
  - Return list sorted by `(start ASC, end DESC)` — matching the `ScopeTable` type contract. Entries with the same `start` are sorted by `end` descending so that the innermost scope (smallest `end`) appears at the highest index, enabling the backward walk in `_resolve_scope` to find it first.
  - The `ScopeTable` does NOT include a module-level catch-all entry. Offsets in module-level code (between functions, at file top/bottom) will have no matching scope entry. `_resolve_scope` returns `None` for these offsets, and `enrich_chunk` handles `None` by returning module-level metadata.
  - **Anonymous arrow functions** (TS `arrow_function` node assigned to a `const`): do NOT create a scope entry; they fall through to the enclosing scope
- **Releasable**: `_build_scope_table(source, lang, ext)` returns correct entries for the fixture files
- **Tests (TDD)** — `tests/test_code_enricher.py` class `TestBuildScopeTablePython`:
  - Setup: `PYTHON_SOURCE = Path("tests/fixtures/code/python/sample.py").read_text()`; obtain language via `_get_grammar(".py")` (skip test if None with `pytest.importorskip` pattern)
  - Unit: `test_top_fn_entry` — assert a `ScopeEntry` with `symbol_type="function"`, `fn_name="top_fn"` present in scope table
  - Unit: `test_outer_method_is_method` — assert `ScopeEntry(symbol_type="method", fn_name="outer_method", class_name="Outer")` present
  - Unit: `test_inner_method_class_is_inner` — assert `ScopeEntry(fn_name="inner_method", class_name="Inner")` present (innermost class wins)
  - Unit: `test_outer_class_entry` — assert `ScopeEntry(symbol_type="class", class_name="Outer")` present
  - Unit: `test_decorated_fn_start_includes_decorator` — `decorated_fn` entry start offset ≤ offset of `@some_decorator` line start. **Important**: compute the expected offset dynamically: `expected_decorator_start = PYTHON_SOURCE.index("@some_decorator")`, not a hardcoded integer.
  - Unit: `test_non_ascii_offsets` — construct a synthetic Python source with a non-ASCII character (e.g., `# café\ndef top_fn(): pass`) before the function; assert the `ScopeEntry.start` for `top_fn` matches the character offset of `def`, not the byte offset (they differ because `é` is 2 bytes but 1 character)
  - Unit: `test_scope_table_sorted` — assert all entries in ascending `start` order
  
  Class `TestBuildScopeTableTypeScript`:
  - Unit: `test_ts_top_fn` — `topFn` scope entry present with `symbol_type="function"`
  - Unit: `test_ts_class_method` — `myMethod` entry with `symbol_type="method"`, `class_name="MyClass"`
  - Unit: `test_ts_arrow_fn_not_captured_as_scope` — no scope entry with `fn_name="arrowFn"`
  - Checkpoint: `uv run pytest tests/test_code_enricher.py::TestBuildScopeTablePython tests/test_code_enricher.py::TestBuildScopeTableTypeScript -v`

---

### Phase 6 — `CodeEnricher` class
> **Releasable**: after Task 6.2; the full enricher is callable end-to-end.

#### Task 6.1 — Implement `CodeEnricher.prepare()`
- [ ] **File**: `archon_search/code_enricher.py`
- **Depends on**: Task 2.1, Task 3.1, Task 5.1
- **Description**:
  - Class `CodeEnricher` with instance variable `_module_path_value: str = ""`
  - `def prepare(self, text: str, ext: str, file_path: Path, collection_root: Path | None) -> ScopeTable`:
    - Compute and store `self._module_path_value = _module_path(file_path, collection_root)`
    - Call `lang = _get_grammar(ext)`; if `None`, return `[]`
    - Attempt `_build_scope_table(text, lang, ext)`:
      - On any exception: emit `logger.warning("tree-sitter parse failed for %s: %s", file_path, exc)`; return `[]`
      - Return the scope table on success
  - Per-process per-extension WARNING cap: maintain a module-level `_parse_failure_count: dict[str, int]` keyed by extension; downgrade to DEBUG after K=10 per extension across the process lifetime. This is a best-effort guard; exact per-job semantics are deferred to a future iteration.
  - **Note**: the WARNING cap is a best-effort guard. Exact implementation of job context tracking is deferred; for v1, count per-process per-ext and downgrade after K=10.
- **Releasable**: `CodeEnricher().prepare(text, ".py", path, root)` returns a ScopeTable or `[]`
- **Tests (TDD)** — `tests/test_code_enricher.py` class `TestCodeEnricherPrepare`:
  - Unit: `test_prepare_returns_scope_table_for_valid_python` — valid Python source → non-empty ScopeTable
  - Unit: `test_prepare_returns_empty_for_missing_grammar` — monkeypatch grammar as None → `[]` returned; no exception
  - Unit: `test_prepare_returns_empty_on_parse_failure` — monkeypatch `_build_scope_table` to raise `RuntimeError`; assert `prepare()` returns `[]` and WARNING is logged. **Note**: tree-sitter does NOT raise on broken syntax — it returns a tree with `ERROR` nodes. The exception path in `prepare()` is for catastrophic failures (null language, library crash). Use `monkeypatch` to simulate this.
  - Unit: `test_prepare_sets_module_path` — after `prepare(text, ".py", Path("/repo/pkg/mod.py"), Path("/repo"))`, assert `enricher._module_path_value == "pkg.mod"`
  - Unit: `test_warning_logged_on_parse_failure` — monkeypatch `_build_scope_table` to raise `RuntimeError`; use `caplog`; assert WARNING appears.
  - Unit: `test_prepare_handles_tree_sitter_error_nodes` — pass source with a function containing an ERROR node: `"def foo(x,: pass\ndef bar(): return 1"`. Assert: `prepare()` does NOT raise; `isinstance(result, list)` is True; no WARNING is emitted. Optionally verify `any(e.fn_name == "bar" for e in result)` if tree-sitter extracts `bar` — but this is grammar-version-dependent and must not be a hard assertion. The critical invariant is: error nodes do NOT abort scope-table construction and do NOT produce a WARNING.
  - Checkpoint: `uv run pytest tests/test_code_enricher.py::TestCodeEnricherPrepare -v`

#### Task 6.2 — Implement `CodeEnricher.enrich_chunk()`
- [ ] **File**: `archon_search/code_enricher.py`
- **Depends on**: Task 6.1
- **Description**:
  - `def enrich_chunk(self, chunk: "ChunkRecord", scope_table: ScopeTable) -> dict[str, str]`:
    - If `scope_table` is empty: return `{"_module_path": self._module_path_value}` if `self._module_path_value` is non-empty, else return `{}`. This preserves file-level metadata even when AST parsing is skipped (missing grammar) or fails.
    - **Guard for sentinel offsets**: if `chunk.start_offset < 0` (sentinel indicating no offset information), treat as module-level (same as the `None` return from `_resolve_scope`).
    - Call `_resolve_scope(chunk.start_offset, scope_table)` → `ScopeEntry | None`
    - If `None` (offset before all scopes): treat as module-level
    - Compute `_symbol_subtype = f"{lang_from_ext(ext)}-{symbol_type}"` — need `ext` for this; store it as `self._ext` during `prepare()`
    - Return:
      ```python
      {
          "_symbol_type": entry.symbol_type,
          "_containing_function": entry.fn_name,
          "_containing_class": entry.class_name,
          "_module_path": self._module_path_value,
          "_symbol_subtype": f"{language}-{entry.symbol_type}",
      }
      ```
    - `_resolve_scope(offset, table)`: binary search (`bisect_right`) on `start` fields to find the rightmost entry whose `start <= offset`; then walk backwards from the bisect point and return the FIRST entry encountered (closest to the bisect point, i.e. highest index) whose `end > offset`; or `None` if no entry qualifies. **Tiebreaker**: when two scopes share the same `start` (e.g., a `decorated_definition` and its inner function at the same byte), sort by `end` DESCENDING so that the innermost scope (smallest span, i.e. smallest `end` at a given `start`) appears latest in the sorted order and is found first by the backward walk. Rationale: at a given `start`, the outermost scope has the largest `end` — placing it first in a DESC-sorted list — so the backward walk arrives at the innermost scope first, which is exactly what it needs to return.
    - `lang_from_ext(ext)`: map `.py` → `"python"`, `.ts` → `"typescript"`, `.js` → `"javascript"`, `.go` → `"go"`, `.rs` → `"rust"`, `.java` → `"java"`, `.sh` → `"bash"`; `def _lang_label(ext: str) -> str`
    - Module-level fallback: if no scope contains the offset, return `{"_symbol_type": "module", "_containing_function": "", "_containing_class": "", "_module_path": ..., "_symbol_subtype": f"{language}-module"}`
- **Releasable**: `enrich_chunk()` returns correct 5-field dict for all symbol types
- **Tests (TDD)** — `tests/test_code_enricher.py`:

  **Class `TestResolveScope`** (tests `_resolve_scope` directly with synthetic scope tables, no fixture dependency):
  - Unit: `test_offset_inside_scope` — scope `[10, 50)`, query offset 25 → returns that entry
  - Unit: `test_offset_exactly_at_start` — scope starts at 10, query offset 10 → returns that entry
  - Unit: `test_offset_at_end_exclusive_no_next_scope` — scope table has only `[10, 50)`; query offset 50 → `None` (exclusive end, no scope contains it)
  - Unit: `test_offset_at_end_exclusive_with_adjacent_scope` — scope table has `[10, 50)` and `[50, 90)`; query offset 50 → returns the second scope (start-inclusive, `bisect_right` lands on it)
  - Unit: `test_offset_before_all_scopes` — query offset 0, first scope starts at 10 → returns `None`
  - Unit: `test_offset_after_all_scopes` — query offset 100, last scope ends at 80 → returns `None`
  - Unit: `test_nested_scopes_innermost_wins` — outer `[0, 100)` class scope, inner `[20, 60)` method scope; query offset 30 → method scope returned
  - Unit: `test_module_gap_between_scopes` — two non-overlapping functions `[0, 20)` and `[40, 60)`; query offset 30 (in gap) → `None`
  - Unit: `test_single_entry_scope_table` — degenerate case with one entry
  - Unit: `test_same_start_tiebreaker` — scope table has `ScopeEntry(start=10, end=100, symbol_type="class", ...)` and `ScopeEntry(start=10, end=50, symbol_type="method", ...)`; query offset 25 → returns the entry with `end=50` (method, innermost scope). This verifies `(start ASC, end DESC)` sorting and backward-walk tiebreaker.

  **Class `TestLangLabel`** (tests `_lang_label` directly):
  - Unit: `test_lang_label_python` — `_lang_label(".py")` → `"python"`
  - Unit: `test_lang_label_typescript` — `_lang_label(".ts")` → `"typescript"`
  - Unit: `test_lang_label_javascript` — `_lang_label(".js")` → `"javascript"`
  - Unit: `test_lang_label_go` — `_lang_label(".go")` → `"go"`
  - Unit: `test_lang_label_rust` — `_lang_label(".rs")` → `"rust"`
  - Unit: `test_lang_label_java` — `_lang_label(".java")` → `"java"`
  - Unit: `test_lang_label_bash` — `_lang_label(".sh")` → `"bash"` (not `"sh"`)
  - Unit: `test_lang_label_unknown_ext` — `_lang_label(".xyz")` → returns a non-empty fallback string or raises `KeyError`; document which behavior is chosen and pin it

  Class `TestEnrichChunk`:
  - Setup helper: `make_chunk(start, end)` → `ChunkRecord` with `start_offset=start`, `end_offset=end`. **Do NOT use hardcoded integer literals** for offsets that come from fixture files. Instead, compute offsets dynamically: `start = sample_py.index("def top_fn")` (searching for the marker string in the fixture content). This makes tests resilient to fixture edits. The helper should accept both marker strings and precomputed ints.
  - Unit: `test_enrich_top_fn_chunk` — a chunk whose `start_offset` is inside `top_fn` body → `_symbol_type="function"`, `_containing_function="top_fn"`, `_containing_class=""`
  - Unit: `test_enrich_outer_method_chunk` → `_symbol_type="method"`, `_containing_function="outer_method"`, `_containing_class="Outer"`
  - Unit: `test_enrich_class_body_chunk` — chunk between `class Outer:` line and first method → `_symbol_type="class"`, `_containing_class="Outer"`, `_containing_function=""`
  - Unit: `test_enrich_module_level_chunk` — chunk after the class (offset in a gap within a **non-empty** scope table) → result contains all 5 keys including `_symbol_type="module"` and empty name fields. Assert `len(result) == 5` to distinguish this from the empty-scope-table path which returns only 1 key.
  - Unit: `test_enrich_inner_method_innermost_class_wins` — chunk in `Inner.inner_method` → `_containing_class="Inner"`, `_containing_function="inner_method"`
  - Unit: `test_enrich_decorator_chunk` — chunk at decorator line of `decorated_fn` → `_containing_function="decorated_fn"`
  - Unit: `test_enrich_ts_arrow_fn` — chunk inside arrow function body → `_symbol_type` matches the enclosing scope (module-level in the fixture)
  - Unit: `test_enrich_module_path_present` — any chunk → `"_module_path"` key present in result with expected value
  - Unit: `test_enrich_symbol_subtype_python` — Python chunk → `_symbol_subtype` matches `"python-{symbol_type}"`
  - Unit: `test_enrich_symbol_subtype_typescript` — TS chunk → `_symbol_subtype` starts with `"typescript-"`
  - Unit: `test_enrich_empty_scope_table_returns_module_path_only` — after `prepare()` is called (so `_module_path_value` is set), call `enrich_chunk(chunk, [])` → result contains `_module_path` key with the expected value, but no `_symbol_type` key.
  - Unit: `test_enrich_chunk_negative_offset_treated_as_module_level` — call `enrich_chunk(make_chunk(-1, -1), scope_table)` where scope_table is non-empty; assert no exception is raised and result has `_symbol_type="module"` (no scope contains offset -1).
  - Checkpoint: `uv run pytest tests/test_code_enricher.py::TestEnrichChunk -v`

---

### Phase 7 — Pipeline integration
> **Releasable**: after Task 7.1; code files ingested via the normal pipeline carry symbol metadata.

#### Task 7.1 — Dispatch `CodeEnricher` in `pipeline.py`
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 6.2; `ingest_file()` and `ingest_directory()` signature changes require updating all direct and transitive callers
- **Description**:
  - Import `CodeEnricher`, `CODE_EXTENSIONS` from `archon_search.code_enricher` at the top of `pipeline.py`
  - In `ingest_file()`, after the parser call and before the chunker, determine which enricher to use:
    ```python
    suffix = path.suffix.lower()
    if suffix in CODE_EXTENSIONS:
        enricher = CodeEnricher()
        scope_table = enricher.prepare(text, suffix, path, collection_root)
    else:
        enricher = MarkdownEnricher()
        scope_table = enricher.prepare(text) if is_text_type else []
    ```
  - In the per-chunk loop, call `enricher.enrich_chunk(record, scope_table)` uniformly for both enricher types (the return type is `dict[str, str]` for both; the call site is already generic)
  - `collection_root` is NOT currently a parameter of `ingest_file()` or `SearchPipeline`. To enable dotted `_module_path` derivation:
    1. Add `collection_root: Path | None = None` as a **keyword-only** argument (after the `*`) to `ingest_file()`'s signature.
    2. Also add `collection_root: Path | None = None` as a **keyword-only** argument to `ingest_directory()`'s signature. Inside `ingest_directory`, forward `collection_root` unchanged to every `ingest_file()` call within the loop. **Do NOT default to `path`**: API callers may pass arbitrary upload directories that are not collection roots, producing meaningless dotted module paths. The sync/watcher callers that know the real collection root (`source_path`) should pass it explicitly; API callers should pass `None`.
    3. Update all direct `ingest_file` callers: `sync.py:670` and `sync.py:697` pass the watcher's `source_path`; `mcp.py:644`, `routes_jobs.py:83` can pass `None`. `eval/runner.py:545` should pass `(corpus_root / "corpus").resolve()` as `collection_root` — the eval corpus is organized relative to this directory and proper dotted `_module_path` values are needed for eval code queries added in Task 8.1. Pass `(corpus_root / "corpus").resolve()` to ensure the path is canonical and matches the resolved `file_path` used by `ingest_file()`. The internal call at `pipeline.py:391` (from `ingest_directory` to `ingest_file`) is handled by step 2's forwarding instruction — it must pass `collection_root=collection_root`.
    4. Update all `ingest_directory` callers. `sync.py:538` can pass `source_path` as `collection_root`. `routes_jobs.py:259` (`_run_reindex_job`) receives `collection_path: Path` which IS the known collection root — it should pass `collection_path` as `collection_root`. `routes_jobs.py:87` (`_dispatch_ingest`, directory branch) and `mcp.py:690` (MCP `ingest_directory` tool) should pass `None` — they accept arbitrary user-supplied paths. CLI callers (`cli/collection.py`, `cli/ingest.py`) also use the default `None` — no changes needed; they produce stem-only `_module_path` values as documented in Known Limitations.
    Where `collection_root` is `None`, `_module_path()` returns `file_path.stem` — acceptable for API-driven single-file ingest.
  - No changes to MarkdownEnricher call paths; the `is_text_type` guard stays unchanged for non-code text files
- **Releasable**: `ingest_file("sample.py", ...)` stores chunks with `_symbol_type` in metadata
- **Tests (TDD)** — `tests/test_pipeline_code_enricher.py`:
  - Unit: `test_ingest_python_file_stores_symbol_metadata` — write a minimal `.py` fixture inline, ingest via pipeline, fetch chunks from store, assert `_symbol_type` key present and non-empty
  - Unit: `test_ingest_markdown_file_unaffected` — ingest a `.md` file, assert no `_symbol_type` key in any chunk (MarkdownEnricher path unchanged)
  - Unit: `test_ingest_python_file_graceful_on_scope_table_crash` — monkeypatch `_build_scope_table` to raise `RuntimeError`; ingest a valid `.py` fixture via pipeline; assert ingest succeeds (no exception), chunks present, `_symbol_type` key absent, `_module_path` present. **Note**: tree-sitter does NOT raise on broken syntax; use monkeypatch to simulate a catastrophic scope-builder failure.
  - Unit: `test_ingest_python_file_module_path` — ingest a `.py` file with a known collection root; assert `_module_path` matches expected dotted path
  - Unit: `test_ingest_directory_forwards_collection_root` — create a two-level in-memory directory structure (e.g., `tmpdir/pkg/mod.py`); call `ingest_directory(tmpdir, collection, collection_root=tmpdir, ...)`; fetch chunks from store; assert `_module_path` equals `"pkg.mod"`. This verifies that `collection_root` is forwarded from `ingest_directory` to each `ingest_file` call.
  - Unit: `test_ingest_directory_default_collection_root_is_none` — call `ingest_directory(tmpdir, collection, embedder=...)` WITHOUT passing `collection_root`; fetch chunks from store; assert `_module_path` equals `path.stem` (e.g., `"mod"` for `tmpdir/mod.py`), NOT a dotted path. This verifies that the default is `None` (not `path`) and that API-driven ingest without explicit root gets the safe stem-only fallback.
  - Unit: `test_ingest_code_file_graceful_on_missing_grammar` — monkeypatch `_get_grammar` to return `None` for `.py`; ingest a valid `.py` fixture inline via pipeline; assert ingest succeeds (no exception), chunks are stored, `_symbol_type` key is absent from chunk metadata, `_module_path` is present.
  - Checkpoint: `uv run pytest tests/test_pipeline_code_enricher.py -v`

> **Note**: These tests must NOT carry `@pytest.mark.integration`. They use inline fixtures and mocked/monkeypatched subsystems, not live infrastructure. Marking them `integration` would exclude them from the default run and leave new `pipeline.py` branches uncovered, potentially dropping total coverage below 85%.

---

### Phase 8 — Eval queries & final verification

#### Task 8.1 — Add code corpus eval queries + labels
- [ ] **File**: `tests/eval/queries.jsonl`, `tests/eval/labels.jsonl`, `tests/eval/corpus/`
- **Depends on**: Task 7.1
- **Description**:
  - Add 2–3 code corpus documents to `tests/eval/corpus/` (small `.py` files with distinct symbols)
  - Add corresponding entries to `tests/eval/documents.jsonl`
  - Add 2–3 queries in `queries.jsonl` targeting code chunks that are uniquely retrievable by FTS when symbol metadata is indexed: e.g., a query for the function name + class name that appears in a chunk
  - Add corresponding labels to `labels.jsonl` per the `tests/eval/README.md` schema
  - Follow the maintenance procedure in `tests/eval/README.md` exactly (query_id format, grade values, collection assignment)
  - These queries serve as a coarse FTS regression check only; they do NOT gate on metadata fields directly (harness is metadata-blind)
- **Releasable**: eval suite runs without fixture errors on the new code corpus entries
- **Tests (TDD)**: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py` must pass without threshold regressions
- **Checkpoint**: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py -v`

#### Task 8.2 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, Architecture docs, `CLAUDE.md`, `roadmap.md`, backlog briefs, `BREAKING.md`) and update every file whose content is affected by C3c. Mandatory updates:
    - `Documentation/Architecture/100_system_architecture_overview.md` — add `CodeEnricher` to enricher layer
    - `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` — add `code_enricher.py` entry
    - `Documentation/Architecture/130_data_architecture_and_persistence.md` — add the 5 new `_symbol_*` metadata keys
    - `Documentation/Backlog/C3a-markdown-structural-enrichment-brief.md` — verify that the `_source_subtype` footnote (in the "Key Decisions" section) already contains correct text referencing `_symbol_subtype` (C3c) and `_source_subtype` (C3b). If it reads: "C3c introduces `_symbol_subtype` (not `_source_subtype`) for code-level chunk classification... C3b introduces `_source_subtype` as its own field" — it is already correct and requires no edit.
    - `Documentation/roadmap.md` — mark C3c as complete
  - Verify all acceptance criteria below are met before marking complete
- **Releasable**: after this task, C3c is fully shipped, tested, and documented.
- **Acceptance criteria** (must all pass):
  - `uv run pytest` (default suite, no markers) passes with `--cov-fail-under=85`
  - `uv run pytest tests/test_code_enricher.py -v` — all symbol type, nested scope, decorator, anonymous function, `_module_path`, parse failure, and grammar-missing tests pass
  - `uv run pytest tests/test_pipeline_code_enricher.py -v` — all pipeline integration tests pass
  - Ingesting `tests/fixtures/code/python/sample.py` via pipeline stores chunks with `_symbol_type`, `_containing_function`, `_containing_class`, `_module_path`, `_symbol_subtype` in metadata
  - Ingesting a `.md` file via pipeline stores chunks with no `_symbol_type` key (MarkdownEnricher path unaffected)
  - Catastrophic parse failure (simulated via monkeypatch of `_build_scope_table`) does not abort ingest; chunks are stored with only `_module_path` and no symbol fields; a WARNING appears in logs
  - `uv run python -c "from archon_search.code_enricher import CodeEnricher"` succeeds even without `[code]` extras installed (tree-sitter not imported at module level)
  - C3a brief `_source_subtype` footnote verified correct; roadmap updated
  - `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py` passes without threshold regressions
- **Tests (TDD)**: N/A — verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
