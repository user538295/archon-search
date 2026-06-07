"""Tests for MarkdownEnricher — heading scanner, page-break extraction, and chunk enrichment."""
from archon_search.enricher import (
    HeadingEntry,
    HeadingTable,
    MarkdownEnricher,
    PAGE_BREAK_MARKER,
    PAGE_BREAK_MARKER_LEN,
)
from archon_search._types import ChunkRecord


# ===========================================================================
# Task 1.1 — PAGE_BREAK_MARKER constant
# ===========================================================================

class TestPageBreakMarkerConstant:
    def test_page_break_marker_literal(self):
        """Marker must be exactly the canonical namespaced string, 35 chars."""
        assert PAGE_BREAK_MARKER == "<!-- archon-search:pagebreak:v1 -->"
        assert len(PAGE_BREAK_MARKER) == 35

    def test_page_break_marker_len_matches(self):
        """PAGE_BREAK_MARKER_LEN must equal len(PAGE_BREAK_MARKER)."""
        assert PAGE_BREAK_MARKER_LEN == len(PAGE_BREAK_MARKER)

    def test_page_break_marker_namespaced(self):
        """Marker contains 'archon-search:' and ':v1' for collision-resistance."""
        assert "archon-search:" in PAGE_BREAK_MARKER
        assert ":v1" in PAGE_BREAK_MARKER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_chunk(start_offset: int = 0, end_offset: int = 10, metadata: dict | None = None) -> ChunkRecord:
    return ChunkRecord(
        doc_id="d",
        chunk_id="d-000000",
        text="text",
        vector=[0.0],
        source_path="/tmp/doc.md",
        indexed_at="2026-01-01T00:00:00.000000Z",
        start_offset=start_offset,
        end_offset=end_offset,
        metadata=metadata or {},
    )


# ===========================================================================
# Task 2.1 — MarkdownEnricher.prepare()
# ===========================================================================

class TestPrepareATXHeadings:
    def test_prepare_atx_headings(self):
        text = "# H1\n\n## H2\n\n### H3\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert len(table) == 3
        assert table[0].text == "H1"
        assert table[0].level == 1
        assert table[1].text == "H2"
        assert table[1].level == 2
        assert table[2].text == "H3"
        assert table[2].level == 3
        # offsets should match start of each heading line
        assert table[0].offset == text.index("# H1")
        assert table[1].offset == text.index("## H2")
        assert table[2].offset == text.index("### H3")

    def test_prepare_atx_no_space_not_matched(self):
        """ATX headings require at least one space after the hash(es)."""
        text = "#NoSpace heading\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert table == []

    def test_prepare_atx_seven_hashes_not_matched(self):
        """Only H1–H6 are valid ATX headings."""
        text = "####### Seven\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert table == []

    def test_prepare_atx_closing_hashes_stripped(self):
        """Trailing ` ## ` sequences are stripped from the heading text."""
        text = "## Heading ##\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert len(table) == 1
        assert table[0].text == "Heading"


class TestPrepareSetextHeadings:
    def test_prepare_setext_headings(self):
        text = "Title\n=====\n\nSubtitle\n--------\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert len(table) == 2
        # H1 from === underline
        assert table[0].level == 1
        assert table[0].text == "Title"
        assert table[0].offset == text.index("Title")
        # H2 from --- underline
        assert table[1].level == 2
        assert table[1].text == "Subtitle"
        assert table[1].offset == text.index("Subtitle")

    def test_prepare_setext_dash_not_thematic_break(self):
        """A '---' line following a blank line is NOT a setext heading."""
        text = "Some paragraph\n\n---\n\nMore text\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert table == []

    def test_prepare_setext_inside_fence(self):
        """Setext heading pattern inside a fenced code block is not detected."""
        text = "```\nHeading\n=======\n```\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert table == []


