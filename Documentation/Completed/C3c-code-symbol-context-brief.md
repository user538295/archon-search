# Feature Brief: C3c — Code Symbol Context Enrichment

## Problem
Code file chunks carry no symbol-level provenance. A chunk containing `return self._cache.get(key)` gives no indication whether it's inside a method, a standalone function, or module-level code — nor which class or module it belongs to. This makes code-specific queries ("find the cache lookup in the Repository class") rely entirely on FTS token matching rather than structural context, which is both imprecise and unreliable across large codebases.

## Goal
Every chunk produced from a code file carries chunk-level metadata fields — `_symbol_type`, `_containing_function`, `_containing_class`, `_module_path`, and `_symbol_subtype` — derived from tree-sitter parse trees at ingest time. Languages with available tree-sitter grammars are supported; Python is the mandatory first-class case, TypeScript is required as the second language for cross-language regression coverage. Field presence degrades gracefully: if symbol resolution fails for any reason, the chunk is stored without those fields (no ingest failure).

## Users & Context
Developers ingesting code repositories or source trees and querying for specific implementations, patterns, or symbols. They arrive with a mental model of the codebase structure ("find where we handle auth in the middleware layer") and expect search to respect that structure.

## Depends on
- **C3a** — the `MarkdownEnricher` infrastructure and underscore-prefixed metadata key convention must be in place. C3c introduces a parallel `CodeEnricher` that follows the same pattern but operates on source files.

### Prerequisite (blocking)
C3c cannot start until C3a ships `start_offset: int` / `end_offset: int` on `ChunkRecord` populated from Chonkie's `chunk.start_index` / `chunk.end_index`. This is the same blocking prerequisite that C3b declares; the offsets are the universal seam every C3 enricher relies on.

## Coordinated cross-brief adjustments
The following items coordinate C3c with its companion briefs. Listed here as the single canonical inventory.

1. **Rename of code-symbol classification field**: C3c's chunk-level code-symbol classification is named `_symbol_subtype` (not `_source_subtype`). The name `_source_subtype` is reserved for C3b's pipeline-level file-format dispatch discriminator with values `"pdf"` and `"image"`. The two fields exist at different granularities (file-format vs chunk-level code symbol) and serve different purposes; reusing the same name would collide on dispatch semantics.
2. **Stale C3a footnote (mandatory cleanup in C3c PR)**: C3a's "Key Decisions" entry on `_source_subtype` anticipates C3c introducing `_source_subtype` for code classification (e.g., "python-function vs python-class"). That footnote becomes stale once C3c lands using `_symbol_subtype` instead. **The C3c PR must update the C3a footnote in the same change** to read: "C3c introduces a separate `_symbol_subtype` field for code-symbol classification; `_source_subtype` is reserved exclusively for C3b's pipeline-level file-format dispatch (`pdf`, `image`)." Not deferred to a follow-up — leaving the footnote stale creates a live documentation contradiction across briefs.
3. **C3b's `_source_subtype` role is exclusive**: pipeline-level dispatch values (`"pdf"`, `"image"`) remain C3b's domain. C3c does not write to `_source_subtype`.

## Core Flow
1. At ingest, `pipeline.py` checks the file extension to determine if the file is a code file (using a language map derived from tree-sitter grammar availability, scoped to extensions in `parser.py:_PLAIN_EXTENSIONS` that have a grammar package available).
2. For code files in `_PLAIN_EXTENSIONS` (`.py`, `.js`, `.ts`, `.go`, `.rs`, `.java`, `.sh`), the parser's output is a pass-through of the raw source bytes (`parser.py:_parse_plain` calls `path.read_text()` with no markdown conversion). The source text and the chunker input text are the same string in the same coordinate space — no offset transformation is needed.
3. `CodeEnricher.preprocess(text)` runs on that text. It uses `tree-sitter` + the appropriate language grammar package to parse into an AST.
4. The AST is walked to build a sorted list of `(start_offset, end_offset, symbol_type, function_name, class_name)` tuples for every function/method, class, and module-level scope. This becomes the `scope_table`.
5. The chunker runs on the same text. Each `ChunkRecord` has `start_offset`/`end_offset` in the same coordinate space as the `scope_table`.
6. For each chunk, `CodeEnricher.enrich_chunk(chunk, scope_table)` resolves the innermost containing scope by offset lookup → writes `_symbol_type`, `_containing_function`, `_containing_class`, `_module_path`, `_symbol_subtype`.
7. If tree-sitter parsing fails (parse error on a specific file), the chunk is stored without code enrichment fields. A warning is logged; ingest is not aborted.

### Pipeline integration
`CodeEnricher` implements the same protocol as `MarkdownEnricher`:

- **`CodeEnricher.preprocess(text) -> (cleaned_text, scope_table)`** — called by `pipeline.py` once after the parser returns text, before the chunker runs. For code files `cleaned_text == text` (no marker removal, no content transformation). Returns the scope table.
- **`CodeEnricher.enrich_chunk(chunk, scope_table) -> dict[str, str]`** — called per chunk after the chunker emits each `ChunkRecord`. Returns the metadata fragment to merge into the chunk's metadata dict.

Dispatch in `pipeline.py`: file extension determines which enricher runs. Text formats (`.md/.txt/.rst/.html`) and docling-parsed formats (`.pdf` + images) route to `MarkdownEnricher`. Code extensions (the subset of `parser.py:_PLAIN_EXTENSIONS` that have a tree-sitter grammar available at import time) route to `CodeEnricher`. The two enrichers do not compose on the same file.

The source text IS the enricher input — there is no "raw source vs markdown" split for plain-extension code files.

## In Scope
- Fields:
  - `_symbol_type`: chunk-level classification — `function`, `method`, `class`, `module` (module-level code)
  - `_containing_function`: name of the innermost function or method the chunk falls within (`""` if none)
  - `_containing_class`: name of the containing class (`""` if none)
  - `_module_path`: dotted module path derived from the source file path relative to the collection root (e.g., `archon_search.store`)
  - `_symbol_subtype`: language + symbol type (e.g., `python-function`, `typescript-class`, `python-module`)
- Language matrix (v1): **Python (mandatory)** and **TypeScript (mandatory)** as the cross-language regression baseline. Other languages with `_PLAIN_EXTENSIONS` coverage (Go, Rust, Java, JS, sh) are deferred — fixtures may be added later, but tree-sitter grammar packages still install via the optional `[code]` group so degradation paths are exercised.
- Dependency: `tree-sitter` core + language grammar packages, added as an optional dependency group `[code]`. Versions are pinned in `pyproject.toml` because the core `tree-sitter` package and each language grammar share a coupled ABI; mismatched versions raise on import and are handled as a missing grammar (see Edge Cases).
- Verification: unit tests on `CodeEnricher` output asserting exact field values against fixture files (see Verification section). Symbol-aware retrieval quality is measured at this layer, not via the macro eval harness.
- Eval queries: code-corpus queries added to `tests/eval/queries.jsonl` and `labels.jsonl` that target code chunks whose FTS content makes them uniquely retrievable when symbol metadata is correctly indexed. The eval harness is deterministic and label-blind / metadata-blind (per `CLAUDE.md` and `tests/eval/README.md`), so it cannot gate on "symbol-scope labels" directly — these queries serve only as a coarse FTS-side regression check. Maintenance procedure per `tests/eval/README.md`.

## Out of Scope
- Non-code files — `MarkdownEnricher` (C3a, C3b) handles those; `CodeEnricher` is strictly for code source files.
- Symbol graph / call graph — what _calls_ what is a separate feature; this brief is about containment only.
- Cross-file symbol resolution (e.g., resolving imported names) — out of scope; only intra-file structure.
- Exposing `_symbol_type`/`_containing_class` as typed filter fields in `SearchFilters` — metadata dict is filterable; dedicated filter support deferred.
- Auto-installing tree-sitter grammars at runtime — grammars are declared in `pyproject.toml` optional deps; not downloaded on demand.
- Macro-level symbol-aware eval gating — would require extending the eval harness to be metadata-aware; tracked as a Future Iteration.
- Non-plain-extension code-like formats (e.g., Jupyter notebooks that require markdown conversion before symbol extraction) — out of scope for v1; if/when added they require their own offset-alignment design.

## Key Decisions
- **`_symbol_subtype` rather than `_source_subtype`**: Avoids collision with C3b's pipeline-level dispatch discriminator. See "Coordinated cross-brief adjustments".
- **tree-sitter over language-specific parsers (e.g. stdlib `ast`)**: The user explicitly chose universality. tree-sitter provides a consistent API across all languages, and grammar packages are available for >100 languages on PyPI. stdlib `ast` would cover Python only and create a divergent code path.
- **Optional dep group `[code]`**: tree-sitter + grammar packages add non-trivial install size. Operators who don't ingest code shouldn't pay for it. Pattern follows the existing `[multilingual]` optional group (C2).
- **Graceful degradation on parse failure**: A parse error on one file must not fail the ingest job. Code files with unknown extensions or unsupported grammars simply skip enrichment. Logged at `WARNING` level with the file path (subject to per-job aggregation; see Edge Cases).
- **Source text is enricher and chunker input (single coordinate space)**: For all v1 code extensions (`parser.py:_PLAIN_EXTENSIONS`), the parser is a raw `path.read_text()` pass-through. Source bytes == parser output bytes == chunker input bytes. No offset transformation is needed between symbol scope offsets and chunk offsets.
- **`_symbol_subtype` at chunk level, not file level**: A file containing both a class and module-level code produces chunks with different `_symbol_subtype` values. This was the explicit user decision in refinement.
- **Innermost scope wins**: A method inside a class → `_symbol_type = method`, `_containing_class = ClassName`. Nested functions → innermost function wins for `_containing_function`. A nested class inside a method inside a class → the innermost class wins for `_containing_class`.
- **`_module_path` derivation algorithm**: see "`_module_path` derivation" below.
- **`_module_path` stored per-chunk despite being file-level**: Duplication is accepted to keep the metadata model uniform (one filter-able dict per chunk). The duplication cost is negligible relative to embedding/text size. Future iteration: lift to a document-level column if query patterns warrant.

