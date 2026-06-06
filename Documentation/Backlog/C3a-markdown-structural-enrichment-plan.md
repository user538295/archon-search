# C3a — Markdown Structural Enrichment
**Purpose**: Enrich every text-format chunk at ingest time with `_heading` and `_section_path` metadata fields derived from the document's heading structure, so operators can locate chunks by section.
**Audience**: archon-search contributors implementing C3a; reviewers of the resulting PRs.
**Status**: To Do

---

## Background

Chunks in LanceDB carry no structural context from their source document. A chunk containing "Install the package with pip" is indistinguishable from the same sentence under a "Quickstart" heading versus a "Troubleshooting" heading. This degrades reranker scoring and filter precision.

The full design, key decisions, and edge cases are in `Documentation/Backlog/C3a-markdown-structural-enrichment-brief.md`.

---

## Goal

After C3a ships: every text-format chunk stored in LanceDB carries `_heading` (nearest preceding heading text) and `_section_path` (e.g. `"Installation > macOS > Homebrew"`) in its `metadata` dict. These fields are populated automatically at ingest time from the document's heading structure — no user YAML required. They are present in search results via `include_metadata=True` and serve as the metadata infrastructure that C3b and C3c build on.

---

## Scope

### In Scope
- `_heading` (nearest heading text, capped at 512 chars) and `_section_path` (capped at 512 chars, left-truncated) stored in `ChunkRecord.metadata`
- Transient `start_offset: int` and `end_offset: int` fields on `ChunkRecord` (not persisted to LanceDB, not in `_schema()`, not in `_do_ingest()`)
- `DocumentChunker.chunk()` propagates chonkie's `start_index`/`end_index` to these transient fields
- ATX headings (`#`–`######`) and setext headings (`===`/`---` underlines) for `.md` and `.txt`
- RST heading detection via underline heuristic (best-effort, documented limitation)
- HTML: best-effort ATX detection from trafilatura plain-text output (expected to produce empty fields in most cases)
- Fenced code block exclusion (backtick and tilde fences)
- Chunks with no preceding heading get `_heading = ""` and `_section_path = ""`
- All ingest paths covered via `pipeline.py:ingest_file()` (CLI, HTTP, watcher/sync)

### Out of Scope
- Page numbers (C3b)
- Code symbol extraction (C3c)
- SQL-level filtering by `_heading`/`_section_path` as typed columns
- Reranker prompt integration
- Non-text formats (PDF, DOCX, images)
- Front-matter-to-metadata propagation (not in scope for C3a; underscore prefix provides namespace boundary)

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 4.1 — Final verification & documentation update].

---

## What does NOT change
- LanceDB schema (`_schema()`) — no new columns; `start_offset`/`end_offset` are transient only
- `_do_ingest()` — no new fields written; `metadata` JSON serialization is unchanged
- `SearchResult` shape — `_heading`/`_section_path` surface via the existing `metadata` dict
- `store.reindex_metadata()` — does not touch the `metadata` dict column; enrichment fields survive reindex unchanged
- `validate_metadata()` constraints — C3a adds at most 2 fields; the 50-field limit is not approached
- All existing tests

---

## Known limitations / accepted trade-offs
- RST heading levels are order-of-first-appearance; may be wrong in non-standard RST. Documented in code.
- HTML heading enrichment is best-effort; expected to produce empty fields in most cases.
- Left-truncation of `_section_path` beyond 512 chars drops outermost ancestors — locally relevant context is preferred over global hierarchy.
- Heading level jumps (H1 → H3) produce no synthetic intermediates; `_section_path` reflects actual document structure.
- Setext `---` is disambiguated from thematic breaks only when the preceding non-blank line is a valid heading text (not blank, not itself an underline sequence).
- Backfill of pre-C3a collections requires full re-ingest; metadata reindex does not populate enrichment fields.
- If a single heading text exceeds 512 chars, both `_heading` and the deepest element of `_section_path` are truncated to 511 chars + `…`.
- ATX headings without closing hashes (`## Heading`) and with closing hashes (`## Heading ##`) both work correctly; closing hashes are stripped from `_heading` and `_section_path`.

