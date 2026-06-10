# Feature Brief: C3a — Markdown Structural Enrichment

## Problem
Chunks produced by the ingest pipeline carry no structural context from their source document. A chunk containing "Install the package with pip" is indistinguishable from the same sentence under a "Quickstart" heading versus a "Troubleshooting" heading. This degrades both reranker scoring (no section signal) and filter precision (no way to scope to a subsection).

## Goal
Every text-format chunk stored in LanceDB carries two auto-populated metadata fields — `_heading` and `_section_path` — derived at ingest time from the document's markdown structure, without any user-supplied YAML. These fields are present in search results (via `include_metadata=True`) and available for future filter expressions once dedicated filter support is added.

## Users & Context
Developers and operators who ingest structured documentation (API docs, manuals, READMEs) and then query specific sections. They want "find the authentication section" to return chunks from that section, not every mention of "authentication" across the corpus.

## Core Flow
1. Parser converts source file to markdown text (existing path).
2. `_extract_front_matter()` strips user-supplied YAML metadata (existing path). The enricher receives this post-front-matter-stripped text.
3. New `MarkdownEnricher` is invoked in `pipeline.py:ingest_file()` between the `_extract_front_matter()` call (current line ~240) and the `chunk()` call (current line ~275). The enricher receives the post-front-matter-stripped text, builds the heading offset table and the offset-aligned heading stack, and returns it for use in step 5. **The MarkdownEnricher has a two-phase API: (1) `prepare(text) -> HeadingTable` — called before chunking; scans the text and returns an immutable heading offset table. (2) `enrich_chunk(chunk, table) -> dict[str, str]` — called per chunk after `chunk()` returns; bisects the table and returns the metadata fragment to merge into `chunk.metadata`.** **Ordering invariant**: `prepare()` must receive the exact same text instance that `chunk()` receives — after all prior transformations (front-matter stripping in C3a; additionally, marker removal in C3b). The heading offset table is only valid for offsets in that final text; computing it against raw parser output would misalign headings if any transformation shifts characters. Note: C3b extends this class and names its pre-chunking method `preprocess(text)`. Implementers should use `prepare()` as the base-class method name for C3a; C3b's brief defines the extension interface.
4. `chunk()` is called. **ChunkRecord requires two new transient fields: `start_offset: int` and `end_offset: int`, populated from chonkie's `Chunk.start_index` / `Chunk.end_index`.** These fields exist only in memory during the ingest run — they are not persisted to LanceDB. No schema migration is needed. DocumentChunker currently discards chonkie's index values; adding propagation is in-scope prerequisite work for C3a.
5. For each chunk, the enricher looks up the last heading whose start offset is ≤ chunk's `start_offset` → writes `_heading` and `_section_path` into `ChunkRecord.metadata`.
6. Chunks with no preceding heading get `_heading = ""` and `_section_path = ""` (not omitted).
7. Enriched fields use underscore-prefixed keys stored in the existing `metadata: dict[str, str]` on ChunkRecord. Front matter keys are not applied to chunk metadata today (only `_acl` is propagated), so key collision with user YAML is avoided by the underscore-prefix convention alone. If a future feature wires front-matter-to-metadata, the underscore prefix provides a stable namespace boundary.