class TestPrepareFenceExclusion:
    def test_prepare_fence_length_matching(self):
        """A 4-backtick fence is NOT closed by a 3-backtick line."""
        text = "````\ncode\n```\nstill inside\n````\n# After\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        # Only the heading after the properly-closed 4-backtick fence is detected
        assert len(table) == 1
        assert table[0].text == "After"

    def test_prepare_fence_exclusion_backtick(self):
        """ATX heading inside a backtick fence is excluded."""
        text = "Before\n\n```\n# Inside fence\n```\n\nAfter\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert table == []

    def test_prepare_fence_exclusion_tilde(self):
        """ATX heading inside a tilde fence is excluded."""
        text = "Before\n\n~~~\n# Inside fence\n~~~\n\nAfter\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert table == []

    def test_prepare_unclosed_fence(self):
        """An unclosed fence extends to end-of-text; heading inside is excluded."""
        text = "```\n# Inside unclosed fence\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert table == []

    def test_prepare_fence_same_char_only(self):
        """A backtick fence is NOT closed by a tilde fence."""
        text = "```\n# Inside\n~~~\n# Still inside\n```\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        # The backtick fence closes at the final ``` — everything before is fenced
        assert table == []


class TestPrepareEdgeCases:
    def test_prepare_empty_text(self):
        enricher = MarkdownEnricher()
        assert enricher.prepare("") == []

    def test_prepare_no_headings(self):
        text = "Just some plain prose without any headings.\n"
        enricher = MarkdownEnricher()
        assert enricher.prepare(text) == []

    def test_prepare_sorted_by_offset(self):
        text = "## Second heading\n\n# First heading\n"
        # "## Second heading" appears before "# First heading" in text
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert len(table) == 2
        for i in range(len(table) - 1):
            assert table[i].offset < table[i + 1].offset


class TestPrepareRSTHeuristic:
    def test_prepare_rst_heuristic(self):
        """RST-style underline headings detected when no ATX headings are present."""
        text = "First Heading\n=============\n\nSecond Heading\n--------------\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)
        assert len(table) == 2
        assert table[0].text == "First Heading"
        assert table[0].level == 1  # first-seen underline char '=' → H1
        assert table[1].text == "Second Heading"
        assert table[1].level == 2  # first-seen underline char '-' → H2


# ===========================================================================
# Task 2.2 — MarkdownEnricher.enrich_chunk()
# ===========================================================================

