# Feature Brief: C3b — Page Number Extraction

## Problem
PDF and image chunks carry no page provenance. A user searching for "the diagram on page 12" has no way to scope retrieval to that page, and search results give no indication of where in a long document a chunk came from. This matters most for operators ingesting technical manuals, contracts, or reports where page references are part of the user's vocabulary.

## Goal
Every chunk produced from a PDF or image file carries a `_page_start` metadata field (1-indexed) indicating the source page. Chunks that span a page boundary also carry `_page_end`. No second PDF parser is introduced — page boundaries are surfaced via docling's native `page_break_placeholder` export-formatting kwarg.

## Users & Context
Operators and end-users working with paginated documents (PDFs, scanned images). They want search results to surface page numbers alongside content, and may want to filter to a specific page range in future.

## Terminology
Two distinct uses of the word "strip" appear in this brief; they are kept separate throughout:
- **`result.strip()`** — Python string method that trims surrounding whitespace; called in `parser.py:_parse_with_docling`. Always referred to by its explicit `result.strip()` form.
- **Marker removal** (a.k.a. **marker excision**) — the enricher's step of deleting the namespaced page-break marker substring from the markdown produced by docling. Always referred to as "marker removal" or "marker excision", never as "stripping".

## Depends on
- **C3a** — the `MarkdownEnricher` infrastructure and underscore-prefixed metadata key convention established in C3a are reused here.
- **docling minimum version**: `page_break_placeholder` parameter is supported by `docling-core` shipped with `docling>=2.80.0` (verified in `.venv/.../docling_core/types/doc/document.py` — `export_to_markdown(..., page_break_placeholder: Optional[str] = None, ...)`). `docling>=2.80.0` is the existing project pin (`pyproject.toml:17`). No bump required.

### Prerequisite (blocking)
C3b cannot start until C3a ships character offsets on `ChunkRecord`. `ChunkRecord` today stores only `text`; the chunker discards Chonkie's per-chunk offsets (verified in `archon_search/_types.py` and `archon_search/chunker.py`). Both C3a (heading offset → chunk lookup) and C3b (page offset → chunk lookup) require these offsets. Chonkie's `Chunk` dataclass exposes `chunk.start_index` and `chunk.end_index` (verified via `uv run python`); the chunker must populate `ChunkRecord.start_offset` / `ChunkRecord.end_offset` from those attributes. C3a's implementation MUST include this; C3b is blocked on it.

