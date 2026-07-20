"""Behavior checks for the standalone Textual core-selection prototype."""

import pytest

pytest.importorskip("textual")
from textual.widgets import Button

from examples.textual_core_matrix import CoreMatrixApp
from examples.textual_design import PALETTE, TitledFrame


async def test_cursor_moves_and_enter_commits_language() -> None:
    app = CoreMatrixApp()

    async with app.run_test() as pilot:
        selector = app.selector
        assert selector.cursor == 0
        assert selector.language_index == 0

        await pilot.press("down", "enter")

        assert selector.cursor == 1
        assert selector.language_index == 1
        assert "MULTIPLE LANGUAGES EQUIPPED" in app.status_text


async def test_cursor_preview_changes_before_profile_is_committed() -> None:
    app = CoreMatrixApp()

    async with app.run_test() as pilot:
        selector = app.selector
        assert selector.profile_index == 3

        await pilot.press("down", "down")

        assert selector.cursor == 2
        assert selector.profile_index == 3
        assert "MODEL STACK // MINIMAL" in app.preview_text


async def test_next_button_arms_hardware_stage() -> None:
    app = CoreMatrixApp()

    async with app.run_test() as pilot:
        await pilot.click("#next")

        assert app.status_text == "NEXT STAGE ARMED // HARDWARE CHECK"


def test_masthead_uses_orange_for_the_signal_and_core_matrix_status() -> None:
    app = CoreMatrixApp()
    app._signal_index = len(app._signal_frames) - 1

    masthead = app._masthead_text()

    assert "● CORE MATRIX LINKED" in masthead.plain
    assert any(PALETTE.meter in str(span.style) for span in masthead.spans)


async def test_arrow_keys_navigate_the_bottom_bar_and_return_to_matching_options() -> None:
    app = CoreMatrixApp()

    async with app.run_test() as pilot:
        await pilot.press("up")
        assert app.query_one("#previous", Button).has_focus
        assert not app.selector.cursor_visible

        await pilot.press("right")
        assert app.query_one("#next", Button).has_focus

        await pilot.press("left")
        assert app.query_one("#previous", Button).has_focus

        await pilot.press("down")
        assert app.selector.has_focus
        assert app.selector.cursor_visible
        assert app.selector.cursor == 0

        await pilot.press("up", "right", "down")
        assert app.selector.has_focus
        assert app.selector.cursor == 4

        await pilot.press("down", "space")
        assert app.query_one("#next", Button).has_focus
        assert app.status_text == "NEXT STAGE ARMED // HARDWARE CHECK"


def test_selector_frame_does_not_render_a_literal_newline_escape() -> None:
    app = CoreMatrixApp()

    assert r"\n┌─ SELECT SEARCH CORE" not in app.selector.render().plain


async def test_selector_fills_and_draws_to_the_available_terminal_width() -> None:
    app = CoreMatrixApp()

    async with app.run_test(size=(120, 30)):
        selector = app.selector
        lines = selector.render().plain.splitlines()

        assert selector.size.width == 116
        assert all(len(line) == selector.size.width for line in lines)

        assert isinstance(app.preview, TitledFrame)
        preview_lines = app.preview_text.splitlines()
        assert app.preview.size.width == 116
        assert all(len(line) == app.preview.size.width for line in preview_lines)
        assert "Module" in preview_lines[1]
        assert "Loadout" in preview_lines[1]
        assert "Language stack" in preview_lines[2]


async def test_panel_titles_headings_and_rows_share_the_left_content_edge() -> None:
    app = CoreMatrixApp()

    async with app.run_test(size=(120, 30)):
        selector_lines = app.selector.render().plain.splitlines()
        preview_lines = app.preview.render().plain.splitlines()

        assert selector_lines[1][1] == "W"
        assert selector_lines[2][1] == "▶"
        assert selector_lines[6][1] == "P"
        assert preview_lines[1][1] == "M"
        assert preview_lines[2][1] == "L"