### `_module_path` derivation
Algorithm, applied in order:
1. Determine collection root (corpus directory). If absent, fallback is the file stem only (no dots).
2. Compute the relative POSIX path from root to file.
3. Strip the extension (one extension only — `.d.ts` becomes `<name>.d`, intentional; documented).
4. Replace `/` with `.`.
5. `__init__.py` resolves to the parent package (drop the trailing `.__init__`).
6. Hyphens in path segments are replaced with `_` for Python files only (Python's dotted-path convention requires identifier-safe segments); left as-is for other languages.
7. `index.ts` / `index.js` resolves to the parent directory (Node convention — drop the trailing `.index`).

Worked examples (collection root = `/repo`):
- `/repo/archon_search/store.py` → `archon_search.store`
- `/repo/archon_search/jobs/__init__.py` → `archon_search.jobs`
- `/repo/types/lib.d.ts` → `types.lib.d`
- `/repo/my-pkg/mod.py` → `my_pkg.mod` (Python hyphen→underscore)
- `/repo/web/api/index.ts` → `web.api`
- No collection root determinable, file = `/tmp/foo.py` → `foo`

## Edge Cases & Constraints
- **Chunk spans a scope boundary** (e.g., last line of a function + first line of the next): The chunk is assigned the scope containing its *start offset*. This is consistent with the C3a heading-resolution convention.
- **Nested classes and functions**: Walk resolves scopes at every nesting level; innermost wins. `_containing_class` is the nearest enclosing class, not the outermost.
- **Class body chunk** (chunk between `class Foo:` and the first method — class-level attributes, docstring): `_symbol_type = "class"`, `_containing_class = "Foo"`, `_containing_function = ""`.
- **Decorated functions/classes**: Behavior is verified per supported language. For the v1 matrix (Python, TypeScript), decorator nodes are placed outside the function node and the enricher extends the function scope's start offset backwards to include any preceding decorator nodes. New languages added later require explicit decorator-attribution tests.
- **Anonymous functions / lambdas** (incl. arrow functions assigned to a const in JS/TS): treated as enclosing-scope code; `_symbol_type` and containing-name fields are the enclosing scope's values. Named closures (assigned to a variable) are captured if the grammar exposes them.
- **Source-to-chunker coordinate space**: For all v1 code extensions, source offsets and chunker offsets share the same coordinate space. No transformation is needed.
- **Grammar not installed for an otherwise supported extension** (e.g., operator uses `pip install archon-search` without `[code]`, or grammar import fails on ABI mismatch): `CodeEnricher` is skipped for that language with a one-time `INFO` log per process. Not a warning per file.
- **Parse failure on a specific file**: The chunk is stored without enrichment fields. A `WARNING` is emitted per file; the per-job ingest summary aggregates "N files failed to parse". To avoid log spam, the per-file WARNING is downgraded to DEBUG after the first K (suggested K = 10) failures within a single ingest job; the aggregated count is always reported at INFO at job end.
- **Backward compatibility / mixed-state collections**: Pre-C3c chunks lack the new fields. Queries or filters referencing them must treat absence as no-match (not error). Re-ingest (or `reindex`) retroactively populates the fields for files matching the code-extension set.
- **50-field metadata limit**: C3c adds at most 5 fields (on top of C3a's 2 and C3b's up-to-2). Total ≤ 9 system fields.
- **Large files**: tree-sitter parses in O(n) time and is designed for large files. No special handling needed; the enricher should be benchmarked against files >10k lines.
- **tree-sitter ABI compatibility**: Core `tree-sitter` and language grammars share a coupled ABI. Mismatched versions raise on import; the failure is handled as "grammar not installed" (degradation path 2 below).

### Graceful degradation paths (single coherent flow)
1. File extension not in the code-extension map → not a code file; `CodeEnricher` not invoked.
2. Extension is in the map but the grammar package is not importable (missing dep or ABI mismatch) → `CodeEnricher` skipped at startup for that language; one-time `INFO` log per process.
3. Grammar present but parse fails on a specific file → chunk stored without enrichment fields; one `WARNING` per file, capped per job (see above).

## Verification
Fixtures live under `tests/fixtures/code/`. v1 fixtures:

- `tests/fixtures/code/python/sample.py` — module docstring, top-level function `top_fn`, class `Outer` containing method `outer_method`, nested class `Inner` with method `inner_method`, a module-level statement after the class, decorated function `decorated_fn`.
- `tests/fixtures/code/typescript/sample.ts` — top-level function, class with method, an exported const arrow function (anonymous-like body assigned to a name).

Test cases, each with setup, input, assertion:

- **`_symbol_type` enumeration (per value)** — using `sample.py`:
  - Chunk overlapping `top_fn` body → `_symbol_type == "function"`, `_containing_function == "top_fn"`, `_containing_class == ""`.
  - Chunk overlapping `outer_method` body → `_symbol_type == "method"`, `_containing_function == "outer_method"`, `_containing_class == "Outer"`.
  - Class body chunk (between `class Outer:` and first method) → `_symbol_type == "class"`, `_containing_class == "Outer"`, `_containing_function == ""`.
  - Module-level chunk (the statement after the class) → `_symbol_type == "module"`, both containing-name fields `""`.
- **Nested class containing nested function** — chunk inside `Inner.inner_method` → `_containing_class == "Inner"` (innermost wins), `_containing_function == "inner_method"`.
- **Decorator attribution** — chunk that starts at the decorator line of `decorated_fn` → `_containing_function == "decorated_fn"`.
- **Anonymous arrow function** (TypeScript) — chunk inside the exported const arrow function body → `_symbol_type` is the enclosing scope's type (module-level in this fixture), per the documented rule. Assert explicitly.
- **`_module_path` derivation** — explicit unit tests:
  - regular Python path (`archon_search/store.py` → `archon_search.store`)
  - `__init__.py` (`archon_search/jobs/__init__.py` → `archon_search.jobs`)
  - `.d.ts` (`types/lib.d.ts` → `types.lib.d`)
  - hyphenated directory, Python (`my-pkg/mod.py` → `my_pkg.mod`)
  - hyphenated directory, non-Python (left as-is)
  - no collection root → fallback to stem
  - `index.ts` (`web/api/index.ts` → `web.api`)
- **Parse failure** — `.py` fixture with `def foo(:` (deliberately broken). Assert: chunk is stored, no enrichment fields present, single WARNING log per file.
- **Grammar missing** — monkey-patch grammar import (e.g., `monkeypatch.setitem(sys.modules, "tree_sitter_python", None)`, or use the enricher's grammar registry). Assert: ingest succeeds, one-time INFO log, no enrichment fields on any chunk.
- **Mixed-state collection (backward compat)** — Setup: insert two `ChunkRecord` rows directly via `Store.add_chunks()` with metadata dicts that omit the C3c fields (simulating pre-C3c ingest). Then re-ingest the same `.py` file via the pipeline. Assert: pre-existing rows are deleted-and-reinserted with C3c fields populated; chunks from non-code files in the same collection remain unmodified.
- **Latency** — tree-sitter parse + AST walk are O(n) over file size. No new ingest-latency gate is proposed; the `[search_filtered]` p95 threshold in `tests/eval/thresholds.toml` remains the search-path gate. Ingest hot-path impact is bounded by file size.

## Open Questions
- None — all decisions resolved in refinement.

## Future Iterations
- `_symbol_signature`: function/method signature (parameter names + return type annotation) — high value for API documentation search.
- Typed filter support: `SearchFilters.symbol_type`, `SearchFilters.containing_class` — cleaner API than raw metadata dict queries.
- Cross-file resolution: resolve imported symbol origins for deeper context.
- Incremental enrichment: re-enrich only changed files on sync (currently a full reindex is triggered on chunk-size change).
- Macro-level symbol-aware eval gating: requires extending the deterministic eval harness to read metadata (currently label/metadata-blind by design).
- Add Go / Rust / Java / JS / sh fixtures to expand the language matrix beyond Python + TypeScript.
- Lift `_module_path` to a document-level column if query patterns warrant.

## Recommendation
C3c lands as a standalone sprint after C3a (which ships the `ChunkRecord` offsets) and benefits from landing after C3b (which establishes the two-pass `preprocess` / `enrich_chunk` enricher contract). The "offset alignment between raw source and markdown" risk does not exist for v1 code extensions — they are plain-text pass-throughs in `parser.py:_PLAIN_EXTENSIONS`, so source and chunker share one coordinate space. The genuine remaining risk is tree-sitter grammar coverage and edge-case correctness across languages (decorators, nested closures, anonymous functions): the verification matrix above is the gate. Python and TypeScript are mandatory in v1; other grammars install but are exercised only through degradation paths until fixtures are added.
