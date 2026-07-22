"""Tests for the shared Textual design primitives, independent of any screen."""

import pytest

pytest.importorskip("textual")

from rich.text import Text

from examples.textual_design import (
    BRAILLE,
    PALETTE,
    box_bottom,
    box_row,
    box_top,
    gauge_color,
    spread,
    stack,
)


def test_palette_matches_the_handoff_tokens() -> None:
    assert PALETTE.background == "#141414"
    assert PALETTE.text == "#ECEFF0"
    assert PALETTE.accent == "#80C0F8"
    assert PALETTE.on_accent == "#141414"
    assert PALETTE.orange == "#F09850"
    assert PALETTE.cyan == "#62C9C3"
    assert PALETTE.green == "#86C08A"
    assert PALETTE.border == "#3a3f43"


def test_gauge_color_walks_green_to_red() -> None:
    assert gauge_color(0) == PALETTE.green
    assert gauge_color(5) == PALETTE.yellow
    assert gauge_color(7) == PALETTE.amber
    assert gauge_color(9) == PALETTE.red


def test_braille_ramp_covers_five_levels() -> None:
    assert len(BRAILLE) == 5
    assert BRAILLE[0] == " "
    assert BRAILLE[1:] == "⣀⣤⣶⣿"


def test_box_top_colours_border_and_title_separately() -> None:
    top = box_top("BENCH", 30)

    assert top.plain.startswith("┌─ BENCH ")
    assert top.plain.endswith("┐")
    assert len(top.plain) == 30
    assert any(PALETTE.accent in str(span.style) for span in top.spans)  # title
    assert top.style == PALETTE.border


def test_box_top_places_a_locked_chip_near_the_right_edge() -> None:
    top = box_top("CORPUS", 40, chip="● LOCKED", chip_style=f"bold {PALETTE.orange}")

    assert "● LOCKED" in top.plain
    assert top.plain.index("● LOCKED") > top.plain.index("CORPUS")
    assert any(PALETTE.orange in str(span.style) for span in top.spans)


def test_box_row_insets_and_pads_to_full_width() -> None:
    row = box_row(Text("hi"), 20)

    assert len(row.plain) == 20
    assert row.plain.startswith("│ hi")
    assert row.plain.endswith(" │")
    assert row.plain[2:-2].strip() == "hi"  # one-cell inset, padded interior


def test_box_row_clips_overlong_content() -> None:
    row = box_row(Text("x" * 100), 12)

    assert len(row.plain) == 12
    assert row.plain.startswith("│ ") and row.plain.endswith(" │")


def test_box_bottom_spans_the_width() -> None:
    bottom = box_bottom(15)

    assert bottom.plain == "└" + "─" * 13 + "┘"
    assert bottom.style == PALETTE.border


def test_spread_flushes_the_right_segment_to_the_edge() -> None:
    line = spread(Text("left"), Text("right"), 20)

    assert line.plain == "left           right"
    assert len(line.plain) == 20


def test_spread_clips_when_the_two_segments_overflow() -> None:
    line = spread(Text("a" * 15), Text("b" * 15), 20)

    assert len(line.plain) == 20


def test_stack_joins_lines_with_newlines() -> None:
    joined = stack([Text("one"), Text("two"), Text("three")])

    assert joined.plain == "one\ntwo\nthree"