### Coordinated C3a scope additions
The following items are coordinated amendments to C3a's agreed scope, owned jointly with C3b. Listed here as the single canonical inventory — do not duplicate elsewhere in this brief:
1. `ChunkRecord.start_offset: int` — populated by the chunker from Chonkie's `chunk.start_index`.
2. `ChunkRecord.end_offset: int` — populated by the chunker from Chonkie's `chunk.end_index`.
3. `_source_subtype` mapping extension — `.pdf → "pdf"` and image extensions (`.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.bmp`, `.webp`) → `"image"`.
4. Acknowledgement in C3a that `MarkdownEnricher` is also invoked on docling-produced markdown for PDF/image inputs (the heading pass is best-effort there; the page-break pass is C3b's addition).

## Core Flow
The order is significant because chunk offsets and the page-offset table must live in the same coordinate space. Marker text exists only transiently between parse and chunk.

1. **Parse with marker.** `parser.py`'s `_parse_with_docling()` passes `page_break_placeholder="<!-- archon-search:pagebreak:v1 -->"` to `export_to_markdown()`. Returns marker-bearing markdown. `result.strip()` (parser.py:105) only trims surrounding whitespace and is safe; see Edge Cases for the leading-marker case.
2. **Build pre-removal page table.** The enricher scans the marker-bearing markdown and records a sorted list of `(pre_removal_offset, page_number)` entries — one entry per marker occurrence in pre-removal coordinates. Page 1 is implicit (offset 0, page 1).
3. **Transform to post-removal coordinates.** For each entry, subtract the cumulative length of preceding markers to convert `pre_removal_offset` into the equivalent offset in the about-to-be-cleaned text. Result: a sorted list of `(post_removal_offset, page_number)` entries.
4. **Remove markers from text.** The marker substring is excised from the markdown. This produces the text that will be passed to the chunker.
5. **Chunk on cleaned text.** The chunker runs on cleaned text and produces `ChunkRecord`s whose `start_offset`/`end_offset` live in post-removal coordinates (matching step 3's table).
6. **Resolve page metadata.** For each chunk, the enricher `bisect`s the post-removal table by `start_offset` to derive `_page_start`, and by `end_offset` to derive `_page_end`. `_page_end` is written only if it differs from `_page_start`.
7. **Write records.** `ChunkRecord.text` contains no marker substrings (excised in step 4); metadata dict carries `_page_start` and optionally `_page_end`.
8. **Non-PDF/image files.** No page fields are set (the field is omitted entirely, not set to `""`).

### Pipeline integration
The enricher exposes a two-pass contract, invoked by `pipeline.py` around the chunker call:

- **`MarkdownEnricher.preprocess(text) -> (cleaned_text, page_table)`** — called by `pipeline.py` once after the parser returns docling markdown, before the chunker runs. Performs steps 2–4 above. Returns the marker-free text that the chunker consumes plus the post-removal `(offset, page)` table.
- **`MarkdownEnricher.enrich_chunk(chunk, page_table) -> dict[str, str]`** — called per chunk after the chunker emits each `ChunkRecord`. Performs step 6. Returns the metadata fragment (`_page_start`, optionally `_page_end`) to merge into the chunk's metadata dict.

The `page_table` is held as state on the enricher instance for the duration of a single ingest (one document = one preprocess + N enrich_chunk calls). The pipeline call graph becomes parser → `enricher.preprocess` → chunker → (loop) `enricher.enrich_chunk`. Heading extraction (C3a) reuses the same two-pass shape; the page-break pass is gated to docling-parsed sources via the `_source_subtype` discriminator.

## In Scope
- Fields: `_page_start` (integer as string, 1-indexed), `_page_end` (integer as string, only if chunk spans pages).
- Formats: PDF (`.pdf`) and image files (`.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.bmp`, `.webp`) — i.e., all files processed via `_parse_with_docling()`.
- Marker: `<!-- archon-search:pagebreak:v1 -->` (namespaced + versioned to avoid collision with user content).
- Enricher composition: `MarkdownEnricher` (C3a-scoped to text formats) is extended to also run on docling-produced markdown for PDF/image inputs. Heading extraction is invoked as today; the page-break pass is an independent, optional method that the pipeline only enables for docling-parsed sources. See Key Decisions.
- Eval gate: one new page-specific query added to `tests/eval/queries.jsonl` plus matching label in `tests/eval/labels.jsonl` (e.g., a query that targets content known to live on a specific page of a fixture PDF). Maintenance procedure per `tests/eval/README.md`.
- No new runtime dependency. (Test-only dev dependency on `reportlab` — see Verification.)

## Out of Scope
- Page-based filtering in `SearchFilters` — `_page_start` lives in the metadata dict; dedicated typed filter is a future item (see Future Iterations).
- Office documents (DOCX, PPTX) processed via markitdown — markitdown does not emit page breaks.
- Total page count metadata — useful but adds no retrieval value without filter support; deferred.

## Key Decisions
- **`page_break_placeholder` over a second PDF parser**: docling's `page_break_placeholder` is a native export-formatting kwarg, not an alternative to parsing. The real trade-off is "use docling's built-in marker output" vs "introduce a second PDF parser (PyMuPDF/pdfplumber) to detect page boundaries alongside docling." We pick the former: zero new runtime dependency, single source of truth for document structure, no double I/O.
- **Marker string vs structured side-channel**: An alternative is for `_parse_with_docling()` to return `(markdown, page_offsets)` directly, or to walk docling's `result.document` object (e.g., per-item page numbers) and build the offset table without round-tripping through a marker substring. **We chose the marker approach** because (a) it keeps `_parse_with_docling()`'s return type a plain `str`, matching all other parser methods, (b) marker positions naturally align with markdown offsets without a separate mapping step, and (c) the structured walk would require reverse-mapping docling's document tree to character offsets in the exported markdown, which is fragile across docling versions. The marker string is namespaced + versioned (see below) to mitigate the collision risk.
- **Unique namespaced marker (`<!-- archon-search:pagebreak:v1 -->`)**: HTML-comment form for compatibility with downstream markdown consumers, prefixed with `archon-search:` to namespace ownership, and `:v1` to make any future marker change a search-and-replace rather than ambiguous.
- **Composition: extend `MarkdownEnricher` rather than introduce `PageEnricher`**: C3a's `MarkdownEnricher` is currently scoped to text formats (`.md/.txt/.rst/.html`). We extend its scope to also be invoked on docling-produced markdown, but the page-break extraction is a separate method (`_extract_page_breaks`) gated by the pipeline (only called for docling-parsed sources). Heading extraction may or may not yield useful output on docling markdown — that is orthogonal and treated as a follow-up question, not a blocker. The alternative — a new `PageEnricher` class — would duplicate the offset/bisect plumbing without earning its keep.
- **String-typed page numbers — accepted debt**: Metadata values are `dict[str, str]` (consistent with the A1 schema). Storing `_page_start = "3"` forecloses cheap LanceDB numeric range predicates. The future `SearchFilters.page_range` filter (see Future Iterations) will therefore need either (a) a dedicated typed column on `ChunkRecord` introduced at that time, or (b) row-level Python filtering after retrieval. We accept the string-typed value for v1 and explicitly document this debt rather than introducing a typed column ahead of demand.
- **Underscore-prefixed keys**: Reuses C3a's convention (`_page_start`, `_page_end`); signals system-generated fields and avoids collision with user YAML.
- **Bisect over per-chunk scan**: Page offset table is built once per document; per-chunk lookup is O(log n). Matches C3a's heading-resolution approach.

## Edge Cases & Constraints
- **Chunk spans a page break**: `_page_start` and `_page_end` are both written; the chunk text is continuous (marker excised).
- **First-page chunks (no preceding marker)**: Chunks whose `start_offset` precedes the first page marker are assigned `_page_start = "1"`. The page table seeds with `(0, 1)` to make this explicit.
- **Document begins with a marker (blank first page)**: `result.strip()` in `parser.py:_parse_with_docling` only trims leading/trailing whitespace; an HTML-comment marker at offset 0 is not whitespace and is preserved. The offset-zero scan must handle a marker found at offset 0 correctly (page table starts with `(0, 2)` in this case rather than `(0, 1)`).
- **Marker appears in user PDF content**: Highly improbable (`<!-- archon-search:pagebreak:v1 -->` is not natural prose, and the `archon-search:` namespace is unique to this project), and docling controls the exported markdown. If a user's PDF body somehow contains the exact comment string, it would be misinterpreted as a page break. Accepted risk; documented under "known limitations" in `Documentation/Architecture/150_security_and_privacy_architecture.md`, and any change to the marker string is a breaking change recorded in `BREAKING.md`.
- **docling fails to detect page breaks**: Some PDFs (scanned flat images, certain malformed PDFs) have no structural page information. docling emits no markers; all chunks get `_page_start = "1"`. Correct behaviour, not an error.
- **Mixed-state collections (backward compatibility)**: Chunks ingested before C3b lack `_page_start`. Pre-existing chunks remain without the field; re-ingest (or `reindex`) retroactively populates page metadata for PDF/image documents. Future filters that reference `_page_start` must handle absence gracefully (treat as no-match, not as error).
- **50-field metadata limit**: C3a adds 3 fields; C3b adds at most 2 more. Total ≤ 5 system fields, well within limits.
- **Marker removal and FTS index**: Markers are excised in step 4 before `ChunkRecord.text` is written. The FTS index is built from `ChunkRecord.text` and therefore never sees the marker.
- **Multi-page TIFF**: Expected to work via the same mechanism (docling treats each image page as a logical page), but **not part of the explicit verification matrix** — tracked as a follow-up to confirm.

## Verification
Concrete test cases (each backed by either a deterministic minimal multi-page PDF fixture or a mock of `_parse_with_docling()`):

- **Fixture strategy**: a `conftest.py` fixture generates `tests/fixtures/pdfs/three_page.pdf` at test-collection time using `reportlab`. Reportlab produces deterministic byte-identical PDFs given identical inputs (fixed timestamp, no compression randomness when called with the same arguments), so the fixture does not need to be committed. **`reportlab` is not currently installed** (verified via `uv run python -c "import reportlab"` → `ModuleNotFoundError`); C3b must add it to `[dependency-groups].dev` in `pyproject.toml`. Page contents are fixed as: page 1 = `"alpha content"`, page 2 = `"beta content"`, page 3 = `"gamma content"` — verification cases below reference these strings directly.
  - *Alternative considered and rejected*: commit a binary PDF under `tests/fixtures/pdfs/` and pin the docling minor version explicitly. Rejected because a binary in-repo fixture obscures provenance and the docling minor pin is a heavier lock than a test-only `reportlab` dep.
- **Coordinate-transform unit test (pure function, no docling)**: the marker literal `<!-- archon-search:pagebreak:v1 -->` is 35 characters — tests must use `len(MARKER)` rather than a hard-coded integer. The transform rule is: for each entry in the pre-removal page table, subtract `n * len(MARKER)` from its offset, where `n` is the number of markers that occur **strictly before** that entry's pre-removal offset. The synthetic `(0, 1)` seed (used when there is no leading marker) has 0 markers before it and is unchanged. Example A (markers between content; page 1 starts implicitly at offset 0): pre-removal marker offsets `[150, 400, 600]` → pre-table `[(0, 1), (150, 2), (400, 3), (600, 4)]`. Markers-strictly-before count per entry: `[0, 0, 1, 2]`. Post-removal page table: `[(0, 1), (150 - 0*35, 2), (400 - 1*35, 3), (600 - 2*35, 4)]` = `[(0, 1), (150, 2), (365, 3), (530, 4)]`. Geometric sanity check: page 1 occupies pre-offsets 0-149 (also cleaned offsets 0-149); the 35-char marker at pre-offset 150 is removed, so page 2 content (pre-offsets 185-399) maps to cleaned offsets 150-364; page 3 (pre-offsets 435-599) maps to cleaned offsets 365-529; page 4 starts at cleaned offset 530. Example B (leading marker at offset 0, i.e. page 1 is empty and content begins on page 2): pre-removal marker offsets `[0, 150, 400]` → pre-table `[(0, 2), (150, 3), (400, 4)]` (no synthetic seed). Markers-strictly-before count per entry: `[0, 1, 2]`. Post-removal page table: `[(0 - 0*35, 2), (150 - 1*35, 3), (400 - 2*35, 4)]` = `[(0, 2), (115, 3), (330, 4)]`. This test exercises the offset-transformation math in isolation, before any docling integration, and is the recommended TDD entry point. The leading-marker variant must reconcile with the edge case described in "Edge Cases & Constraints" above.
- **Single-page chunk**: chunk fully within one page → assert `_page_start == "N"` and **`_page_end` key is absent from the metadata dict** (not present as empty string).
- **Cross-page chunk**: chunk that spans a marker → assert both `_page_start` and `_page_end` are present and differ.
- **First-page chunk**: chunk before any marker → assert `_page_start == "1"`.
- **No-markers PDF** (docling emits no marker): assert every chunk gets `_page_start == "1"` and no `_page_end`.
- **Marker in user content**: feed canned markdown where the marker appears inside a normal paragraph; assert no exception is raised and the (false) page break is honoured (this is the accepted-risk path — the test pins current behaviour, not a fix).
- **Marker excised from text**: assert `ChunkRecord.text` contains no occurrence of the marker substring, and that the FTS index built from those records returns zero hits when searching for any part of the marker namespace.
- **Leading marker (blank first page)**: canned markdown beginning with the marker at offset 0; assert chunks at the start are assigned `_page_start == "2"`.
- **Mixed-state**: Setup — insert two `ChunkRecord` rows directly via `Store.add_chunks()` with metadata dicts that omit `_page_start` (simulating pre-C3b ingest). Then re-ingest the same PDF via the pipeline. Assert: pre-existing rows are deleted-and-reinserted with `_page_start` populated; chunks from unrelated formats (e.g., a `.md` file in the same collection) remain unmodified.
- **`_source_subtype` extension**: `.pdf` input produces `_source_subtype = "pdf"`; image input produces `"image"`.
- **Latency**: page-break scanning is O(n) over markdown text per document at ingest time. No measurable impact on search-path latency; the `[search_filtered]` p95 threshold in `tests/eval/thresholds.toml` remains the gate. Ingest latency is not currently threshold-gated; no new gate added here.

## Open Questions
- None — all decisions resolved in refinement.

## Future Iterations
- `_page_total` field (total pages in document) — useful for UI display ("page 3 of 47").
- `SearchFilters.page_range` — typed filter for `_page_start` within a numeric range. Will require either a dedicated typed `ChunkRecord` column or row-level Python filtering (see Key Decisions / string-typed debt).
- Office document page extraction — requires markitdown to emit page markers or an alternative parser.
- Confirm multi-page TIFF behaviour and promote it from "expected" to a verified case.

## Recommendation
C3b is a clean, low-risk feature once the blocking C3a prerequisite (chunk offsets on `ChunkRecord`) ships. The mechanism rests on a one-line change to the `_parse_with_docling()` call, an enricher pass that mirrors C3a's offset/bisect pattern, and a careful coordinate-space transformation between pre-removal and post-removal text. Build C3a first (including the offset fields and `_source_subtype` mapping listed under "Coordinated C3a scope additions"); C3b lands as a small follow-on with minimal new surface area.