## In Scope
- Fields: `_heading` (nearest heading text), `_section_path` (e.g., `"Installation > macOS > Homebrew"`)
- Extend `ChunkRecord` with transient `start_offset: int` and `end_offset: int` fields; extend `DocumentChunker.chunk()` to populate them from chonkie's `Chunk.start_index` / `Chunk.end_index`. These fields are not written to LanceDB, not in `_schema()`, not in `_do_ingest()`.
- Formats: all text-format files (`.md`, `.txt`, `.rst`, `.html`) — i.e., files that already go through front-matter extraction
- Heading extraction: ATX-style (`# H1`, `## H2`, ...) via stdlib `re` — no new dependency (applies to `.md` and `.txt` only; RST files use the underline heuristic exclusively; HTML files get best-effort ATX from trafilatura output)
- Setext headings (`===`, `---` underlines) are recognised but treated as H1/H2
- Fenced code block exclusion from heading detection (backtick and tilde fences)
- Key collision avoidance via `_`-prefix convention (front-matter-to-metadata propagation is not in scope for C3a)
- HTML heading extraction is best-effort (see Edge Cases)
- RST heading detection is a best-effort heuristic with documented limitations (see Edge Cases)
- Schema: `_heading` and `_section_path` are stored in the existing `metadata: dict[str, str]` field — no LanceDB schema migration needed. `start_offset` and `end_offset` are transient fields on `ChunkRecord` (not written to LanceDB, not in `_schema()`, not in `_do_ingest()`). They exist only between `chunk()` returning and the per-chunk enrichment loop completing. `SearchResult` needs NO changes — `_heading`/`_section_path` appear via the existing `metadata` dict; transient offset fields are never in `SearchResult`.
- Backfill strategy: enrichment fields are populated on next re-ingest; backfill of enrichment fields for pre-C3a collections requires a full re-ingest (delete + re-ingest).
- Verification via unit tests that directly assert metadata field values: given a fixture markdown document with known heading structure, each ChunkRecord's `_heading` and `_section_path` must match expected values. These tests live in `tests/test_enricher.py`. A test in `tests/test_chunker.py` verifies that `DocumentChunker.chunk()` populates `start_offset` and `end_offset` on every returned `ChunkRecord`, that `text[chunk.start_offset:chunk.end_offset] == chunk.text` holds for all non-empty chunks (confirming character-offset semantics), and that these fields are NOT present in the LanceDB schema (i.e., `_schema()` does not include them and `_do_ingest()` does not write them). An integration test verifies the fields survive the LanceDB store round-trip: ingest a markdown document with headings, query via `SearchStore.hybrid_search()`, assert `_heading` and `_section_path` are present in `result.metadata` on matching chunks. The integration test explicitly verifies the JSON round-trip: `_heading` and `_section_path` keys survive `json.dumps` (on write via `_do_ingest`) → LanceDB storage → `parse_metadata` deserialization (on read) without being stripped, renamed, or mutated.
- Eval harness: no change to eval queries in this feature — heading enrichment improves retrieval quality indirectly; baseline drift (if any) will appear in the regular eval run.

## Out of Scope
- Page numbers — covered by C3b (depends on PDF-specific docling change)
- Code symbol extraction — covered by C3c
- SQL-level filtering by `_heading`/`_section_path` — the metadata dict is a JSON string column; per-key filtering requires typed columns added in a future hardening item
- Reranker changes — enrichment fields are stored; reranker prompt templating is a separate concern
- Non-text formats (PDF, DOCX, images) — heading extraction from these formats is not a goal of C3a. C3b extends `MarkdownEnricher` to also run on docling-produced markdown for PDF/image inputs (heading extraction may yield results for PDF docs with heading markup, though this is incidental).

## Key Decisions
- **stdlib regex over markdown-it-py**: Heading extraction from ATX markdown is a well-defined regex (`^#{1,6}\s+(.+)$`). Adding `markdown-it-py` for this one use-case adds a dep without meaningful accuracy gain for heading-only extraction. Revisit if richer AST traversal is needed later.
- **Underscore-prefixed keys** (`_heading`, `_section_path`): Signals system-generated fields and avoids collision with arbitrary user YAML keys.
- **Character-offset lookup, not line-scan at chunk time**: The enricher pre-computes a sorted list of `(offset, heading_text, level)` tuples from the full document, then resolves each chunk with `bisect`. This keeps enrichment O(n log n) regardless of chunk count.
- **Empty string, not omitted**: Chunks with no heading set `_heading = ""` to keep query behaviour predictable (filter `_heading != ""` works cleanly).
- **Offsets are transient on ChunkRecord**: `start_offset`/`end_offset` are populated from chonkie's Chunk indices during `ingest_file()` and used immediately by the enricher to resolve headings. They are discarded after the enrichment loop — not written to LanceDB. This avoids schema migration and avoids exposing internal implementation details in `SearchResult`. C3b (page numbers) follows the same transient-offset pattern.
- **`_source_subtype` removed**: `_source_subtype` was considered but removed — the existing `file_type` field on `ChunkRecord` already carries the file extension. A human-readable label is a presentation concern; storing a duplicate is a metadata budget cost without retrieval value. C3c introduces `_symbol_subtype` (not `_source_subtype`) for code-level chunk classification (e.g., `python-function`, `typescript-class`). C3b introduces `_source_subtype` as its own field for pipeline-level file-format dispatch values (`pdf`, `image`) — C3a neither implements nor depends on it.