---

## Architecture

### New module
- `archon_search/enricher.py` — `MarkdownEnricher` class with:
  - `HeadingEntry = namedtuple("HeadingEntry", ["offset", "text", "level"])` — immutable tuple in the heading table
  - `HeadingTable = list[HeadingEntry]` — sorted by `offset` ascending; passed between `prepare()` and `enrich_chunk()`
  - `prepare(text: str) -> HeadingTable` — scans the post-front-matter text, builds and returns the immutable heading offset table. Excludes headings inside fenced code blocks.
  - `enrich_chunk(chunk: ChunkRecord, table: HeadingTable) -> dict[str, str]` — bisects `table` on `chunk.start_offset` to find the nearest preceding heading, then builds `_heading` and `_section_path`. Returns the two-key metadata fragment to merge into `chunk.metadata`.

### Changes to existing modules
- `archon_search/_types.py` — `ChunkRecord` gains two transient fields: `start_offset: int = -1` and `end_offset: int = -1`. These are not in `_schema()` or `_do_ingest()`; they exist only between `chunk()` returning and the enrichment loop completing.
- `archon_search/chunker.py` — `DocumentChunker.chunk()` assigns `chunk.start_index` → `record.start_offset` and `chunk.end_index` → `record.end_offset` for each chonkie Chunk.
- `archon_search/pipeline.py` — `ingest_file()` is extended:
  1. `enricher = MarkdownEnricher()` is instantiated unconditionally; for `is_text_type` files, `heading_table = enricher.prepare(markdown)` is called after front-matter stripping and before `self._chunker.chunk()`; for non-text files, `heading_table = []`
  2. After `self._chunker.chunk()` returns: loop over all records unconditionally, call `enricher.enrich_chunk(record, heading_table)`, merge the two-key dict into `record.metadata` (empty strings for non-text files with empty table)

### Data flow
```
parse() → _extract_front_matter() → [if text type] enricher.prepare(text) → chunk() →
  [for each record] enricher.enrich_chunk(record, heading_table) → merge into record.metadata →
  assign chunk IDs → embed → store
```

### No new config keys, env vars, or API changes

---

## Task breakdown

### Phase 1 — Transient offset fields on ChunkRecord
> **Releasable**: after Task 1.2 — `DocumentChunker.chunk()` propagates character offsets to every `ChunkRecord`; the enricher can use them in Phase 2.

#### Task 1.1 — Add transient `start_offset`/`end_offset` to `ChunkRecord`
- [x] **File**: `archon_search/_types.py`
- **Depends on**: nothing
- **Description**:
  - Add two new fields after the `ingested_by` field in `ChunkRecord`:
    ```python
    start_offset: int = -1
    """transient: character offset of chunk start in the post-front-matter text. Not persisted to LanceDB."""
    end_offset: int = -1
    """transient: character offset of chunk end (exclusive) in the post-front-matter text. Not persisted to LanceDB."""
    ```
  - Both default to `-1` (sentinel meaning "unpopulated / not applicable"). Binary formats that skip enrichment will retain `-1`.
  - No changes to `_schema()` or `_do_ingest()` in `store.py` — these fields must not appear in any LanceDB write path.
- **Releasable**: after this task, `ChunkRecord` accepts transient offsets without breaking existing construction sites.
- **Tests (TDD)** — `tests/test_types.py`:
  - Unit: `test_chunk_record_default_offsets` — construct a minimal `ChunkRecord` and assert `start_offset == -1`, `end_offset == -1`
  - Unit: `test_chunk_record_offset_fields_not_in_schema` — import `SearchStore._schema` and assert neither `"start_offset"` nor `"end_offset"` appears in `[f.name for f in schema]`
  - Checkpoint: `uv run pytest tests/test_types.py -x`

