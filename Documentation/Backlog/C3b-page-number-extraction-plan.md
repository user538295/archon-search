# C3b — Page Number Extraction
**Purpose**: Surface PDF and image page provenance on every chunk so operators can map search results back to the source page, and lay the groundwork for a future typed page-range filter.
**Audience**: archon-search contributors implementing C3b; reviewers of the resulting PRs.
**Status**: To Do

---

## Background

PDF and image chunks carry no page provenance. A user searching for "the diagram on page 12" has no way to scope retrieval to that page, and search results give no indication of where in a long document a chunk came from. This matters most for operators ingesting technical manuals, contracts, and reports where page references are part of the user's vocabulary.

The full design and all key decisions are in `Documentation/Backlog/C3b-page-number-extraction-brief.md`.

C3b is **blocked on C3a** for two reasons:
1. `ChunkRecord` must expose `start_offset: int` / `end_offset: int` (populated from Chonkie's `Chunk.start_index` / `Chunk.end_index`). The chunker currently discards these. C3a's plan owns introducing the fields.
2. `MarkdownEnricher` is introduced in C3a with `prepare(text) -> HeadingTable` as its pre-chunking method. C3b adds a sibling method `preprocess(text) -> (cleaned_text, page_table)` on the same class. These are **two distinct methods**, not a rename — `prepare` is called for text-format sources, `preprocess` is called for docling-parsed sources.

C3b also coordinates an addition to C3a: the `_source_subtype` discriminator gains `"pdf"` and `"image"` values so the pipeline can gate the page-break pass on docling-parsed sources without sniffing file extensions inside the enricher.

> **Brief and plan agree on the transform**: the parent brief's `Verification` section (Coordinate-transform unit test) and Task 2.2 below derive the post-removal page table using the "markers strictly before this entry" rule. Example A: `[(0, 1), (150, 2), (365, 3), (530, 4)]`. Example B (leading marker): `[(0, 2), (115, 3), (330, 4)]`. An earlier revision of the brief had an off-by-one error in Example A; that has been corrected in the brief.

---

## Goal

After C3b ships: every chunk produced from a `.pdf`, `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.bmp`, or `.webp` file carries a `_page_start` metadata field (1-indexed). Chunks that span a page boundary additionally carry `_page_end`. Page boundaries are surfaced via docling's native `page_break_placeholder` export kwarg — no second PDF parser, no new runtime dependency. Marker substrings never appear in `ChunkRecord.text` and never reach the FTS index. The eval harness includes one new page-targeted query with a matching label.

---

## Scope

### In Scope
- Page-break marker constant `<!-- archon-search:pagebreak:v1 -->` (namespaced + versioned).
- `parser.py:_parse_with_docling` passes `page_break_placeholder=<marker>` to `export_to_markdown()`.
- `MarkdownEnricher._extract_page_breaks(text) -> list[tuple[int, int]]` — pre-removal `(offset, page)` table builder.
- `MarkdownEnricher._transform_page_table(pre_table, marker_len) -> list[tuple[int, int]]` — pure coordinate-transform function.
- `MarkdownEnricher._excise_markers(text) -> str` — marker removal.
- `MarkdownEnricher.preprocess(text) -> (cleaned_text, page_table)` — pre-chunking entry point for docling-parsed sources (sibling to C3a's `prepare(text) -> HeadingTable`; both methods coexist on the class).
- `MarkdownEnricher.enrich_chunk(chunk, *, heading_table=None, page_table=None) -> dict[str, str]` — per-chunk bisect-based resolution of `_page_start` and (conditionally) `_page_end`. Already defined in C3a for heading enrichment; this task adds the page-break branch and changes the signature to explicit keyword arguments.
- `_source_subtype` mapping extension: `.pdf → "pdf"` and image extensions → `"image"`. (Coordinated C3a addition; owned by C3b.)
- `is_docling_source(subtype) -> bool` helper in `enricher.py` encapsulating the `{"pdf", "image"}` set.
- Pipeline integration: `pipeline.py:ingest_file` constructs a per-call `MarkdownEnricher`, calls `enricher.preprocess(markdown)` between parse and chunk for sources where `is_docling_source(subtype)`, then merges per-chunk metadata via `enricher.enrich_chunk`.
- Eval gate: one new page-targeted query in `tests/eval/queries.jsonl` + matching label in `tests/eval/labels.jsonl`.
- Test fixture: `tests/fixtures/pdfs/three_page.pdf` generated at test-collection time via a `conftest.py` fixture using `reportlab`; page contents pinned to `"alpha content"`, `"beta content"`, `"gamma content"`.
- `reportlab` added as a `[dependency-groups].dev` test-only dependency in `pyproject.toml`.
- Documentation updates: `Documentation/Architecture/130_data_architecture_and_persistence.md` documents the marker as an internal implementation detail; `Documentation/Architecture/150_security_and_privacy_architecture.md` documents the marker-collision accepted risk. The marker is internal — it never reaches `ChunkRecord.text`, API responses, or schemas — so it is NOT a `BREAKING.md` entry.

### Out of Scope
- Page-based filtering in `SearchFilters` — `_page_start` is stored in the metadata dict only; a typed filter is deferred.
- Office documents (DOCX, PPTX) via markitdown — markitdown does not emit page breaks.
- `_page_total` metadata — deferred until there is a UI consumer.
- Multi-page TIFF verification — expected to work via the same mechanism; tracked as a follow-up.
- Backfill tooling — pre-C3b chunks remain without page metadata until re-ingest / `reindex`.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task F.1 — Final verification & documentation update].

---

## What does NOT change
- `_parse_with_docling()`'s return type — still `str` (the marker travels inside the string).
- `parser.py:105`'s `result.strip()` — preserved as-is; an HTML-comment marker at offset 0 is not whitespace and survives the trim.
- `ChunkRecord.text` semantics — markers are excised before any `ChunkRecord` is constructed; downstream consumers never see them.
- LanceDB schema — `_page_start` and `_page_end` live inside the existing `metadata: dict[str, str]` column. No `_schema()` change, no `_do_ingest()` change.
- `SearchResult` shape — page fields surface through the existing `metadata` dict.
- C3a heading enrichment path — the page-break branch is independent and only runs for docling-parsed sources; text-format inputs (`.md`, `.txt`, `.rst`, `.html`) hit the C3a heading path unchanged.
- `store.reindex_metadata()` — never reads or writes the `metadata` dict column; surviving enrichment fields is the existing contract.
- Eval thresholds — no threshold changes; one new query/label entry only.

---

## Known limitations / accepted trade-offs
- **String-typed page numbers**: `_page_start = "3"` (not `3`) — consistent with the `dict[str, str]` metadata schema; forecloses cheap LanceDB numeric range predicates. The future `SearchFilters.page_range` filter will need a typed column or row-level Python filtering.
- **Marker-in-user-content risk**: if a user's PDF body contains the literal string `<!-- archon-search:pagebreak:v1 -->`, it would be misinterpreted as a page break. Highly improbable given the namespace; accepted and documented.
- **Mixed-state collections**: chunks ingested before C3b lack `_page_start`. Re-ingest backfills; absence is treated as no-match by future filters, not as an error.
- **PDFs without structural page info**: docling emits no markers for scanned flat images or malformed PDFs; all chunks get `_page_start = "1"`. Correct behaviour, not an error.
- **Multi-page TIFF**: expected to work via the same mechanism but not in the explicit verification matrix.
- **Office documents**: out of scope; markitdown does not surface page breaks.

---

## Architecture

### Marker constant
`MARKER = "<!-- archon-search:pagebreak:v1 -->"` (35 characters). Lives in `archon_search/enricher.py` as a module-level constant and is imported by both the enricher and the parser.

### Parser change
`archon_search/parser.py:_parse_with_docling` passes `page_break_placeholder=MARKER` to `result.document.export_to_markdown(...)`. No other parser change. `result.strip()` (parser.py:105) is preserved.

### Enricher composition contract
C3a's `prepare(text) -> HeadingTable` and C3b's `preprocess(text) -> (cleaned_text, page_table)` are **two distinct methods** on the same class, called by the pipeline for different source types:
- **Text-format sources** (`.md`, `.txt`, `.rst`, `.html`): the pipeline calls `enricher.prepare(text)` and binds the returned heading table for the chunk loop.
- **Docling-parsed sources** (`.pdf`, image extensions): the pipeline calls `enricher.preprocess(text)` and binds the cleaned text and the page table. **Heading extraction is NOT run on docling output in v1** — the brief frames it as incidental and best-effort; we explicitly skip it to keep the contract simple. The pipeline passes `heading_table=None` to `enrich_chunk` for docling sources. This is documented as a known limitation and is the cleanest resolution of the "where does the heading table for docling sources live" question — no instance state, no double pre-chunking pass, no implicit `self` reads.

`enrich_chunk(chunk, *, heading_table=None, page_table=None) -> dict[str, str]` takes the two tables as explicit keyword arguments. The C3a heading branch is gated on `heading_table is not None`; the C3b page branch is gated on `page_table is not None`. Both can be non-None for docling sources if the heading pass also ran.

### Enricher instance lifecycle
`MarkdownEnricher` is instantiated **once per `ingest_file` call** (constructed inside `ingest_file`, **not** on `SearchPipeline.__init__`). This guarantees there is no cross-document state leak even under concurrent ingests. The instance is short-lived and not reused. Any transient state on `self` (e.g., a stashed heading table from C3a) is therefore fresh per document; no explicit `reset()` method is needed.

Because the enricher is per-ingest, the pipeline carries `page_table` as a local variable through the chunk loop rather than reading it off `self._page_table`. C3b does not introduce a `self._page_table` attribute.

### Enricher extensions (additive to C3a's `MarkdownEnricher`)
```python
# archon_search/enricher.py (extends C3a's class)
class MarkdownEnricher:
    # ... C3a heading members (including prepare(text) -> HeadingTable) ...

    def _extract_page_breaks(self, text: str) -> list[tuple[int, int]]:
        """Build pre-removal (offset, page) table from marker positions.

        Returns a sorted list seeded with (0, 1) for first-page provenance.
        If text begins with the marker at offset 0, the seed is omitted and
        the first emitted entry is (0, 2).
        """

    @staticmethod
    def _transform_page_table(
        pre_table: list[tuple[int, int]], marker_len: int
    ) -> list[tuple[int, int]]:
        """Convert pre-removal offsets into post-removal coordinates.

        Pure function. For each marker entry, subtract
        (markers_strictly_before_this_entry) * marker_len from the
        pre-removal offset. The synthetic (0, 1) seed (when present) is
        unchanged. Equivalently: number the marker entries 0, 1, 2, ...
        starting AFTER the seed (no-leading-marker case) or starting AT
        index 0 (leading-marker case); subtract marker_index * marker_len.
        Returns a new sorted list; does not mutate the input.
        """

    def _excise_markers(self, text: str) -> str:
        """Remove every occurrence of MARKER from text."""

    def preprocess(self, text: str) -> tuple[str, list[tuple[int, int]]]:
        """Pre-chunking entry point for docling-parsed sources.

        Builds the pre-removal page table, transforms it into
        post-removal coordinates, excises markers, and returns
        (cleaned_text, post_removal_table). Distinct from C3a's
        prepare() — both methods can coexist on the same class.
        """

    def enrich_chunk(
        self,
        chunk,
        *,
        heading_table=None,
        page_table: list[tuple[int, int]] | None = None,
    ) -> dict[str, str]:
        """Resolve enrichment metadata for a single chunk.

        Uses bisect on chunk.start_offset to derive _page_start, and on
        chunk.end_offset to derive _page_end. Writes _page_end only if
        it differs from _page_start. C3a's heading branch runs when
        heading_table is non-None; the page branch runs when page_table
        is non-None. Both branches can fire together for docling sources.
        """
```

`is_docling_source(subtype: str) -> bool` is exposed alongside `_SOURCE_SUBTYPE_MAP` and `source_subtype_for()`; it returns `subtype in {"pdf", "image"}`. The pipeline calls the helper rather than inlining the set.

### Pipeline integration
`archon_search/pipeline.py:ingest_file` between current line ~240 (`_extract_front_matter`) and the existing `self._chunker.chunk(...)` call:
1. Construct a fresh `enricher = MarkdownEnricher()` at the top of `ingest_file` (per-ingest lifecycle; see "Enricher instance lifecycle").
2. Compute `subtype = source_subtype_for(path.suffix)`.
3. If `is_docling_source(subtype)`, call `cleaned_text, page_table = enricher.preprocess(markdown)` and rebind `markdown = cleaned_text`. Otherwise call `heading_table = enricher.prepare(markdown)` (C3a's path) and set `page_table = None`.
4. After `records = self._chunker.chunk(...)`, for each `record` call `record.metadata.update(enricher.enrich_chunk(record, heading_table=heading_table, page_table=page_table))`. (`heading_table` and `page_table` are bound in the appropriate branches above; the unused one is `None`.)
5. Text-format inputs continue to hit the C3a heading branch unchanged.

### `_source_subtype` map
`archon_search/enricher.py` (or a small module constant) defines:
```python
_SOURCE_SUBTYPE_MAP: dict[str, str] = {
    ".pdf": "pdf",
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".tiff": "image", ".tif": "image",
    ".bmp": "image", ".webp": "image",
}
```
C3a is responsible for whatever text-format values it defines (e.g., `".md" → "markdown"`); C3b only adds the docling-source entries.

### Test fixture
`tests/conftest.py` exposes a session-scoped `three_page_pdf` fixture that generates `tests/fixtures/pdfs/three_page.pdf` on first use via `reportlab.pdfgen.canvas.Canvas`. Generation logic lives in `tests/_pdf_fixture.py` so the eval-corpus fixture (Task 5.2) can reuse it from a separate `tests/eval/conftest.py`. Page contents are pinned to `"alpha content"`, `"beta content"`, `"gamma content"`. The output is **not** byte-deterministic across sessions (reportlab embeds `CreationDate` / `ModDate` timestamps); only the textual content is stable. Tests rely on textual assertions, not byte-hash assertions.

### Config keys, env vars
None.

---

## Task breakdown

> **Atomic-landing constraint**: Phases 1–4 must land in a single atomic PR. Equivalently, Task 1.2's parser kwarg change must land in the same commit (or merge group) as Phase 4's pipeline wiring. **Landing Phase 1 alone is not acceptable** — it would introduce marker leakage into `ChunkRecord.text` and the FTS index on main until Phase 4 ships. Phase 5 (fixture + eval) and Phase 6 (docs) may land in trailing commits within the same PR, but Task 5.1 (fixture) must be ordered before Task 1.2 to avoid an `xfail` workaround.

### Phase 1 — Parser-level marker emission
> **Releasable**: when Task 1.2 is complete AND Phase 4 has shipped in the same PR (see Atomic-landing constraint above). On its own, Task 1.2 leaks markers into chunk text.

#### Task 1.1 — Define the page-break marker constant
- [x] **File**: `archon_search/enricher.py`
- **Depends on**: nothing (C3a's `enricher.py` is assumed to exist; if not, this task creates it as a stub for C3a to extend).
- **Description**:
  - Add module-level constant `PAGE_BREAK_MARKER: str = "<!-- archon-search:pagebreak:v1 -->"`.
  - Add module-level constant `PAGE_BREAK_MARKER_LEN: int = len(PAGE_BREAK_MARKER)` — convenience for the coordinate-transform code.
  - Export both via `__all__`.
  - No other logic; this is a single source-of-truth task so the parser and enricher cannot drift.
- **Releasable**: after this task, the marker literal is importable from one place; no behavioural change.
- **Tests (TDD)** — `tests/test_enricher.py`:
  - Unit: `test_page_break_marker_literal` — asserts the exact string value and the 35-character length (catches accidental edits).
  - Unit: `test_page_break_marker_namespaced` — asserts the marker contains `"archon-search:"` and `":v1"` (collision-resistance invariants).
  - Checkpoint: `uv run pytest tests/test_enricher.py -k page_break_marker -v`

#### Task 1.2 — Pass `page_break_placeholder` to docling
- [x] **File**: `archon_search/parser.py`
- **Depends on**: Task 1.1, Task 5.1 (fixture must exist first — see Atomic-landing constraint)
- **Description**:
  - At `parser.py:104`, change the `export_to_markdown()` call to pass `page_break_placeholder=PAGE_BREAK_MARKER`. Import the constant from `archon_search.enricher`.
  - `result.strip()` on the following line is unchanged.
  - No other change to `_parse_with_docling`.
- **Releasable**: under the Atomic-landing constraint, this task is only safe to merge to main when Phase 4 (pipeline wiring) ships in the same PR. On its own it would leak markers into `ChunkRecord.text`.
- **Tests (TDD)** — `tests/test_parser.py`:
  - Integration: `test_parse_with_docling_emits_page_marker` — uses the `three_page_pdf` fixture from Task 5.1. Because Task 5.1 lands first per task ordering, no `xfail` workaround is needed. Asserts the parser output contains exactly two occurrences of `PAGE_BREAK_MARKER` (between three pages).
  - Unit: `test_parse_with_docling_kwarg_threaded` — uses `monkeypatch` to replace docling's `DocumentConverter` with a stub whose `.export_to_markdown(**kwargs)` records `kwargs`. Asserts `kwargs["page_break_placeholder"] == PAGE_BREAK_MARKER`. Independent of fixture availability.
  - Checkpoint: `uv run pytest tests/test_parser.py -k page_marker -v`

---

### Phase 2 — Enricher page-break extraction (pure logic)
> **Releasable**: after each task — every function is unit-testable in isolation against canned strings. Nothing is wired to the pipeline yet.

#### Task 2.1 — Pre-removal page table builder
- [x] **File**: `archon_search/enricher.py`
- **Depends on**: Task 1.1
- **Description**:
  - Add `MarkdownEnricher._extract_page_breaks(self, text: str) -> list[tuple[int, int]]`.
  - Behaviour: scan `text` with `text.find(PAGE_BREAK_MARKER, pos)` in a loop, recording each marker start offset. Build a sorted list of `(offset, page_number)`.
  - Seed rule: if `text` does not begin with the marker, prepend `(0, 1)`. If `text` begins with the marker at offset 0, omit the seed and assign the first emitted entry `(0, 2)`.
  - Page numbers increment by 1 per marker.
  - No state mutation on `self`; this is a pure helper used by `preprocess`.
- **Releasable**: after this task, `_extract_page_breaks` returns a correct pre-removal page table for any input string.
- **Tests (TDD)** — `tests/test_enricher.py`:
  - Unit: `test_extract_page_breaks_no_markers` — input with no marker → `[(0, 1)]`.
  - Unit: `test_extract_page_breaks_three_pages` — input `"A" + MARKER + "B" + MARKER + "C"` → `[(0, 1), (1, 2), (2 + len(MARKER), 3)]`.
  - Unit: `test_extract_page_breaks_leading_marker` — input `MARKER + "B"` → `[(0, 2)]` (no seed; first entry uses the leading-marker offset of 0).
  - Unit: `test_extract_page_breaks_consecutive_markers` — input `"A" + MARKER + MARKER + "C"` (two consecutive markers, i.e. an empty page) → `[(0, 1), (1, 2), (1 + len(MARKER), 3)]`.
  - Unit: `test_extract_page_breaks_empty_text` — empty string → `[(0, 1)]`.
  - Unit: `test_extract_page_breaks_trailing_marker` — input `"A" + MARKER` → `[(0, 1), (1, 2)]` (the trailing page is empty but the marker is still recorded).
  - Checkpoint: `uv run pytest tests/test_enricher.py -k extract_page_breaks -v`

#### Task 2.2 — Coordinate-transform pure function
- [x] **File**: `archon_search/enricher.py`
- **Depends on**: Task 2.1
- **Description**:
  - Add `MarkdownEnricher._transform_page_table(pre_table: list[tuple[int, int]], marker_len: int) -> list[tuple[int, int]]` as a `@staticmethod`.
  - Behaviour: for each marker entry in `pre_table` (i.e., entries OTHER than the synthetic `(0, 1)` seed in the no-leading-marker case), subtract `n * marker_len` from its pre-removal offset, where `n` is the count of markers strictly before this entry's pre-removal position. The seed entry (when present) is unchanged.
  - Equivalent procedural specification: number the marker entries `0, 1, 2, ...` — in the leading-marker case the first entry (offset 0) IS marker 0; in the no-leading-marker case the seed occupies index 0 and the first marker entry is marker 0 (its 0-based marker index, NOT its 0-based list index, drives the multiplier). Subtract `marker_index * marker_len` from each marker entry's pre offset.
  - Returns a new list; does not mutate the input.
  - **Worked derivation for Example A** (no leading marker, `marker_len=35`): pre-table `[(0, 1), (150, 2), (400, 3), (600, 4)]` where 150/400/600 are MARKER START offsets in pre-removal text. Marker entry `(150, 2)` is marker 0 (no markers before it) → `150 - 0*35 = 150`. Marker entry `(400, 3)` is marker 1 (one marker, 35 chars, removed before it) → `400 - 1*35 = 365`. Marker entry `(600, 4)` is marker 2 → `600 - 2*35 = 530`. Result: `[(0, 1), (150, 2), (365, 3), (530, 4)]`. Geometric cross-check: page 1 occupies post offsets 0–149 (150 chars). Marker 1 (pre 150–184, 35 chars) is removed; page 2 (pre 185–399 = 215 chars) lives at post 150–364. Marker 2 (pre 400–434) is removed; page 3 (pre 435–599 = 165 chars) lives at post 365–529. Page 4 starts at post 530. Confirmed.
  - **Worked derivation for Example B** (leading marker, `marker_len=35`): pre-table `[(0, 2), (150, 3), (400, 4)]`. Entry `(0, 2)` is marker 0 → `0 - 0*35 = 0`. Entry `(150, 3)` is marker 1 → `150 - 1*35 = 115`. Entry `(400, 4)` is marker 2 → `400 - 2*35 = 330`. Result: `[(0, 2), (115, 3), (330, 4)]`. (Note: Example B happens to satisfy both the correct formula and the naïve "i * marker_len by list index" formula because the seed is absent. Example A distinguishes them.)
  - This is the brief's "Coordinate-transform unit test" function — pure function, no docling, no I/O. **The parent brief shows incorrect expected values for Example A; the values above are the correct ones.**
- **Releasable**: after this task, the coordinate-transform math is callable in isolation and is the recommended TDD entry point per the brief.
- **Tests (TDD)** — `tests/test_enricher.py`:
  - Unit: `test_transform_page_table_no_leading_marker` — input `[(0, 1), (150, 2), (400, 3), (600, 4)]` with `marker_len=35` → `[(0, 1), (150, 2), (365, 3), (530, 4)]` (Example A; corrects the parent brief's miscalculation).
  - Unit: `test_transform_page_table_leading_marker` — input `[(0, 2), (150, 3), (400, 4)]` with `marker_len=35` → `[(0, 2), (115, 3), (330, 4)]` (Example B).
  - Unit: `test_transform_page_table_empty` — input `[]` → `[]`.
  - Unit: `test_transform_page_table_single_entry` — input `[(0, 1)]` → `[(0, 1)]` (seed-only case; no transform applied).
  - Unit: `test_transform_page_table_uses_len_marker` — re-runs Example A with `marker_len=PAGE_BREAK_MARKER_LEN` (confirms the production wiring uses `len(MARKER)`, not a hard-coded integer).
  - Unit: `test_transform_page_table_matches_cleaned_text_geometry` — construct a known pre-text (e.g., `"a"*150 + MARKER + "b"*215 + MARKER + "c"*165 + MARKER + "d"*70`), build the pre-table by hand, apply the transform, then independently compute the cleaned text via `str.replace(MARKER, "")` and assert each post-removal offset matches the actual position of the next page's content in the cleaned text (computed via `cleaned.index("b")`, `cleaned.index("c")`, `cleaned.index("d")` or analogous slice arithmetic). This anchors the math to ground truth and would have caught the brief's miscalculation.
  - Checkpoint: `uv run pytest tests/test_enricher.py -k transform_page_table -v`

#### Task 2.3 — Marker excision
- [x] **File**: `archon_search/enricher.py`
- **Depends on**: Task 1.1
- **Description**:
  - Add `MarkdownEnricher._excise_markers(self, text: str) -> str`.
  - Behaviour: `return text.replace(PAGE_BREAK_MARKER, "")`. One-line; explicit method exists so that the call site is greppable and the brief's terminology contract ("marker excision", not "stripping") is enforced in code.
- **Releasable**: after this task, marker excision is a single named function and the call site is traceable.
- **Tests (TDD)** — `tests/test_enricher.py`:
  - Unit: `test_excise_markers_removes_all` — input `"A" + MARKER + "B" + MARKER + "C"` → `"ABC"`.
  - Unit: `test_excise_markers_no_marker_unchanged` — input without the marker is returned byte-identical.
  - Unit: `test_excise_markers_leading_and_trailing` — input `MARKER + "B" + MARKER` → `"B"`.
  - Checkpoint: `uv run pytest tests/test_enricher.py -k excise_markers -v`

#### Task 2.4 — `preprocess` entry point for docling sources
- [x] **File**: `archon_search/enricher.py`
- **Depends on**: Task 2.1, Task 2.2, Task 2.3
- **Description**:
  - Add `MarkdownEnricher.preprocess(self, text: str) -> tuple[str, list[tuple[int, int]]]` as a NEW method (distinct from C3a's `prepare(text) -> HeadingTable`; both methods coexist on the class — see "Enricher composition contract").
  - Behaviour:
    1. `pre_table = self._extract_page_breaks(text)`.
    2. `post_table = self._transform_page_table(pre_table, PAGE_BREAK_MARKER_LEN)`.
    3. `cleaned = self._excise_markers(text)`.
    4. (Best-effort heading extraction on docling output may also run here per the "Enricher composition contract"; the heading table is stashed on `self` for C3a's `enrich_chunk` branch to consult. The page table is **not** stashed — it is returned and carried by the pipeline as a local.)
    5. Return `(cleaned, post_table)`.
  - No I/O; no logging.
- **Releasable**: after this task, the pipeline can call `preprocess` and receive marker-free text plus a usable page table. Per-chunk lookup is the next task.
- **Tests (TDD)** — `tests/test_enricher.py`:
  - Unit: `test_preprocess_returns_cleaned_text_and_table` — input `"alpha" + MARKER + "beta" + MARKER + "gamma"` → returns the tuple `("alphabetagamma", [(0, 1), (5, 2), (5 + len("beta"), 3)])` (table returned via the function result, NOT via instance state).
  - Unit: `test_preprocess_no_markers` — input without markers → `(text, [(0, 1)])`.
  - Unit: `test_preprocess_leading_marker` — input `MARKER + "beta"` → cleaned `"beta"` and page table `[(0, 2)]`.
  - Checkpoint: `uv run pytest tests/test_enricher.py -k preprocess -v`

#### Task 2.5 — `enrich_chunk` page resolution
- [x] **File**: `archon_search/enricher.py`
- **Depends on**: Task 2.4
- **Description**:
  - Extend `MarkdownEnricher.enrich_chunk(self, chunk, *, heading_table=None, page_table=None) -> dict[str, str]`. The signature uses **explicit keyword arguments** for the two tables (the C3a branch is gated on `heading_table is not None`; the C3b branch is gated on `page_table is not None`).
  - Behaviour when `page_table` is non-None:
    1. Resolve `_page_start` by `bisect.bisect_right(offsets, chunk.start_offset) - 1`, indexing into `page_table`.
    2. Resolve `_page_end` analogously using `chunk.end_offset`.
    3. Always write `_page_start = str(page_start_value)`.
    4. Write `_page_end = str(page_end_value)` **only if** `page_end_value != page_start_value`. Otherwise omit the key entirely (no empty string).
  - When `page_table` is None (text-format input), the page-break branch is skipped entirely and only the C3a heading fields are emitted.
  - Returns a `dict[str, str]` fragment for the caller to merge into `chunk.metadata`. Never mutates `chunk` directly.
  - **End-offset convention**: taken from Chonkie's `Chunk.end_index` semantics (exclusive vs inclusive). Verify at implementation time with `uv run python -c "from chonkie import RecursiveChunker; ..."` and document the chosen interpretation in a one-line code comment in `enricher.py`. The boundary tests below pin the convention; if the convention flips, the tests pin which side breaks.
  - **Test stand-in**: Phase 2 unit tests construct a lightweight stand-in object (e.g., `types.SimpleNamespace(start_offset=N, end_offset=M, metadata={})`) for `chunk` rather than a real `ChunkRecord`. This decouples Phase 2 from C3a's `ChunkRecord` extension and keeps Phase 2 implementable in isolation.
- **Releasable**: after this task, the enricher fully implements the brief's "Resolve page metadata" step. The pipeline is not wired yet.
- **Tests (TDD)** — `tests/test_enricher.py`:
  - Unit: `test_enrich_chunk_single_page` — chunk with `start_offset=2, end_offset=4` against table `[(0, 1), (10, 2)]` → `{"_page_start": "1"}` (no `_page_end` key).
  - Unit: `test_enrich_chunk_single_page_assert_no_page_end_key` — explicit assertion that `"_page_end" not in result` (catches accidental empty-string writes).
  - Unit: `test_enrich_chunk_cross_page` — chunk with `start_offset=8, end_offset=15` against `[(0, 1), (10, 2)]` → `{"_page_start": "1", "_page_end": "2"}`.
  - Unit: `test_enrich_chunk_first_page_seed` — chunk with `start_offset=0, end_offset=4` against `[(0, 1), (50, 2)]` → `{"_page_start": "1"}`.
  - Unit: `test_enrich_chunk_leading_marker_starts_at_page_2` — chunk against table `[(0, 2), (100, 3)]` with `start_offset=0` → `{"_page_start": "2"}`.
  - Unit: `test_enrich_chunk_end_offset_at_page_boundary` — chunk with `end_offset` exactly equal to a page-table offset (e.g., `end_offset=10` against `[(0, 1), (10, 2)]`); pins the bisect-right vs bisect-left convention.
  - Unit: `test_enrich_chunk_end_offset_one_before_boundary` — chunk with `end_offset = page_2_offset - 1` (e.g., `end_offset=9` against `[(0, 1), (10, 2)]`); paired with the test above, pins both sides of the boundary.
  - Unit: `test_enrich_chunk_page_table_none_skips_page_branch` — call with `page_table=None`; result contains no `_page_start` and no `_page_end` keys (heading-only behaviour preserved).
  - Unit: `test_enrich_chunk_values_are_strings` — both keys map to `str`, not `int`. Asserts `isinstance(result["_page_start"], str)` and the same for `_page_end` when present. Also assert that `_page_end` (when present) is `str(int(...))` form — no leading zeros, no decimal points (e.g., `'3'` not `'3.0'` not `'03'`).
  - Checkpoint: `uv run pytest tests/test_enricher.py -k enrich_chunk and (page or marker) -v`

---

### Phase 3 — `_source_subtype` mapping
> **Releasable**: when Task 3.1 is complete — extension map is callable and tested; no pipeline use yet.

#### Task 3.1 — Extend `_source_subtype` for docling sources
- [ ] **File**: `archon_search/enricher.py`
- **Depends on**: Task 1.1
- **Description**:
  - Define `_SOURCE_SUBTYPE_MAP: dict[str, str]` (module-level constant) including the C3a text-format entries (if any) AND adding:
    - `.pdf → "pdf"`
    - `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.bmp`, `.webp` → `"image"`
  - Provide a helper `def source_subtype_for(suffix: str) -> str` that lowercases the suffix and returns the map value or `""` if unmapped.
  - Provide a helper `def is_docling_source(subtype: str) -> bool` that returns `subtype in {"pdf", "image"}`. This encapsulates the docling-source predicate so `pipeline.py` does not inline the set.
  - Per the brief's "Coordinated C3a scope additions", C3b owns adding the docling entries even if C3a defined the original map.
- **Releasable**: after this task, the pipeline can compute a stable subtype from a file extension.
- **Tests (TDD)** — `tests/test_enricher.py`:
  - Unit: `test_source_subtype_pdf` — `source_subtype_for(".pdf") == "pdf"`.
  - Unit: `test_source_subtype_image_extensions` — parametrized over each image extension; each returns `"image"`.
  - Unit: `test_source_subtype_uppercase_extension` — `source_subtype_for(".PDF") == "pdf"` (the helper lowercases).
  - Unit: `test_source_subtype_unknown_returns_empty` — `source_subtype_for(".xyz") == ""`.
  - Unit: `test_is_docling_source_true_for_pdf_and_image` — `is_docling_source("pdf")` and `is_docling_source("image")` both return `True`.
  - Unit: `test_is_docling_source_false_for_text_and_empty` — `is_docling_source("markdown")`, `is_docling_source("")`, and `is_docling_source("html")` all return `False`.
  - Checkpoint: `uv run pytest tests/test_enricher.py -k source_subtype or is_docling_source -v`

---

### Phase 4 — Pipeline integration
> **Releasable**: after Phase 4 — end-to-end ingest of a PDF/image file produces chunks with `_page_start` in `ChunkRecord.metadata`.

#### Task 4.1 — Wire `preprocess` into `ingest_file`
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 2.4, Task 3.1, Task 1.2
- **Description**:
  - At the top of `ingest_file`, construct a per-call enricher: `enricher = MarkdownEnricher()`. The enricher is **not** stored on `SearchPipeline`; one fresh instance per ingest, per the "Enricher instance lifecycle" subsection in Architecture.
  - After `_extract_front_matter` (around `pipeline.py:240`) and **before** the existing language detection / chunker call:
    1. Compute `subtype = source_subtype_for(path.suffix)`.
    2. If `is_docling_source(subtype)`, call `cleaned_text, page_table = enricher.preprocess(markdown)`; rebind `markdown = cleaned_text`. Set `heading_table = None` in this branch — heading enrichment is not run on docling output in v1 (see composition contract).
    3. Otherwise, call `heading_table = enricher.prepare(markdown)` (the C3a path) and set `page_table = None`.
    4. Carry `heading_table` and `page_table` as **local variables** through the chunk loop for Task 4.2; do not stash on `self`.
  - The chunker call (`self._chunker.chunk(markdown, ...)`) is now called with marker-free text in the docling-source path. The chunker's `start_offset` / `end_offset` (from C3a's prerequisite) therefore live in post-removal coordinates, matching the `page_table`.
  - `_extract_front_matter` continues to be called on the original `markdown` for text formats — no functional change there because docling sources are not in `_FRONT_MATTER_EXTENSIONS` (`pipeline.py:110`).
- **Releasable**: after this task, markers are excised from the text that reaches the chunker. The chunker offsets and the page table share a coordinate space. Per-chunk page metadata is the next task.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Integration: `test_ingest_pdf_excises_markers_before_chunker` — feed the `three_page_pdf` fixture; spy on the chunker call (monkeypatch `self._chunker.chunk`) and assert the `text` argument contains zero occurrences of `PAGE_BREAK_MARKER`.
  - Integration: `test_ingest_pdf_passes_page_table_to_loop` — patch `MarkdownEnricher.preprocess` to return a sentinel table; assert the per-chunk loop in Task 4.2 sees it. (This test ships green only after Task 4.2 lands; until then, mark `xfail(strict=True)`. Under the Atomic-landing constraint Phase 4 ships together so the `xfail` is removed before the PR is opened.)
  - Integration: `test_ingest_text_format_unchanged` — feed a `.txt` file; assert `preprocess` is **not** called (the prepare branch runs instead); `page_table` is `None` for every `enrich_chunk` call; existing C3a heading behaviour unchanged.
  - Integration: `test_ingest_md_front_matter_unchanged` — ingest a `.md` file with YAML front matter; assert front matter is extracted (`_extract_front_matter` still runs first), `_acl` propagates onto every chunk, and the text passed to the chunker is the post-front-matter content. Catches regressions in the existing text-format path caused by restructuring the pipeline loop.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -k page_marker or page_table or front_matter -v`

#### Task 4.2 — Per-chunk metadata merge
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 4.1, Task 2.5
- **Description**:
  - After `records = self._chunker.chunk(...)` (around `pipeline.py:275`), loop:
    ```python
    for record in records:
        fragment = enricher.enrich_chunk(
            record,
            heading_table=heading_table,
            page_table=page_table,
        )
        record.metadata.update(fragment)
    ```
  - The loop runs for both text and docling sources; `heading_table` and `page_table` come from the local variables bound in Task 4.1's branches. In the text case, `page_table is None` and only C3a's heading fragment is merged; in the docling case, both can contribute.
  - The existing ACL / chunk-ID assignment loop already exists at `pipeline.py:288`; this loop runs **before** that one so `metadata` is fully populated before ACL propagation.
- **Releasable**: after this task, end-to-end ingest of a `.pdf` or image file results in `ChunkRecord.metadata["_page_start"]` (and optionally `_page_end`) being set. The feature is functionally complete pending eval gate and fixture (Phase 5) and documentation (Phase 6).
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Integration: `test_ingest_pdf_assigns_page_start_to_chunks` — feed the `three_page_pdf` fixture; assert every emitted chunk has `_page_start` in its `metadata`, values are `str`, and the set of values is a subset of `{"1", "2", "3"}`.
  - Integration: `test_ingest_pdf_cross_page_chunk_has_page_end` — the fixture pages contain `"alpha content"`, `"beta content"`, `"gamma content"` (~13 chars each); default chunk size is much larger, so this test **must** override the chunker config to `chunk_size=8, chunk_overlap=0` (or similar small values) when constructing the pipeline so that at least one chunk straddles a page boundary in the cleaned-text span (e.g., crossing offset 13 between "alpha content" and "beta content"). Assert at least one chunk has both `_page_start` and `_page_end` and they differ.
  - Integration: `test_ingest_text_md_has_no_page_fields` — feed a `.md` file; assert no chunk has `_page_start` or `_page_end` in its metadata.
  - Integration: `test_ingest_pdf_chunk_text_contains_no_marker` — assert every `ChunkRecord.text` produced from the fixture contains zero occurrences of `PAGE_BREAK_MARKER`.
  - Integration: `test_ingest_pdf_with_language_detection_uses_cleaned_text` — when language detection (C2) is enabled, the call order is `parse → preprocess → language_detect → chunk → enrich_chunk`. Monkeypatch the language detector to spy on its input string; assert the detector receives the marker-free cleaned text (zero occurrences of `PAGE_BREAK_MARKER`), not the marker-bearing parser output. Regression guard for the C2+C3b interaction.
  - Integration: `test_ingest_pdf_metadata_survives_store_roundtrip` — full ingest, then query via `SearchStore.hybrid_search()`. Spell out the embedding step: import `EvalEmbedderBackend` (or whatever deterministic backend `archon_search/eval/backends.py` exposes), instantiate it, call it on the query string `"beta content"` to obtain a `list[float]` vector, and pass that to `SearchStore.hybrid_search(query_vector=..., query_text="beta content", ...)` per the signature at `store.py:1418`. Assert `_page_start` is present in `result.metadata` for matching chunks (JSON round-trip through LanceDB).
  - Checkpoint: `uv run pytest tests/test_pipeline.py -k page_start or page_end or language_detection -v`

---

### Phase 5 — Fixture and eval gate
> **Task ordering**: Task 5.1 (fixture) must land **before** Task 1.2 within the atomic PR — Task 1.2's integration test depends on the fixture. Within the same PR, the recommended commit order is 5.1 → 1.1 → 1.2 → 2.* → 3.1 → 4.1 → 4.2 → 5.2 → F.1.
> **Releasable**: when Task 5.1 ships, the deterministic PDF fixture is available to every test in Phases 1–4; when Task 5.2 ships, the eval harness includes a page-targeted regression query.

#### Task 5.1 — `reportlab` dev dep + `three_page_pdf` conftest fixture
- [x] **File**: `pyproject.toml`, `tests/conftest.py`, `tests/fixtures/pdfs/.gitignore`
- **Depends on**: nothing
- **Description**:
  - Add `reportlab` to `[dependency-groups].dev` in `pyproject.toml`. Use the most recent compatible version available.
  - Add a session-scoped fixture `three_page_pdf` in `tests/conftest.py` that returns the absolute `Path` to `tests/fixtures/pdfs/three_page.pdf`. The fixture generates the file on first use via:
    ```python
    from reportlab.pdfgen.canvas import Canvas
    c = Canvas(str(target), pagesize=(612, 792))
    c.setCreator("archon-search-test")
    c.setProducer("archon-search-test")
    c.drawString(100, 700, "alpha content"); c.showPage()
    c.drawString(100, 700, "beta content");  c.showPage()
    c.drawString(100, 700, "gamma content"); c.showPage()
    c.save()
    ```
  - The fixture is regenerated each test session via `reportlab`; in-repo storage is gitignored (`tests/fixtures/pdfs/.gitignore` contains `*.pdf`).
  - The PDF is **not** byte-deterministic across sessions: `reportlab` embeds the current timestamp in `CreationDate` / `ModDate` regardless of `setCreator` / `setProducer` (those set metadata strings, not timestamps). The **textual content** is deterministic. Tests must rely on textual assertions (`"alpha content" in parsed`), not byte-hash assertions.
  - Factor the generation code into a small helper `tests/_pdf_fixture.py` so the eval-corpus copy (Task 5.2) can call the same generator from a different conftest.
  - Page contents are exactly `"alpha content"`, `"beta content"`, `"gamma content"` — verification cases reference these strings directly.
- **Releasable**: after this task, every test in Phases 1–2 and 4 can use the shared deterministic fixture.
- **Tests (TDD)** — `tests/test_fixtures.py`:
  - Unit: `test_three_page_pdf_exists` — fixture path resolves to an existing file.
  - Integration: `test_three_page_pdf_contains_expected_text` — parse via `archon_search.parser.Parser._parse_with_docling`; assert the parsed text contains `"alpha content"`, `"beta content"`, `"gamma content"` in that order.
  - Integration: `test_three_page_pdf_has_two_markers` — parse and assert the output contains exactly two occurrences of `PAGE_BREAK_MARKER`. **Move this test to Task 1.2's test file** (it depends on Task 1.2's parser kwarg change to pass). Listed here for traceability only; do not add it to `tests/test_fixtures.py` in Task 5.1.
  - Checkpoint: `uv run pytest tests/test_fixtures.py -k three_page_pdf -v`

#### Task 5.2 — Eval query + label
- [ ] **File**: `tests/eval/queries.jsonl`, `tests/eval/labels.jsonl`, `tests/eval/corpus/` (one fixture PDF copy or reference)
- **Depends on**: Task 5.1, Task 4.2
- **Description**:
  - Add one query to `tests/eval/queries.jsonl` of the form `{"query_id": "page_provenance_001", "text": "find the beta content on page 2"}` (exact wording per `tests/eval/README.md` conventions).
  - Add one label to `tests/eval/labels.jsonl` mapping `page_provenance_001` to the document/chunk that contains `"beta content"` (i.e. the chunk that should carry `_page_start = "2"`).
  - Ensure the fixture document is part of the eval corpus. The eval runner (`archon_search/eval/runner.py:_ingest_corpus`) ingests real files from `tests/eval/corpus/` via `pipeline.ingest_file()`, so the PDF must exist on disk **before** the runner starts. Add a **separate** `tests/eval/conftest.py` (distinct from the project-level `tests/conftest.py`) with an autouse, session-scoped fixture that generates `tests/eval/corpus/three_page.pdf` before the eval suite runs. The generator reuses the helper factored out in Task 5.1 (`tests/_pdf_fixture.py`). The two PDF files (`tests/fixtures/pdfs/three_page.pdf` for unit/integration tests and `tests/eval/corpus/three_page.pdf` for eval) are intentionally distinct; sharing only the generation code keeps the fixture surface explicit.
  - Both PDF outputs are gitignored. The unit-test `tests/conftest.py` and the eval `tests/eval/conftest.py` each own their own generation lifecycle.
  - The eval runner uses `chunk_size=256` (`archon_search/eval/runner.py`); confirm the labelled chunk under that config carries `_page_start == "2"`. If chunking dynamics make the labelled chunk straddle pages, prefer adjusting the query target rather than chunk size.
  - Do **not** change `tests/eval/thresholds.toml` — the brief explicitly states no threshold change.
  - Follow the maintenance procedure in `tests/eval/README.md`.
- **Releasable**: after this task, the eval harness includes a coarse FTS-side regression check that page-targeted queries still match the correct chunk.
- **Tests (TDD)** — `tests/eval/test_eval_suite.py`:
  - Integration: `test_eval_includes_page_provenance_query` — load `queries.jsonl`; assert `"page_provenance_001"` is present.
  - Integration: `test_eval_page_provenance_label_resolves` — load `labels.jsonl`; assert the labelled chunk's metadata includes `_page_start == "2"` after a fresh ingest.
  - Live E2E: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py` — full eval suite passes with the new query in place (no threshold change).
  - Checkpoint: `uv run pytest -m eval tests/eval/test_eval_suite.py -k page_provenance -v`

---

### Final Phase — Verification & Documentation

#### Task F.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: Task 1.2, Task 2.5, Task 3.1, Task 4.2, Task 5.2
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, `Documentation/Architecture/*`, `Documentation/UserManual/*`, `BREAKING.md`, `CHANGELOG.md` if present, `CLAUDE.md`) and update every file whose content is affected by the changes delivered in this plan. The agent must not update docs that are unrelated.
  - Mandatory documentation deltas:
    - `Documentation/Architecture/130_data_architecture_and_persistence.md` — note `_page_start` / `_page_end` as system-generated metadata keys (alongside C3a's `_heading` / `_section_path`); add a single line documenting `<!-- archon-search:pagebreak:v1 -->` as an internal, pre-chunker-only implementation detail that never reaches `ChunkRecord.text`, API responses, or schemas.
    - `Documentation/Architecture/150_security_and_privacy_architecture.md` — add a "known limitations" bullet for the marker-collision accepted risk.
    - `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` — extend the `MarkdownEnricher` entry to mention the page-break extension (`preprocess` as a sibling method to `prepare`).
    - `Documentation/Backlog/03_world_class_roadmap.md` — mark C3b shipped, set last-reviewed date.
    - **NOT `BREAKING.md`** — the marker is internal and never crosses a REST/MCP boundary; `BREAKING.md` is reserved for breaking REST/MCP surface changes (see `CLAUDE.md`).
    - `CLAUDE.md` — only if the marker-string or `_page_start` field name needs to appear as a project-level invariant (likely not).
  - The OpenAPI snapshot (`GET /openapi.json`) is unchanged — `metadata` remains an open `dict[str, str]`. Confirm and do not regenerate the snapshot.
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: after this task, C3b is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - `parser.py:_parse_with_docling` passes `page_break_placeholder=PAGE_BREAK_MARKER` to `export_to_markdown()` and the marker constant lives in `archon_search/enricher.py`.
  - Every `ChunkRecord` produced from `.pdf` and the seven image extensions has `_page_start` in its `metadata` dict; values are `str`, 1-indexed.
  - Chunks that span a marker boundary additionally have `_page_end`; chunks that do not span a boundary have **no** `_page_end` key (not empty string).
  - No `ChunkRecord.text` produced from a docling-parsed source contains the marker substring.
  - The FTS index built from those records returns zero hits for any search containing `archon-search:pagebreak`.
  - Text-format inputs (`.md`, `.txt`, `.rst`, `.html`) have no `_page_start` or `_page_end` in chunk metadata.
  - `MarkdownEnricher.preprocess` is a distinct method from C3a's `prepare`; both coexist on the class. `enrich_chunk` accepts `heading_table` and `page_table` as explicit keyword arguments. The enricher is constructed per `ingest_file` call (not stored on `SearchPipeline`); the page table is carried as a local variable through the pipeline rather than stashed on `self`.
  - `_source_subtype` is `"pdf"` for `.pdf` inputs and `"image"` for the seven image extensions; unmapped extensions yield `""`.
  - `tests/eval/queries.jsonl` includes `page_provenance_001` and the eval harness passes with no threshold changes.
  - `reportlab` is declared in `[dependency-groups].dev` and the `three_page_pdf` fixture generates a deterministic PDF on first use.
  - LanceDB round-trip preserves `_page_start` / `_page_end` keys end-to-end (verified by Task 4.2's integration test).
  - `store.reindex_metadata()` is not modified and continues to leave the `metadata` dict column untouched (regression check).
  - Coordinate-transform pure function produces the **corrected** Example A `[(0, 1), (150, 2), (365, 3), (530, 4)]` and Example B `[(0, 2), (115, 3), (330, 4)]` exactly (verified by Task 2.2's unit tests). The parent brief's incorrect Example A values are tracked as a follow-up brief edit.
  - `Documentation/Architecture/130_data_architecture_and_persistence.md` and `Documentation/Architecture/150_security_and_privacy_architecture.md` carry the documentation deltas listed above. `BREAKING.md` is **not** edited (the marker is internal).
  - `Documentation/Backlog/03_world_class_roadmap.md` marks C3b as shipped with the current last-reviewed date.
  - Default `uv run pytest` passes; the `eval` marker suite passes with `--thresholds-path tests/eval/thresholds.toml`; coverage stays at or above the existing 85% gate.
  - Coverage report after Phase 4 lands shows all new lines in `enricher.py` (`_extract_page_breaks`, `_transform_page_table`, `_excise_markers`, `preprocess`, `enrich_chunk` page branch, `is_docling_source`) are covered by Phase 2 / Phase 3 unit tests at ≥95%; combined coverage stays at or above the project's 85% gate.
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
