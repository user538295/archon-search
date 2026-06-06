"""Tests for MarkdownEnricher — Task 2.1 (prepare) and Task 2.2 (enrich_chunk)."""
from archon_search.enricher import HeadingEntry, HeadingTable, MarkdownEnricher
from archon_search._types import ChunkRecord


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
        result = enricher.enrich_chunk(chunk, [])
        assert result == {"_heading": "", "_section_path": ""}

    def test_enrich_chunk_negative_offset(self):
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=-1)
        table = self._make_table([(0, "H1", 1)])
        result = enricher.enrich_chunk(chunk, table)
        assert result == {"_heading": "", "_section_path": ""}

    def test_enrich_chunk_before_first_heading(self):
        """Chunk whose start_offset precedes all headings gets empty strings."""
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=2)
        table = self._make_table([(10, "H1", 1)])
        result = enricher.enrich_chunk(chunk, table)
        assert result == {"_heading": "", "_section_path": ""}

    def test_enrich_chunk_under_single_heading(self):
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=20)
        table = self._make_table([(0, "H1 text", 1)])
        result = enricher.enrich_chunk(chunk, table)
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
        result = enricher.enrich_chunk(chunk, table)
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
        result = enricher.enrich_chunk(chunk, table)
        assert result["_heading"] == "Deep"
        assert result["_section_path"] == "Top > Deep"

    def test_enrich_chunk_heading_truncation(self):
        """Heading text > 512 chars is truncated to 511 chars + '…' (U+2026)."""
        long_text = "A" * 600
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=100)
        table = self._make_table([(0, long_text, 1)])
        result = enricher.enrich_chunk(chunk, table)
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
        result = enricher.enrich_chunk(chunk, table)
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
        result = enricher.enrich_chunk(chunk, table)
        chunk.metadata.update(result)
        assert chunk.metadata["_heading"] == "Real Heading"

    def test_enrich_chunk_independent_of_prepare(self):
        """enrich_chunk is stateless and works without calling prepare() first."""
        enricher = MarkdownEnricher()
        table = self._make_table([(0, "Direct", 1)])
        chunk = make_chunk(start_offset=5)
        result = enricher.enrich_chunk(chunk, table)
        assert result["_heading"] == "Direct"

    def test_prepare_called_twice_uses_second_table(self):
        """No stale-cache issue: second prepare() result is independent."""
        enricher = MarkdownEnricher()
        doc_a = "# HeadingA\n\nContent A\n"
        doc_b = "# HeadingB\n\nContent B\n"
        _table_a = enricher.prepare(doc_a)
        table_b = enricher.prepare(doc_b)
        chunk_b = make_chunk(start_offset=doc_b.index("Content B"))
        result = enricher.enrich_chunk(chunk_b, table_b)
        assert result["_heading"] == "HeadingB"

    def test_enrich_chunk_at_exact_heading_offset(self):
        """chunk.start_offset == heading offset → heading IS matched."""
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=50)
        table = self._make_table([(50, "Exact", 1)])
        result = enricher.enrich_chunk(chunk, table)
        assert result["_heading"] == "Exact"

    def test_enrich_chunk_deepest_heading_over_512(self):
        """Single heading > 512 chars: both _heading and _section_path are truncated."""
        long_text = "Z" * 600
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=10)
        table = self._make_table([(0, long_text, 1)])
        result = enricher.enrich_chunk(chunk, table)
        assert len(result["_heading"]) == 512
        assert result["_heading"].endswith("…")
        assert len(result["_section_path"]) == 512
        assert result["_section_path"].endswith("…")

    def test_enrich_chunk_h2_as_first_heading_no_h1_ancestor(self):
        """enrich_chunk with H2 as the first (and only) heading — no H1 exists."""
        enricher = MarkdownEnricher()
        chunk = make_chunk(start_offset=50)
        table = self._make_table([(0, "Subsection", 2)])
        result = enricher.enrich_chunk(chunk, table)
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

        r_intro = enricher.enrich_chunk(make_chunk(start_offset=intro_offset), table)
        assert r_intro["_heading"] == "Alpha"
        assert r_intro["_section_path"] == "Alpha"

        r_section = enricher.enrich_chunk(make_chunk(start_offset=section_offset), table)
        assert r_section["_heading"] == "Beta"
        assert r_section["_section_path"] == "Alpha > Beta"

        r_deep = enricher.enrich_chunk(make_chunk(start_offset=deep_offset), table)
        assert r_deep["_heading"] == "Gamma"
        assert r_deep["_section_path"] == "Alpha > Beta > Gamma"