class TestEnrichChunk:
    def _make_table(self, entries: list[tuple[int, str, int]]) -> HeadingTable:
        return [HeadingEntry(offset=o, text=t, level=l) for o, t, l in entries]

    def test_enrich_chunk_empty_table(self):
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=10)
        result = enricher.enrich_chunk(chunk, heading_table=[])
        assert result == {"_heading": "", "_section_path": ""}

    def test_enrich_chunk_negative_offset(self):
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=-1)
        table = self._make_table([(0, "H1", 1)])
        result = enricher.enrich_chunk(chunk, heading_table=table)
        assert result == {"_heading": "", "_section_path": ""}

    def test_enrich_chunk_before_first_heading(self):
        """Chunk whose start_offset precedes all headings gets empty strings."""
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=2)
        table = self._make_table([(10, "H1", 1)])
        result = enricher.enrich_chunk(chunk, heading_table=table)
        assert result == {"_heading": "", "_section_path": ""}

    def test_enrich_chunk_under_single_heading(self):
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=20)
        table = self._make_table([(0, "H1 text", 1)])
        result = enricher.enrich_chunk(chunk, heading_table=table)
        assert result["_heading"] == "H1 text"
        assert result["_section_path"] == "H1 text"

    def test_enrich_chunk_section_path_nested(self):
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=100)
        table = self._make_table([
            (0, "Alpha", 1),
            (30, "Beta", 2),
            (60, "Gamma", 3),
        ])
        result = enricher.enrich_chunk(chunk, heading_table=table)
        assert result["_heading"] == "Gamma"
        assert result["_section_path"] == "Alpha > Beta > Gamma"

    def test_enrich_chunk_heading_level_jump(self):
        """H1 → H3 (no H2) results in 'H1 > H3' path."""
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=100)
        table = self._make_table([
            (0, "Top", 1),
            (50, "Deep", 3),
        ])
        result = enricher.enrich_chunk(chunk, heading_table=table)
        assert result["_heading"] == "Deep"
        assert result["_section_path"] == "Top > Deep"

    def test_enrich_chunk_heading_truncation(self):
        """Heading text > 512 chars is truncated to 511 chars + '…' (U+2026)."""
        long_text = "A" * 600
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=100)
        table = self._make_table([(0, long_text, 1)])
        result = enricher.enrich_chunk(chunk, heading_table=table)
        assert result["_heading"].endswith("…")
        assert len(result["_heading"]) == 512

    def test_enrich_chunk_section_path_truncation(self):
        """Outermost ancestors are dropped when joined path > 512 chars."""
        h1 = "A" * 200
        h2 = "B" * 200
        h3 = "C" * 100
        h4 = "D" * 50
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=1000)
        table = self._make_table([
            (0, h1, 1),
            (100, h2, 2),
            (200, h3, 3),
            (300, h4, 4),
        ])
        result = enricher.enrich_chunk(chunk, heading_table=table)
        section_path = result["_section_path"]
        assert len(section_path) <= 512
        # deepest element (h4) must be present unchanged
        assert section_path.endswith(h4)
        # no leading separator
        assert not section_path.startswith(" > ")
        # outermost (h1) must have been dropped
        assert h1 not in section_path

    def test_enrich_chunk_update_overwrites_existing_key(self):
        """enrich_chunk result, when merged via dict.update, overwrites pre-existing keys."""
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=10, metadata={"_heading": "pre-existing"})
        table = self._make_table([(0, "Real Heading", 1)])
        result = enricher.enrich_chunk(chunk, heading_table=table)
        chunk.metadata.update(result)
        assert chunk.metadata["_heading"] == "Real Heading"

    def test_enrich_chunk_independent_of_prepare(self):
        """enrich_chunk is stateless and works without calling prepare() first."""
        enricher = MarkdownEnricher()
        table = self._make_table([(0, "Direct", 1)])
        chunk = make_chunk(start_offset=5)
        result = enricher.enrich_chunk(chunk, heading_table=table)
        assert result["_heading"] == "Direct"

    def test_prepare_called_twice_uses_second_table(self):
        """No stale-cache issue: second prepare() result is independent."""
        enricher = MarkdownEnricher()
        doc_a = "# HeadingA\n\nContent A\n"
        doc_b = "# HeadingB\n\nContent B\n"
        _table_a = enricher.prepare(doc_a)
        table_b = enricher.prepare(doc_b)
        chunk_b = make_chunk(start_offset=doc_b.index("Content B"))
        result = enricher.enrich_chunk(chunk_b, heading_table=table_b)
        assert result["_heading"] == "HeadingB"

    def test_enrich_chunk_at_exact_heading_offset(self):
        """chunk.start_offset == heading offset → heading IS matched."""
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=50)
        table = self._make_table([(50, "Exact", 1)])
        result = enricher.enrich_chunk(chunk, heading_table=table)
        assert result["_heading"] == "Exact"

    def test_enrich_chunk_deepest_heading_over_512(self):
        """Single heading > 512 chars: both _heading and _section_path are truncated."""
        long_text = "Z" * 600
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=10)
        table = self._make_table([(0, long_text, 1)])
        result = enricher.enrich_chunk(chunk, heading_table=table)
        assert len(result["_heading"]) == 512
        assert result["_heading"].endswith("…")
        assert len(result["_section_path"]) == 512
        assert result["_section_path"].endswith("…")

    def test_enrich_chunk_h2_as_first_heading_no_h1_ancestor(self):
        """enrich_chunk with H2 as the first (and only) heading — no H1 exists."""
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=50)
        table = self._make_table([(0, "Subsection", 2)])
        result = enricher.enrich_chunk(chunk, heading_table=table)
        assert result["_heading"] == "Subsection"
        assert result["_section_path"] == "Subsection"

    def test_enricher_full_document(self):
        """Integration: prepare + enrich_chunk on a real markdown document."""
        text = "# Alpha\n\nIntro text.\n\n## Beta\n\nSection text.\n\n### Gamma\n\nDeep text.\n"
        enricher = MarkdownEnricher()
        table = enricher.prepare(text)

        # Verify heading table
        assert len(table) == 3
        assert table[0].level == 1
        assert table[0].text == "Alpha"
        assert table[0].offset == text.index("# Alpha")
        assert table[1].level == 2
        assert table[1].text == "Beta"
        assert table[1].offset == text.index("## Beta")
        assert table[2].level == 3
        assert table[2].text == "Gamma"
        assert table[2].offset == text.index("### Gamma")

        # Enrich each chunk
        intro_offset = text.index("Intro text")
        section_offset = text.index("Section text")
        deep_offset = text.index("Deep text")

        r_intro = enricher.enrich_chunk(make_chunk(start_offset=intro_offset), heading_table=table)
        assert r_intro["_heading"] == "Alpha"
        assert r_intro["_section_path"] == "Alpha"

        r_section = enricher.enrich_chunk(make_chunk(start_offset=section_offset), heading_table=table)
        assert r_section["_heading"] == "Beta"
        assert r_section["_section_path"] == "Alpha > Beta"

        r_deep = enricher.enrich_chunk(make_chunk(start_offset=deep_offset), heading_table=table)
        assert r_deep["_heading"] == "Gamma"
        assert r_deep["_section_path"] == "Alpha > Beta > Gamma"


