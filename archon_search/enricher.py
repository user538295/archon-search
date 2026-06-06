"""Markdown structural enrichment — heading scanner and per-chunk resolver.

Provides :class:`MarkdownEnricher` with two public methods:

* ``prepare(text)`` — scans post-front-matter text and returns a sorted
  :data:`HeadingTable` of :class:`HeadingEntry` tuples.
* ``enrich_chunk(chunk, table)`` — bisects the table on
  ``chunk.start_offset`` and returns ``{"_heading": ..., "_section_path": ...}``.
"""

from __future__ import annotations

import bisect
import re
from collections import namedtuple
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon_search._types import ChunkRecord

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

HeadingEntry = namedtuple("HeadingEntry", ["offset", "text", "level"])
"""Immutable record for a single heading found in a document.

Fields:
- ``offset`` (int): byte/character offset of the heading line's start in the
  post-front-matter text string.
- ``text`` (str): heading text with optional closing hashes stripped.
- ``level`` (int): heading level 1–6.
"""

HeadingTable = list[HeadingEntry]  # sorted by offset ascending

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MAX_LEN = 512
_ELLIPSIS = "…"  # U+2026 HORIZONTAL ELLIPSIS — counts as 1 char

# ATX heading: 1–6 hashes, at least one space, then heading text
_ATX_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

# Fence opener: ``` or ~~~, optionally followed by info string
_FENCE_OPEN_RE = re.compile(r"^(?P<char>[`~])(?P=char){2,}", re.MULTILINE)

# RST underline characters (must be repeated ≥2, same char filling the line)
_RST_UNDERLINE_CHARS = frozenset("=-~^\"#")


def _truncate(text: str) -> str:
    """Truncate text to at most _MAX_LEN characters (511 content + '…')."""
    if len(text) <= _MAX_LEN:
        return text
    return text[: _MAX_LEN - 1] + _ELLIPSIS


def _collect_fence_ranges(text: str) -> list[tuple[int, int]]:
    """Return a list of (start, end_inclusive) character ranges for fenced code blocks.

    Fences opened with backticks are only closed by a backtick fence of equal
    or greater length. Similarly for tildes. An unclosed fence extends to the
    end of the text.
    """
    ranges: list[tuple[int, int]] = []
    i = 0
    length = len(text)

    while i < length:
        m = _FENCE_OPEN_RE.search(text, i)
        if m is None:
            break
        fence_char = m.group("char")
        fence_len = len(m.group(0))
        open_start = m.start()

        # Build closer pattern: same char, ≥ fence_len, end of line.
        # Compiled here (not cached globally) because fence_char and fence_len vary
        # per opener; for typical docs with ≤ a few dozen fences the cost is negligible.
        closer_pattern = (
            r"^"
            + re.escape(fence_char * fence_len)
            + r"["
            + re.escape(fence_char)
            + r"]*\s*$"
        )
        closer_re = re.compile(closer_pattern, re.MULTILINE)
        close_m = closer_re.search(text, m.end())
        if close_m is None:
            # Unclosed fence — extends to end of text
            ranges.append((open_start, length - 1))
            break
        ranges.append((open_start, close_m.end() - 1))
        i = close_m.end()

    return ranges


def _in_fence(offset: int, fence_ranges: list[tuple[int, int]]) -> bool:
    """Return True if *offset* falls within any fence range."""
    for start, end in fence_ranges:
        if start <= offset <= end:
            return True
    return False


def _strip_closing_hashes(text: str) -> str:
    """Strip optional trailing ' ##' sequence from an ATX heading text capture."""
    return re.sub(r"\s+#+\s*$", "", text)


# ---------------------------------------------------------------------------
# MarkdownEnricher
# ---------------------------------------------------------------------------