#### Task 1.2 — Propagate chonkie offsets in `DocumentChunker.chunk()`
- [x] **File**: `archon_search/chunker.py`
- **Depends on**: Task 1.1
- **Description**:
  - In the list comprehension / loop that constructs `ChunkRecord` instances from chonkie `chunks`, assign:
    ```python
    start_offset=chunk.start_index,
    end_offset=chunk.end_index,
    ```
  - `chunk.start_index` and `chunk.end_index` are character offsets into the `text` argument that was passed to `self._chunker.chunk(text)`. `text[chunk.start_index:chunk.end_index]` equals `chunk.text` for all non-empty chunks.
  - No other changes to `DocumentChunker`.
- **Releasable**: after this task, every `ChunkRecord` returned by `DocumentChunker.chunk()` carries accurate character offsets.
- **Tests (TDD)** — `tests/test_chunker.py`:
  - Unit: `test_chunk_offsets_populated` — call `chunker.chunk(text, ...)` on a short text string; assert every returned record has `start_offset >= 0` and `end_offset > start_offset`
  - Unit: `test_chunk_offset_text_slice_matches` — for all non-empty chunks, assert `text[record.start_offset:record.end_offset] == record.text`
  - Note: the non-persistence guarantee is already verified by `test_chunk_record_offset_fields_not_in_schema` (Task 1.1) which checks `_schema()` directly. Since `_do_ingest()` explicitly enumerates fields (not `dataclasses.asdict()`), schema absence is sufficient verification.
  - Integration: `test_chunk_offsets_absent_from_lancedb_row` — in `tests/test_store_ingest_metadata.py`, ingest a single chunk with `start_offset=5`, `end_offset=10`; query the row directly from LanceDB; assert `"start_offset" not in row` and `"end_offset" not in row`. Uses the existing `connected_store` fixture.
  - Checkpoint: `uv run pytest tests/test_chunker.py tests/test_store_ingest_metadata.py::test_chunk_offsets_absent_from_lancedb_row -x`

---

### Phase 2 — MarkdownEnricher
> **Releasable**: after Task 2.2 — `MarkdownEnricher` is fully functional and unit-tested; wiring in Phase 3 activates it end-to-end.