# ===========================================================================
# Task 2.5 — enrich_chunk page-break branch
# ===========================================================================

import types  # noqa: E402  (inline import; only used in this section)


class TestEnrichChunkPageBreak:
    """Tests for the page-break branch of enrich_chunk (Task 2.5)."""

    def _enrich(self) -> MarkdownEnricher:
        return MarkdownEnricher()

    def _chunk(self, start_offset: int, end_offset: int):
        """Lightweight chunk stand-in using types.SimpleNamespace."""
        return types.SimpleNamespace(start_offset=start_offset, end_offset=end_offset, metadata={})

    # ------------------------------------------------------------------
    # Basic page resolution
    # ------------------------------------------------------------------

    def test_enrich_chunk_single_page(self):
        """Chunk entirely within page 1 → _page_start='1', no _page_end key."""
        table = [(0, 1), (10, 2)]
        chunk = self._chunk(start_offset=2, end_offset=4)
        result = self._enrich().enrich_chunk(chunk, page_table=table)
        assert result.get("_page_start") == "1"
        assert "_page_end" not in result

    def test_enrich_chunk_single_page_assert_no_page_end_key(self):
        """Explicit assertion that _page_end is absent (catches empty-string writes)."""
        table = [(0, 1), (10, 2)]
        chunk = self._chunk(start_offset=2, end_offset=4)
        result = self._enrich().enrich_chunk(chunk, page_table=table)
        assert "_page_end" not in result

    def test_enrich_chunk_cross_page(self):
        """Chunk spanning a page boundary → both _page_start and _page_end."""
        table = [(0, 1), (10, 2)]
        chunk = self._chunk(start_offset=8, end_offset=15)
        result = self._enrich().enrich_chunk(chunk, page_table=table)
        assert result["_page_start"] == "1"
        assert result["_page_end"] == "2"

    def test_enrich_chunk_first_page_seed(self):
        """Chunk at offset 0 → page 1 from the seed entry."""
        table = [(0, 1), (50, 2)]
        chunk = self._chunk(start_offset=0, end_offset=4)
        result = self._enrich().enrich_chunk(chunk, page_table=table)
        assert result["_page_start"] == "1"
        assert "_page_end" not in result

    def test_enrich_chunk_leading_marker_starts_at_page_2(self):
        """Table starts at (0, 2) (leading-marker case) → chunk at 0 resolves to page 2."""
        table = [(0, 2), (100, 3)]
        chunk = self._chunk(start_offset=0, end_offset=5)
        result = self._enrich().enrich_chunk(chunk, page_table=table)
        assert result["_page_start"] == "2"

    # ------------------------------------------------------------------
    # Boundary pinning (bisect_right convention)
    # ------------------------------------------------------------------

    def test_enrich_chunk_end_offset_at_page_boundary(self):
        """end_offset exactly at a page-table entry offset: bisect_right puts it on the new page."""
        table = [(0, 1), (10, 2)]
        chunk = self._chunk(start_offset=5, end_offset=10)
        result = self._enrich().enrich_chunk(chunk, page_table=table)
        # bisect_right(offsets=[0,10], 10) = 2 → index 1 → page 2
        # bisect_right(offsets=[0,10], 5) = 1 → index 0 → page 1
        assert result["_page_start"] == "1"
        assert result["_page_end"] == "2"

    def test_enrich_chunk_end_offset_one_before_boundary(self):
        """end_offset one before a page-table entry → stays on the earlier page, no _page_end."""
        table = [(0, 1), (10, 2)]
        chunk = self._chunk(start_offset=5, end_offset=9)
        result = self._enrich().enrich_chunk(chunk, page_table=table)
        assert result["_page_start"] == "1"
        assert "_page_end" not in result

    # ------------------------------------------------------------------
    # None / absent page_table (heading-only path)
    # ------------------------------------------------------------------

    def test_enrich_chunk_page_table_none_skips_page_branch(self):
        """page_table=None → no _page_start, no _page_end; heading branch still fires."""
        chunk = self._chunk(start_offset=5, end_offset=9)
        result = self._enrich().enrich_chunk(chunk, page_table=None)
        assert "_page_start" not in result
        assert "_page_end" not in result

    # ------------------------------------------------------------------
    # Type invariants
    # ------------------------------------------------------------------

    def test_enrich_chunk_values_are_strings(self):
        """Both _page_start and _page_end (when present) must be str, not int or float."""
        table = [(0, 1), (5, 2)]
        chunk = self._chunk(start_offset=3, end_offset=7)
        result = self._enrich().enrich_chunk(chunk, page_table=table)
        assert isinstance(result["_page_start"], str)
        assert isinstance(result["_page_end"], str)
        # Must be plain integer string — no leading zeros, no decimal points
        assert result["_page_start"] == str(int(result["_page_start"]))
        assert result["_page_end"] == str(int(result["_page_end"]))

    # ------------------------------------------------------------------
    # Interaction with heading_table (combined case)
    # ------------------------------------------------------------------

    def test_enrich_chunk_both_tables_combined(self):
        """When both heading_table and page_table are provided, both branches fire."""
        page_table = [(0, 1), (100, 2)]
        heading_table = [HeadingEntry(offset=0, text="Intro", level=1)]
        chunk = make_chunk(start_offset=10, end_offset=20)
        result = MarkdownEnricher().enrich_chunk(
            chunk, heading_table=heading_table, page_table=page_table
        )
        assert result["_heading"] == "Intro"
        assert result["_section_path"] == "Intro"
        assert result["_page_start"] == "1"

    def test_enrich_chunk_heading_table_none_no_heading_keys(self):
        """heading_table=None → no _heading or _section_path keys in result."""
        page_table = [(0, 1)]
        chunk = self._chunk(start_offset=0, end_offset=5)
        result = self._enrich().enrich_chunk(chunk, heading_table=None, page_table=page_table)
        assert "_heading" not in result
        assert "_section_path" not in result
        assert result.get("_page_start") == "1"

    # ------------------------------------------------------------------
    # Robustness guards
    # ------------------------------------------------------------------

    def test_enrich_chunk_empty_page_table_skips_page_branch(self):
        """page_table=[] (empty) → page branch is skipped, no crash, no page keys."""
        chunk = self._chunk(start_offset=5, end_offset=9)
        result = self._enrich().enrich_chunk(chunk, page_table=[])
        assert "_page_start" not in result
        assert "_page_end" not in result

    def test_enrich_chunk_negative_start_offset_skips_page_branch(self):
        """chunk.start_offset=-1 sentinel + page_table → page branch skipped, no wrong -1 index."""
        table = [(0, 1), (50, 2), (100, 3)]
        chunk = self._chunk(start_offset=-1, end_offset=5)
        result = self._enrich().enrich_chunk(chunk, page_table=table)
        assert "_page_start" not in result
        assert "_page_end" not in result

    def test_enrich_chunk_does_not_mutate_chunk_metadata(self):
        """enrich_chunk must not mutate chunk.metadata — only returns a dict fragment."""
        table = [(0, 1), (10, 2)]
        chunk = self._chunk(start_offset=5, end_offset=15)
        original_metadata = dict(chunk.metadata)
        self._enrich().enrich_chunk(chunk, page_table=table)
        assert chunk.metadata == original_metadata


