"""Tests for reusable Textual design bricks, independent of any wizard screen."""

import pytest

pytest.importorskip("textual")

from examples.textual_design import (
    PALETTE,
    NavigationBar,
    RadioGroup,
    RadioOption,
    TableColumn,
    bold,
    frame_lines,
    italic,
    meter,
    table_lines,
    text,
    underlined,
)


def test_palette_matches_approved_html_mockup() -> None:
    assert PALETTE.background == "#181818"
    assert PALETTE.foreground == "#ffffff"
    assert PALETTE.muted == "#8b8b8b"
    assert PALETTE.accent == "#7fa8d7"
    assert PALETTE.accent_foreground == "#181818"
    assert PALETTE.confirmed == "#7fa8d7"
    assert PALETTE.meter == "#e89e63"
    assert PALETTE.stack_label == "#8b8b8b"
    assert PALETTE.stack_value == "#ffffff"


def test_text_atoms_preserve_content_and_requested_emphasis() -> None:
    plain = text("signal")
    strong = bold("signal")
    slanted = italic("signal")
    line = underlined("signal")

    assert (plain.plain, strong.plain, slanted.plain, line.plain) == ("signal",) * 4
    assert plain.style == PALETTE.foreground
    assert strong.style == f"bold {PALETTE.foreground}"
    assert slanted.style == f"italic {PALETTE.foreground}"
    assert line.style == f"underline {PALETTE.foreground}"


def test_meter_is_clamped_and_uses_square_glyphs() -> None:
    assert meter(-1) == "□□□□□"
    assert meter(3) == "■■■□□"
    assert meter(8) == "■■■■■"
    with pytest.raises(ValueError):
        meter(1, total=0)


def test_titled_frame_lines_follow_available_width() -> None:
    lines = frame_lines("CORE", ("alpha", "beta"), width=24)

    assert len(lines) == 4
    assert all(len(line) == 24 for line in lines)
    assert lines[0].startswith("┌─ CORE ")
    assert lines[-1] == "└──────────────────────┘"


def test_radio_group_keeps_cursor_and_committed_value_separate() -> None:
    group = RadioGroup((RadioOption("Minimal"), RadioOption("Balanced")), selected=1)

    assert group.selected == group.cursor == 1
    assert group.move(-1)
    assert group.cursor == 0
    assert group.selected == 1

    group.commit()
    assert group.selected == 0


def test_radio_group_hides_cursor_without_forgetting_return_position() -> None:
    group = RadioGroup((RadioOption("English"), RadioOption("Multiple")))

    group.move(1)
    group.hide_cursor()
    assert not group.cursor_visible
    assert group.cursor == 1

    group.show_cursor()
    assert group.cursor_visible
    assert group.cursor == 1


def test_navigation_bar_switches_only_between_previous_and_next() -> None:
    bar = NavigationBar()

    assert bar.active == "previous"
    assert bar.move_horizontal(1) == "next"
    assert bar.move_horizontal(-1) == "previous"


def test_table_expands_to_width_clips_and_right_aligns_columns() -> None:
    columns = (TableColumn("Profile"), TableColumn("RAM", align="right"))
    lines = table_lines(columns, (("Balanced", "1–1.5 GB"), ("Maximum profile", "2.5–3 GB")), width=20)

    assert all(len(line) == 20 for line in lines)
    assert lines[0].endswith("RAM")
    assert "Maximum profile" not in lines[2]
    assert lines[2].endswith("2.5–3 GB")


def test_table_rejects_invalid_column_contracts() -> None:
    with pytest.raises(ValueError):
        table_lines((), (), 20)
    with pytest.raises(ValueError):
        table_lines((TableColumn("A", weight=0),), (), 20)
    with pytest.raises(ValueError):
        table_lines((TableColumn("A", align="center"),), (), 20)