#### Task 2.1 — `MarkdownEnricher.prepare()` — heading scanner
- [x] **File**: `archon_search/enricher.py` (new file)
- **Depends on**: nothing
- **Description**:
  - Define `HeadingEntry = namedtuple("HeadingEntry", ["offset", "text", "level"])`.
  - Define `HeadingTable = list[HeadingEntry]` as a type alias.
  - Implement `class MarkdownEnricher`:
    - `prepare(text: str) -> HeadingTable`:
      1. **Fence exclusion**: scan `text` once with a single-pass regex to collect `(start, end)` ranges for all fenced code blocks (triple-backtick ` ``` ` or triple-tilde `~~~`). A range is `(fence_open_start, fence_close_end_inclusive)`. A fence opened with backticks is only closed by a backtick fence of equal or greater length; a fence opened with tildes is only closed by a tilde fence of equal or greater length. A backtick opener is NOT closed by a tilde fence and vice versa. Unclosed fences (no matching closer found before end-of-text) extend to end-of-text.
      2. **ATX heading scan**: find all matches of `^(#{1,6})\s+(.+?)\s*$` (multiline). For each match, skip if `match.start()` falls within any fence range. Strip optional closing hashes from the captured heading text: assign `heading_text = match.group(2).strip()`, then apply `heading_text = re.sub(r'\s+#+\s*$', '', heading_text)` to remove trailing ` ##` sequences. `## Heading ##` produces `heading_text = 'Heading'`; `## Heading` (no closing hashes) is unchanged. Append `HeadingEntry(offset=match.start(), text=heading_text, level=len(match.group(1)))`.
      3. **Setext heading scan**: scan for lines where the *immediately following non-blank line* consists entirely of `=` or `-` characters (min 2). A blank line between the heading-text line and the underline line disqualifies it as a setext heading (it becomes a thematic break). The `===` line → H1, a `---` line on the *immediately following* line forms a setext H2 only when the preceding non-blank line (with no blank between them) is a non-empty, non-underline-character-only line. Apply the disambiguation rule: the preceding non-blank line must not itself be an underline-character-only sequence and must not be blank or absent. Record offset as the start of the heading-text line, not the underline line. Skip if offset falls within a fence range.
      4. **RST heading heuristic** (applied only when no ATX headings were found — heuristic fallback): scan for lines consisting entirely of a single repeated character from `=`, `-`, `~`, `^`, `"`, `#` (min 2 chars), where the preceding non-blank line is the heading text. Map first-seen underline char to H1, second-distinct to H2, etc. Skip if within fence range.
      5. Deduplicate by offset (ATX and setext may not overlap; deduplicate defensively with a set on `offset`). Sort by `offset` ascending. Return the `HeadingTable`.
      - Edge: returns `[]` for empty text or text with no headings.
- **Releasable**: after this task, `MarkdownEnricher().prepare(text)` produces a sorted heading offset table for any text input.
- **Tests (TDD)** — `tests/test_enricher.py`:
  - Unit: `test_prepare_atx_headings` — text with `# H1`, `## H2`, `### H3`; assert 3 entries with correct `offset`, `text`, `level`
  - Unit: `test_prepare_setext_headings` — text with a setext H1 (`===`) and H2 (`---`); assert 2 entries at correct offsets with levels 1 and 2
  - Unit: `test_prepare_setext_dash_not_thematic_break` — text where `---` follows a blank line; assert it is NOT treated as a heading
  - Unit: `test_prepare_fence_exclusion_backtick` — ATX heading inside a backtick fence; assert it does NOT appear in the table
  - Unit: `test_prepare_fence_exclusion_tilde` — ATX heading inside a tilde fence; assert it does NOT appear in the table
  - Unit: `test_prepare_empty_text` — `prepare("")` returns `[]`
  - Unit: `test_prepare_no_headings` — plain prose text returns `[]`
  - Unit: `test_prepare_rst_heuristic` — RST-style underlines with no ATX headings; assert headings detected in order-of-appearance, level assigned by first-seen underline char
  - Unit: `test_prepare_sorted_by_offset` — multiheading document; assert returned table is sorted ascending by `offset`
  - Unit: `test_prepare_atx_no_space_not_matched` — text `'#NoSpace heading'`; assert the table is empty (ATX requires at least one space after `#`)
  - Unit: `test_prepare_atx_seven_hashes_not_matched` — text `'####### Seven'`; assert the table is empty (only H1–H6 supported)
  - Unit: `test_prepare_unclosed_fence` — text with an opening ` ``` ` but no closing fence, followed by an ATX heading; assert the heading is NOT in the returned table (fence extends to end-of-text)
  - Unit: `test_prepare_setext_inside_fence` — a setext heading pattern (text line followed by `===`) inside a fenced code block; assert it is NOT in the table
  - Unit: `test_prepare_fence_same_char_only` — a backtick fence (` ``` `) is NOT closed by a tilde fence (`~~~`); an ATX heading between them is NOT in the table
  - Checkpoint: `uv run pytest tests/test_enricher.py -x`

#### Task 2.2 — `MarkdownEnricher.enrich_chunk()` — per-chunk resolver
- [x] **File**: `archon_search/enricher.py`
- **Depends on**: Task 2.1, Task 1.1
- **Description**:
  - Add `enrich_chunk(self, chunk: ChunkRecord, table: HeadingTable) -> dict[str, str]` to `MarkdownEnricher`:
    1. If `table` is empty or `chunk.start_offset < 0`: return `{"_heading": "", "_section_path": ""}`.
    2. **Bisect**: use `bisect.bisect_right([e.offset for e in table], chunk.start_offset) - 1` to find the index of the last heading with `offset <= chunk.start_offset`. If the result is `-1` (chunk starts before all headings), return empty strings.
    3. The matched heading is `table[idx]`.
    4. **Build heading stack**: walk backward from `idx` toward the start of the table to build the ancestor chain. For each level `l` from 1 to `L-1`, find the entry with the highest index ≤ `idx` that has `level == l`. This gives the closest (most recent) ancestor at each level. Order the collected ancestors outermost-first (H1, H2, ..., up to and including the matched heading at index `idx`). This correctly reconstructs the path even with level jumps — if no H2 exists between the H1 and an H3, `_section_path` is `"H1 > H3"` with no synthetic intermediate.
    5. **`_heading`**: `table[idx].text`, truncated to 511 chars + `…` (Unicode U+2026, `…`, length=1) suffix if needed, giving a total `len()` of 512.
    6. **`_section_path`**: join stack texts with `" > "`. If the joined string exceeds 512 chars, left-truncate by dropping the outermost elements one by one until it fits. Never truncate the deepest (matched) element text — only drop ancestors. Exception: if the matched heading text alone exceeds 512 chars, truncate it to 511 chars + `…` (Unicode U+2026), matching the `_heading` truncation rule. This is a degenerate case; in practice heading texts are short.
    7. Return `{"_heading": heading, "_section_path": section_path}`.
  - The `enrich_chunk` signature is `enrich_chunk(self, chunk: ChunkRecord, table: HeadingTable) -> dict[str, str]` as specified in the brief. The bisect operates on `[e.offset for e in table]` computed inline from the passed `table`. For typical documents with fewer than a few hundred headings, this O(N) list build is negligible. The method is stateless — there is no instance-level offset cache, and `enrich_chunk` can be called independently of `prepare()` (e.g., in isolated unit tests that construct a `HeadingTable` directly without calling `prepare()`).
- **Releasable**: after this task, `enrich_chunk()` resolves any chunk against any heading table.
- **Tests (TDD)** — `tests/test_enricher.py`:
  - Unit: `test_enrich_chunk_before_first_heading` — chunk whose `start_offset` precedes all headings; assert `_heading == ""` and `_section_path == ""`
  - Unit: `test_enrich_chunk_under_single_heading` — chunk under one H1; assert `_heading == "H1 text"` and `_section_path == "H1 text"`
  - Unit: `test_enrich_chunk_section_path_nested` — chunk under H1 → H2 → H3; assert `_section_path == "H1 > H2 > H3"` and `_heading == "H3 text"`
  - Unit: `test_enrich_chunk_heading_level_jump` — H1 → H3 (no H2); assert `_section_path == "H1 > H3"`
  - Unit: `test_enrich_chunk_heading_truncation` — heading text > 512 chars; assert `_heading` ends with `'…'` (U+2026) and `len(_heading) == 512` (511 content chars + 1 ellipsis char)
  - Unit: `test_enrich_chunk_section_path_truncation` — fixture: H1 text of 200 chars, H2 text of 200 chars, H3 text of 100 chars, H4 (deepest) text of 50 chars; joined with ' > ' separators gives 200 + 3 + 200 + 3 + 100 + 3 + 50 = 559 chars total, which exceeds 512; assert (1) `len(_section_path) <= 512`, (2) `_section_path` ends with the exact 50-char H4 text (deepest, not truncated), (3) `_section_path` does NOT start with ' > ' (no leading separator after ancestor-dropping), (4) the H1 200-char text does NOT appear in `_section_path` (outermost ancestor dropped first). Exact heading text values are for the implementer to choose, but must satisfy these character counts.
  - Unit: `test_enrich_chunk_empty_table` — empty `HeadingTable`; assert both fields are `""`
  - Unit: `test_enrich_chunk_negative_offset` — `chunk.start_offset == -1`; assert both fields are `""`
  - Unit: `test_enrich_chunk_update_overwrites_existing_key` — construct a `ChunkRecord` with `metadata={'_heading': 'pre-existing'}`, construct a `HeadingTable` with a single H1 at `offset=0`, call `enrich_chunk(record, table)` and then `record.metadata.update(result)`; assert `record.metadata['_heading']` is the enricher's value (not `'pre-existing'`). This documents the intentional overwrite semantics of `dict.update()`.
  - Unit: `test_enrich_chunk_independent_of_prepare` — construct a `HeadingTable` directly (without calling `prepare()`), call `enrich_chunk(chunk, table)` with `chunk.start_offset` pointing into the table; assert correct `_heading` value is returned (this verifies `enrich_chunk` is stateless and does NOT require `prepare()` to be called first)
  - Unit: `test_prepare_called_twice_uses_second_table` — call `prepare(doc_A_text)`, save returned table_A; call `prepare(doc_B_text)` with different headings; call `enrich_chunk(chunk_B, table_B)` with `start_offset` pointing into doc_B's headings; assert `_heading` matches doc_B's heading (verifies no stale-cache issue)
  - Unit: `test_enrich_chunk_at_exact_heading_offset` — `chunk.start_offset` equals exactly a heading's `offset`; assert that heading IS matched (bisect boundary: `bisect_right([50], 50) - 1 = 0`)
  - Unit: `test_enrich_chunk_deepest_heading_over_512` — fixture: single H1 with text of 600 characters (no ancestor headings); assert `_heading` ends with `'…'` (U+2026) and `len(_heading) == 512` (511 content chars + 1 ellipsis); assert `_section_path` also ends with `'…'` and `len(_section_path) == 512` (deepest heading text alone, truncated to 511 + ellipsis, with no ` > ` separator since there are no ancestors)
  - Integration: `test_enricher_full_document` — fixture markdown string (inline in the test, not a file): `'# Alpha\n\nIntro text.\n\n## Beta\n\nSection text.\n\n### Gamma\n\nDeep text.\n'`; derive chunk `start_offset` values programmatically using `text.index()` on substrings ('Intro text', 'Section text', 'Deep text') rather than manual character counting. The `prepare()` return value can be inspected to verify heading offsets are at `text.index('# Alpha')`, `text.index('## Beta')`, `text.index('### Gamma')` respectively; call `prepare()` and verify 3 entries (level 1/2/3, correct offsets); then call `enrich_chunk()` with `start_offset` at the start of 'Intro text', 'Section text', 'Deep text' respectively; assert `_heading == 'Alpha'` / `'Beta'` / `'Gamma'` and `_section_path == 'Alpha'` / `'Alpha > Beta'` / `'Alpha > Beta > Gamma'`
  - Checkpoint: `uv run pytest tests/test_enricher.py -x`

---

### Phase 3 — Pipeline wiring
> **Releasable**: after Task 3.1 — every text-format file ingested through any entry point produces chunks with `_heading` and `_section_path` in `metadata`.

#### Task 3.1 — Wire `MarkdownEnricher` into `pipeline.py:ingest_file()`
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 1.2, Task 2.2
- **Description**:
  - Import `MarkdownEnricher` from `archon_search.enricher`.
  - In `ingest_file()`, after the existing `if is_text_type:` block that calls `_extract_front_matter()` (currently around line 240), add:
    ```python
    enricher = MarkdownEnricher()
    if is_text_type:
        heading_table = enricher.prepare(markdown)
    else:
        heading_table = []
    ```
    `enricher` is instantiated unconditionally (it is a cheap operation). This must run **after** `_extract_front_matter()` and **before** `self._chunker.chunk()`, so that `markdown` is already front-matter-stripped when the heading scanner sees it. The `else` branch sets an empty table for binary formats (PDF, DOCX); `enrich_chunk` handles empty tables gracefully by returning empty strings.
  - After `self._chunker.chunk()` returns `records`, and after the existing `if not records: return ...` guard, add the enrichment loop **before** the chunk-ID assignment loop:
    ```python
    for record in records:
        enrichment = enricher.enrich_chunk(record, heading_table)
        record.metadata.update(enrichment)
    ```
  - The enrichment loop is unconditional — `enrich_chunk` with an empty `heading_table` returns `{"_heading": "", "_section_path": ""}` without error. The `enricher` variable is local to `ingest_file()`; `MarkdownEnricher` instances are cheap to construct.
  - No changes to any other method in `pipeline.py`.
  - C3b extension point: C3b's `MarkdownEnricher.preprocess(text)` (which strips page markers and returns cleaned text) must be called AFTER `_extract_front_matter()` and BEFORE `prepare()`. The current wiring leaves room for this: C3b will call `preprocess()` on `markdown` before the `enricher.prepare(markdown)` call, passing the transformed text to both `prepare()` and `chunk()`.
- **Releasable**: after this task, all text-format ingest paths produce enriched chunks.
- **Tests (TDD)** — `tests/test_pipeline_metadata.py` (extend existing file):
  - Unit: `test_ingest_file_heading_metadata_populated` — ingest a fixture `.md` file with known headings; assert returned chunks have non-empty `_heading` and `_section_path` in `metadata`
  - Unit: `test_ingest_file_no_heading_empty_strings` — ingest a `.md` file with no headings; assert `_heading == ""` and `_section_path == ""`
  - Unit: `test_ingest_file_binary_no_enrichment` — ingest a non-front-matter file (e.g., a `.py` source file or a temporary `.json` file — any extension NOT in `_FRONT_MATTER_EXTENSIONS`); assert `_heading == ""` and `_section_path == ""` in `metadata`. This uses no docling and exercises the `heading_table = []` → empty `enrich_chunk` path.
  - Integration: `test_enrichment_survives_lancedb_roundtrip` — ingest a markdown file with headings into a temporary `SearchStore`, call `hybrid_search()`, assert `_heading` and `_section_path` are present in `result.metadata` on matching chunks and match expected values; explicitly verify `json.dumps`/`parse_metadata` round-trip does not strip or mutate these keys
  - Checkpoint: `uv run pytest tests/test_pipeline_metadata.py -x`

---

### Phase 4 — Verification & Documentation

#### Task 4.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (Architecture docs, ADRs, API docs, user guides, CLAUDE.md, BREAKING.md) and update every file whose content is affected by the changes delivered in this plan. Files to check: `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md`, `Documentation/Architecture/130_data_architecture_and_persistence.md`, `Documentation/Architecture/100_system_architecture_overview.md`, `Documentation/roadmap.md`. The agent must not update docs that are unrelated.
  - Run the full default test suite (`uv run pytest`) and confirm it passes.
  - Run `uv run pytest tests/test_enricher.py tests/test_chunker.py tests/test_pipeline_metadata.py -v` and confirm all pass.
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - `uv run pytest` passes with coverage ≥ 85%; no warnings
  - Every `ChunkRecord` returned by `DocumentChunker.chunk()` has `start_offset >= 0` and `end_offset > start_offset`; `text[start_offset:end_offset] == record.text` for all non-empty chunks
  - `"start_offset"` and `"end_offset"` do not appear in `SearchStore._schema()` column names
  - Ingesting a markdown file with `# H1` / `## H2` / `### H3` headings produces chunks where `result.metadata["_heading"]` and `result.metadata["_section_path"]` contain the expected heading text and path string
  - Chunks that precede all headings in the document have `_heading == ""` and `_section_path == ""`
  - `_heading` and `_section_path` survive the `json.dumps` → LanceDB → `parse_metadata` round-trip without mutation
  - Ingesting a non-text-format file produces no error, no `KeyError` on `metadata["_heading"]`, and `metadata["_heading"] == ""`
  - `uv run pytest tests/test_no_fstring_sql.py` passes (no new f-string SQL regressions introduced)
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