# ===========================================================================
# Task 2.1 — _extract_page_breaks
# ===========================================================================

M = PAGE_BREAK_MARKER
ML = PAGE_BREAK_MARKER_LEN


class TestExtractPageBreaks:
    def _enrich(self) -> MarkdownEnricher:
        return MarkdownEnricher()

    def test_extract_page_breaks_no_markers(self):
        """No markers in text → seed-only table [(0, 1)]."""
        result = self._enrich()._extract_page_breaks("plain text no markers")
        assert result == [(0, 1)]

    def test_extract_page_breaks_empty_text(self):
        """Empty string → seed-only table [(0, 1)]."""
        result = self._enrich()._extract_page_breaks("")
        assert result == [(0, 1)]

    def test_extract_page_breaks_three_pages(self):
        """Three pages: seed + two marker entries."""
        text = "A" + M + "B" + M + "C"
        result = self._enrich()._extract_page_breaks(text)
        # "A" is 1 char, first marker at offset 1
        # "B" is 1 char, second marker at offset 1 + ML + 1 = 2 + ML
        assert result == [(0, 1), (1, 2), (2 + ML, 3)]

    def test_extract_page_breaks_leading_marker(self):
        """Text starts with marker → no seed, first entry is (0, 2)."""
        text = M + "B"
        result = self._enrich()._extract_page_breaks(text)
        assert result == [(0, 2)]

    def test_extract_page_breaks_consecutive_markers(self):
        """Two consecutive markers (empty middle page)."""
        text = "A" + M + M + "C"
        result = self._enrich()._extract_page_breaks(text)
        assert result == [(0, 1), (1, 2), (1 + ML, 3)]

    def test_extract_page_breaks_trailing_marker(self):
        """Trailing marker after content — empty page recorded."""
        text = "A" + M
        result = self._enrich()._extract_page_breaks(text)
        assert result == [(0, 1), (1, 2)]


