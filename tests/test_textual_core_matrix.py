"""Behavior checks for the standalone Textual core-selection prototype."""

import pytest

pytest.importorskip("textual")
from textual.widgets import Button, Footer

from examples.textual_core_matrix import CoreMatrixApp
from examples.textual_design import PALETTE


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


async def test_app_has_no_footer_and_allows_text_selection() -> None:
    app = CoreMatrixApp()

    async with app.run_test():
        assert app.ALLOW_SELECT is True
        assert len(app.query(Footer)) == 0


def test_escape_is_bound_to_quit() -> None:
    assert any(binding.key == "escape" and binding.action == "quit" for binding in CoreMatrixApp.BINDINGS)


async def test_info_follows_the_cursor_within_its_frame_then_returns_to_the_committed_choice() -> None:
    app = CoreMatrixApp()

    async with app.run_test() as pilot:
        assert "ENGLISH ONLY // INFO" in app.selector.render().plain
        assert "BALANCED // INFO" in app.selector.render().plain

        await pilot.press("down")
        assert "MULTIPLE LANGUAGES // INFO" in app.selector.render().plain
        assert "Balanced profile" in app.selector.render().plain
        assert "License confirmation follows" in app.selector.render().plain

        await pilot.press("down")
        rendered = app.selector.render().plain
        assert "ENGLISH ONLY // INFO" in rendered
        assert "MINIMAL // INFO" in rendered


async def test_info_returns_to_committed_choices_after_leaving_the_selector() -> None:
    app = CoreMatrixApp()

    async with app.run_test() as pilot:
        await pilot.press("down", "enter", "up", "up")
        assert app.query_one("#previous", Button).has_focus
        assert "MULTIPLE LANGUAGES // INFO" in app.selector.render().plain

        await pilot.press("down", "down", "down", "down", "down", "down")
        assert app.query_one("#next", Button).has_focus
        assert "BALANCED // INFO" in app.selector.render().plain



async def test_next_button_arms_hardware_stage() -> None:
    app = CoreMatrixApp()

    async with app.run_test(size=(80, 30)) as pilot:
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
        assert "YOUR CORPUS" in lines[0]
        assert "ENGLISH ONLY // INFO" in selector.render().plain
        assert "SELECT SEARCH CORE" in selector.render().plain
        assert "BALANCED // INFO" in selector.render().plain


async def test_selector_does_not_overflow_when_its_frame_is_too_narrow_for_padding() -> None:
    app = CoreMatrixApp()

    async with app.run_test(size=(7, 30)):
        selector_lines = app.selector.render().plain.splitlines()

        assert app.selector.size.width == 3
        assert all(len(line) == app.selector.size.width for line in selector_lines)


async def test_panel_content_has_a_one_cell_inset_from_each_frame_border() -> None:
    app = CoreMatrixApp()

    async with app.run_test(size=(120, 30)):
        selector_lines = app.selector.render().plain.splitlines()
        profile_header = next(line for line in selector_lines if "Profile" in line and "Best for" in line)

        assert selector_lines[2][1:3] == " W"
        assert selector_lines[3][1:3] == " ▶"
        assert profile_header[1:3] == " P"
        assert selector_lines[2][-2] == " "
        assert selector_lines[3][-2] == " "
        assert profile_header[-2] == " "


async def test_each_frame_has_a_blank_line_below_its_top_border() -> None:
    app = CoreMatrixApp()

    async with app.run_test(size=(120, 30)):
        selector_lines = app.selector.render().plain.splitlines()

        top_borders = [index for index, line in enumerate(selector_lines) if line.startswith("┌")]

        assert len(top_borders) == 2
        assert all(selector_lines[index + 1][1:-1].strip() == "" for index in top_borders)


async def test_default_terminal_keeps_the_selector_and_navigation_visible() -> None:
    app = CoreMatrixApp()

    async with app.run_test(size=(80, 24)):
        next_button = app.query_one("#next", Button)

        assert app.selector.region.bottom <= app.size.height
        assert next_button.region.bottom <= app.size.height


async def test_info_rows_restore_muted_labels_and_bright_values() -> None:
    app = CoreMatrixApp()

    async with app.run_test(size=(120, 30)):
        rendered = app.selector.render()

        assert any(PALETTE.stack_label in str(span.style) for span in rendered.spans)
        assert any(PALETTE.stack_value in str(span.style) for span in rendered.spans)


async def test_language_info_values_share_one_column() -> None:
    app = CoreMatrixApp()

    async with app.run_test(size=(120, 30)):
        lines = app.selector.render().plain.splitlines()
        values = (
            "English models",
            "~330 MB · 1–1.5 GB RAM",
            "Hardware acceleration check",
        )
        positions = [next(line.index(value) for line in lines if value in line) for value in values]

        assert positions == [positions[0]] * len(positions)