## Edge Cases & Constraints
- **Chunk starts mid-heading line**: The heading scanner records the *start* offset of the heading line (the position of `#`). The bisect lookup uses `<=` so a chunk starting anywhere within or after a heading line is assigned that heading. A chunk that starts before the first heading gets empty strings.
- **Fenced code blocks**: The heading scanner skips content inside triple-backtick (` ``` `) and triple-tilde (`~~~`) fences (both are CommonMark-valid fence markers). Implementation: before scanning for headings, track open/close fence positions and exclude those ranges. ATX headings inside code fences are Python/shell comments, not document structure.
- **Deeply nested headings** (H4–H6): Included in `_section_path` but H4+ are uncommon in practice; no special handling needed.
- **Heading text truncation**: `_heading` values are capped at 512 chars (well within the 4096-char metadata value limit). Long headings are truncated with `…`.
- **`_section_path` truncation**: If the concatenated section path exceeds 512 chars, truncate from the left (drop outermost ancestors first), keeping the deepest path that fits. This preserves the most locally relevant context. Note: left-truncation favours local context (deepest ancestor) over global context (outermost ancestor). For queries that rely on a top-level section name (e.g., 'API Reference') to disambiguate, this means deeply nested paths under long hierarchies may lose that outermost signal. This is an accepted tradeoff — the innermost heading is typically the most immediately relevant to the chunk's content.
- **Heading level jumps** (e.g., H1 followed directly by H3): The heading stack treats H3 as a child of the most recent heading with level < 3, regardless of level gaps. No synthetic intermediate headings are inserted. `_section_path` reflects actual document structure, not an idealized hierarchy.
- **50-field metadata limit**: C3a adds at most 2 fields; the existing limit is not approached.
- **RST heading detection**: RST files are read as raw text (`path.read_text()`) — no markdown conversion occurs. The ATX heading regex does not apply to RST files. Heading detection for RST relies solely on the underline heuristic: scan for lines consisting entirely of a single repeated underline character (`=`, `-`, `~`, `^`, `"`, `#`), where the preceding non-blank line is the heading text. Heading levels in RST are determined by order-of-first-appearance of underline characters, which cannot be reliably inferred without a full RST parser. RST heading detection is implemented as a best-effort heuristic: the first underline character seen is treated as H1, the second distinct character as H2, etc. Detected headings may be at the wrong level in non-standard RST documents. This is documented as a known limitation.
- **HTML files**: trafilatura strips all tags before the enricher receives the text. Heading detection for `.html` falls back to ATX-style heuristics in trafilatura's plain-text output (rare). HTML is included in scope but heading enrichment for HTML files is best-effort and expected to produce empty `_heading`/`_section_path` in most cases. This is documented and not an error.
- **Front-matter stripping**: The enricher receives post-front-matter text (same as the chunker), so YAML blocks don't produce spurious headings.
- **All ingest paths covered**: All ingest entry points (CLI `ingest` command, HTTP `/ingest` endpoint, watcher/sync-triggered ingestion) route through `pipeline.py:ingest_file()`, so heading enrichment applies uniformly without special-casing.
- **Setext `---` vs thematic break**: A `---` line is treated as a Setext H2 underline only when the immediately preceding non-blank line is a non-empty, non-underline-character line (i.e., it is not itself a sequence of `=`, `-`, `~`, `^`, `"`, or `#` characters). If the preceding non-blank line is blank, an underline sequence, or absent, the `---` is treated as a thematic break and ignored for heading purposes.
- **Existing collections**: Chunks ingested before C3a have no `_heading`/`_section_path`. Queries filtering `_heading != ''` will exclude legacy chunks. Backfill requires re-ingest.
- **Reindex compatibility**: `store.reindex_metadata()` updates only `file_type`, `updated_at`, `ingested_by`, and `indexed_at` — it never reads or writes the `metadata` dict column. Enrichment fields (`_heading`, `_section_path`) survive a metadata reindex unchanged. Backfill of enrichment fields for pre-C3a collections requires a full re-ingest (delete + re-ingest), not a metadata reindex.

## Open Questions
- None — all decisions resolved in refinement.

## Future Iterations
- Expose `_heading` and `_section_path` as typed filter fields in `SearchFilters` (not just raw metadata dict) for cleaner API ergonomics.
- Feed `_section_path` into the reranker prompt as additional context.
- Extend to non-text formats once C3b establishes docling enrichment infrastructure.

## Recommendation
C3a is the right first move. It's zero-dep, covers every text-format ingest path, and provides the metadata infrastructure that C3b and C3c build on. The prerequisite work (adding `start_offset`/`end_offset` to `ChunkRecord`) is small and shared with C3b. RST heading detection is best-effort by design — keep the heuristic simple and document the limitation rather than chasing AST fidelity. The verification gate is unit tests asserting exact field values against fixture documents, not eval queries.