class MarkdownEnricher:
    """Stateless heading scanner and chunk enricher.

    :meth:`prepare` and :meth:`enrich_chunk` are independent: ``enrich_chunk``
    can be called with any :data:`HeadingTable` constructed outside ``prepare``.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def prepare(self, text: str) -> HeadingTable:
        """Scan *text* and return a sorted :data:`HeadingTable`.

        Processing order:

        1. Collect fenced code block ranges (backtick and tilde fences).
        2. Scan for ATX headings (``#``–``######``).
        3. Scan for setext headings (``===`` / ``---`` underlines).
        4. If no ATX headings found, apply RST underline heuristic.
        5. Deduplicate by offset and sort ascending.
        """
        if not text:
            return []

        fence_ranges = _collect_fence_ranges(text)

        # --- ATX headings ---
        atx_entries: list[HeadingEntry] = []
        for m in _ATX_RE.finditer(text):
            if _in_fence(m.start(), fence_ranges):
                continue
            heading_text = _strip_closing_hashes(m.group(2).strip())
            level = len(m.group(1))
            atx_entries.append(HeadingEntry(offset=m.start(), text=heading_text, level=level))

        # --- Setext headings ---
        setext_entries = self._scan_setext(text, fence_ranges)

        # --- RST heuristic (only when no ATX headings found) ---
        if not atx_entries:
            rst_entries = self._scan_rst(text, fence_ranges)
            all_entries = setext_entries + rst_entries
        else:
            all_entries = atx_entries + setext_entries

        # Deduplicate by offset and sort
        seen_offsets: set[int] = set()
        unique: list[HeadingEntry] = []
        for entry in all_entries:
            if entry.offset not in seen_offsets:
                seen_offsets.add(entry.offset)
                unique.append(entry)

        unique.sort(key=lambda e: e.offset)
        return unique

    def enrich_chunk(
        self,
        chunk: "ChunkRecord",
        table: HeadingTable,
    ) -> dict[str, str]:
        """Return ``{"_heading": ..., "_section_path": ...}`` for *chunk*.

        Falls back to empty strings when *table* is empty, or
        ``chunk.start_offset < 0`` (sentinel), or the chunk precedes all headings.

        **Contract**: *table* must be sorted by ``offset`` ascending (as returned
        by :meth:`prepare`). Passing an unsorted table produces undefined results
        because this method uses :func:`bisect.bisect_right` for the lookup.
        """
        empty = {"_heading": "", "_section_path": ""}

        if not table or chunk.start_offset < 0:
            return empty

        offsets = [e.offset for e in table]
        idx = bisect.bisect_right(offsets, chunk.start_offset) - 1

        if idx < 0:
            return empty

        matched = table[idx]
        heading_text = _truncate(matched.text)

        # Build ancestor stack: for each level l from 1 to matched.level-1,
        # find the latest entry at that level with index ≤ idx.
        stack: list[str] = []
        matched_level = matched.level
        for target_level in range(1, matched_level):
            # Walk backward from idx to find closest ancestor at target_level
            ancestor_text: str | None = None
            for j in range(idx - 1, -1, -1):
                if table[j].level == target_level:
                    ancestor_text = table[j].text
                    break
            if ancestor_text is not None:
                stack.append(ancestor_text)

        stack.append(matched.text)

        # Build _section_path with left-truncation if needed
        section_path = self._build_section_path(stack)

        return {"_heading": heading_text, "_section_path": section_path}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _scan_setext(
        self,
        text: str,
        fence_ranges: list[tuple[int, int]],
    ) -> list[HeadingEntry]:
        """Scan for setext headings (underline-style H1/H2)."""
        entries: list[HeadingEntry] = []
        lines = text.splitlines(keepends=True)
        # Build a list of (line_start_offset, line_content_without_newline)
        line_offsets: list[tuple[int, str]] = []
        pos = 0
        for line in lines:
            line_offsets.append((pos, line.rstrip("\r\n")))
            pos += len(line)

        for i, (offset, line_text) in enumerate(line_offsets):
            # Check if this line is an underline (all '=' or all '-', min 2 chars)
            stripped = line_text.strip()
            if len(stripped) < 2:
                continue
            if not (all(c == "=" for c in stripped) or all(c == "-" for c in stripped)):
                continue

            # Find the immediately preceding non-blank line
            prev_idx = i - 1
            while prev_idx >= 0 and line_offsets[prev_idx][1].strip() == "":
                prev_idx -= 1

            if prev_idx < 0:
                continue

            prev_offset, prev_line = line_offsets[prev_idx]
            prev_stripped = prev_line.strip()

            # Preceding line must not be blank or itself an underline-char-only line
            if not prev_stripped:
                continue
            if all(c in "=-~^\"#" for c in prev_stripped) and len(prev_stripped) >= 2:
                continue

            # There must be NO blank lines between prev_idx and i (immediate adjacency)
            if prev_idx != i - 1:
                continue

            if _in_fence(prev_offset, fence_ranges):
                continue

            level = 1 if stripped[0] == "=" else 2
            entries.append(HeadingEntry(offset=prev_offset, text=prev_stripped, level=level))

        return entries

    def _scan_rst(
        self,
        text: str,
        fence_ranges: list[tuple[int, int]],
    ) -> list[HeadingEntry]:
        """RST heading heuristic: underline-only lines following a heading text line.

        Only applied when no ATX headings exist. Maps first-seen underline char
        to H1, second-distinct char to H2, etc.
        """
        entries: list[HeadingEntry] = []
        level_map: dict[str, int] = {}
        next_level = 1

        lines = text.splitlines(keepends=True)
        line_offsets: list[tuple[int, str]] = []
        pos = 0
        for line in lines:
            line_offsets.append((pos, line.rstrip("\r\n")))
            pos += len(line)

        for i, (offset, line_text) in enumerate(line_offsets):
            stripped = line_text.strip()
            if len(stripped) < 2:
                continue
            if stripped[0] not in _RST_UNDERLINE_CHARS:
                continue
            if not all(c == stripped[0] for c in stripped):
                continue

            # Preceding non-blank line must be the heading text
            prev_idx = i - 1
            while prev_idx >= 0 and line_offsets[prev_idx][1].strip() == "":
                prev_idx -= 1

            if prev_idx < 0:
                continue

            prev_offset, prev_line = line_offsets[prev_idx]
            prev_stripped = prev_line.strip()

            if not prev_stripped:
                continue

            # Immediately adjacent (no blank between)
            if prev_idx != i - 1:
                continue

            if _in_fence(prev_offset, fence_ranges):
                continue

            underline_char = stripped[0]
            if underline_char not in level_map:
                level_map[underline_char] = next_level
                next_level += 1
            level = level_map[underline_char]
            entries.append(HeadingEntry(offset=prev_offset, text=prev_stripped, level=level))

        return entries

    @staticmethod
    def _build_section_path(stack: list[str]) -> str:
        """Join *stack* with ' > '; left-truncate ancestors if > 512 chars."""
        if not stack:
            return ""

        # Truncate deepest element if needed
        deepest = _truncate(stack[-1])
        ancestors = stack[:-1]

        # Try dropping outermost ancestors until fits
        while True:
            parts = ancestors + [deepest]
            joined = " > ".join(parts)
            if len(joined) <= _MAX_LEN:
                return joined
            if not ancestors:
                # Only deepest remains; already truncated above
                return deepest
            ancestors = ancestors[1:]