# ===========================================================================
# Task 2.2 — _transform_page_table
# ===========================================================================

class TestTransformPageTable:
    """Tests for MarkdownEnricher._transform_page_table (pure static function)."""

    def test_transform_page_table_no_leading_marker(self):
        """Example A (no leading marker): corrects pre-removal offsets."""
        pre = [(0, 1), (150, 2), (400, 3), (600, 4)]
        result = MarkdownEnricher._transform_page_table(pre, marker_len=35)
        assert result == [(0, 1), (150, 2), (365, 3), (530, 4)]

    def test_transform_page_table_leading_marker(self):
        """Example B (leading marker): no seed, all entries are markers."""
        pre = [(0, 2), (150, 3), (400, 4)]
        result = MarkdownEnricher._transform_page_table(pre, marker_len=35)
        assert result == [(0, 2), (115, 3), (330, 4)]

    def test_transform_page_table_empty(self):
        """Empty pre-table → empty result."""
        result = MarkdownEnricher._transform_page_table([], marker_len=35)
        assert result == []

    def test_transform_page_table_single_entry(self):
        """Seed-only table: single entry (0, 1) is unchanged (no markers)."""
        result = MarkdownEnricher._transform_page_table([(0, 1)], marker_len=35)
        assert result == [(0, 1)]

    def test_transform_page_table_uses_len_marker(self):
        """Re-run Example A with PAGE_BREAK_MARKER_LEN to confirm production wiring."""
        pre = [(0, 1), (150, 2), (400, 3), (600, 4)]
        result = MarkdownEnricher._transform_page_table(pre, marker_len=PAGE_BREAK_MARKER_LEN)
        assert result == [(0, 1), (150, 2), (365, 3), (530, 4)]

    def test_transform_page_table_matches_cleaned_text_geometry(self):
        """Ground-truth check: post-removal offsets must match actual positions in cleaned text."""
        page1 = "a" * 150
        page2 = "b" * 215
        page3 = "c" * 165
        page4 = "d" * 70
        pre_text = page1 + M + page2 + M + page3 + M + page4

        # Build pre-table by hand
        m1 = len(page1)                             # 150
        m2 = len(page1) + ML + len(page2)           # 150 + 35 + 215 = 400
        m3 = len(page1) + ML + len(page2) + ML + len(page3)  # 400 + 35 + 165 = 600
        pre = [(0, 1), (m1, 2), (m2, 3), (m3, 4)]

        result = MarkdownEnricher._transform_page_table(pre, marker_len=ML)

        # Independently compute cleaned text
        cleaned = pre_text.replace(M, "")
        # Find actual positions of each page's content in cleaned text
        pos_b = cleaned.index("b")
        pos_c = cleaned.index("c")
        pos_d = cleaned.index("d")

        assert result[0] == (0, 1)
        assert result[1] == (pos_b, 2)
        assert result[2] == (pos_c, 3)
        assert result[3] == (pos_d, 4)

    def test_transform_does_not_mutate_input(self):
        """The function must return a new list and not mutate the input."""
        pre = [(0, 1), (150, 2), (400, 3)]
        original = list(pre)
        MarkdownEnricher._transform_page_table(pre, marker_len=35)
        assert pre == original


# ===========================================================================
# Task 2.3 — _excise_markers
# ===========================================================================

M = PAGE_BREAK_MARKER


class TestExciseMarkers:
    """Tests for MarkdownEnricher._excise_markers."""

    def _enrich(self) -> MarkdownEnricher:
        return MarkdownEnricher()

    def test_excise_markers_removes_all(self):
        """All occurrences of the marker are removed."""
        text = "A" + M + "B" + M + "C"
        result = self._enrich()._excise_markers(text)
        assert result == "ABC"

    def test_excise_markers_no_marker_unchanged(self):
        """Text without a marker is returned byte-identical."""
        text = "no markers here"
        result = self._enrich()._excise_markers(text)
        assert result == text

    def test_excise_markers_leading_and_trailing(self):
        """Leading and trailing markers are removed, inner content preserved."""
        text = M + "B" + M
        result = self._enrich()._excise_markers(text)
        assert result == "B"


# ===========================================================================
# Task 2.4 — preprocess
# ===========================================================================


class TestPreprocess:
    """Tests for MarkdownEnricher.preprocess — pre-chunking entry point for docling sources."""

    def _enrich(self) -> MarkdownEnricher:
        return MarkdownEnricher()

    def test_preprocess_returns_cleaned_text_and_table(self):
        """Standard case: three pages → cleaned text with correct post-removal table."""
        text = "alpha" + M + "beta" + M + "gamma"
        cleaned, table = self._enrich().preprocess(text)
        assert cleaned == "alphabetagamma"
        # post-removal offsets: page 1 at 0, page 2 at len("alpha")=5,
        # page 3 at len("alpha") + len("beta")=5+4=9
        assert table == [(0, 1), (5, 2), (9, 3)]

    def test_preprocess_no_markers(self):
        """No markers: text unchanged, table is seed-only [(0, 1)]."""
        text = "plain text without markers"
        cleaned, table = self._enrich().preprocess(text)
        assert cleaned == text
        assert table == [(0, 1)]

    def test_preprocess_leading_marker(self):
        """Text starts with marker: cleaned text has no leading marker, table starts at page 2."""
        text = M + "beta"
        cleaned, table = self._enrich().preprocess(text)
        assert cleaned == "beta"
        assert table == [(0, 2)]

    def test_preprocess_table_returned_not_stored_on_self(self):
        """The page table is returned, not stored as instance state."""
        enricher = self._enrich()
        text = "alpha" + M + "beta"
        _, table = enricher.preprocess(text)
        # Should not have a _page_table attribute on the enricher
        assert not hasattr(enricher, "_page_table")
        # The table should be in the return value, not on the instance
        assert table == [(0, 1), (5, 2)]

    def test_preprocess_does_not_mutate_input(self):
        """preprocess must not mutate the input text string."""
        text = "alpha" + M + "beta"
        original = text
        self._enrich().preprocess(text)
        assert text == original
