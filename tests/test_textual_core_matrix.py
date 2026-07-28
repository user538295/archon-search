"""Behaviour checks for the two-step Archon Search setup wizard prototype."""

import pytest

pytest.importorskip("textual")

from examples.textual_calibration import DEVICES, TICK_MAX, CalibrationScreen, CalibrationView
from examples.textual_core_matrix import CoreScreen, CoreView, WizardApp
from examples.textual_design import PALETTE

SIZE = (120, 60)


async def _finish_benchmark(app, pilot) -> CalibrationView:
    view = app.screen.query_one(CalibrationView)
    view.tick = TICK_MAX
    view.refresh()
    await pilot.pause()
    return view


def test_app_allows_selection_and_binds_quit() -> None:
    assert WizardApp.ALLOW_SELECT is True
    assert any(binding.key == "escape" and binding.action == "quit" for binding in WizardApp.BINDINGS)


async def test_wizard_starts_on_the_calibration_screen() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, CalibrationScreen)
        text = pilot.app.screen.query_one(CalibrationView).render()
        assert "DEVICE CALIBRATION" in text.plain
        assert "SETUP 01/06 · CALIBRATE" in text.plain
        assert any(PALETTE.orange in str(span.style) for span in text.spans)


async def test_benchmark_clock_advances_from_zero() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        await pilot.pause()
        view = pilot.app.screen.query_one(CalibrationView)
        assert view.tick == 0.0
        await pilot.pause(0.3)
        assert view.tick > 0.0


async def test_calibration_completion_shows_factor_and_verdict() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        await pilot.pause()
        view = await _finish_benchmark(pilot.app, pilot)
        rendered = view.render().plain
        assert "● calibrated" in rendered
        assert "×1.48" in rendered  # 71 tok/s ÷ 48 reference
        assert "ceiling: MAXIMUM" in rendered
        assert "3.0 GB peak of 11.2 GB free" in rendered


async def test_rerun_resets_the_benchmark_clock() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        await pilot.pause()
        view = await _finish_benchmark(pilot.app, pilot)
        await pilot.press("r")
        # action_rerun sets tick = 0.0, but the live on_mount _advance interval
        # may fire once before this assert, so tick can be a small non-zero
        # value. The invariant is that the clock was reset from TICK_MAX back
        # toward zero, not that it is caught at exactly 0.0 (racy).
        assert view.tick < 5.0
        assert "awaiting bench results" in view.render().plain


async def test_cycling_device_reprobes_with_a_new_factor() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        await pilot.pause()
        view = pilot.app.screen.query_one(CalibrationView)
        assert view.device.key == "m2"
        await pilot.press("d")
        assert view.device is DEVICES[1]
        assert view.tick < TICK_MAX  # cycling re-runs the bench from the start
        view.tick = TICK_MAX
        view.refresh()
        await pilot.pause()
        assert "×2.75" in view.render().plain  # 132 ÷ 48


async def test_next_advances_to_core_only_after_calibration_completes() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("n")  # bench not done yet
        assert isinstance(pilot.app.screen, CalibrationScreen)
        await _finish_benchmark(pilot.app, pilot)
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(pilot.app.screen, CoreScreen)


async def _goto_core(pilot) -> CoreView:
    await pilot.pause()
    await _finish_benchmark(pilot.app, pilot)
    await pilot.press("n")
    await pilot.pause()
    return pilot.app.screen.query_one(CoreView)


async def test_core_starts_on_the_committed_balanced_profile() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        core = await _goto_core(pilot)
        assert core.cur == 3 and core.sel_profile == 1 and core.sel_corpus == 0
        rendered = core.render().plain
        assert "CORE MATRIX LINKED" in rendered
        assert "SETUP 02/06" in rendered
        assert "BALANCED // INFO" in rendered


async def test_core_arrow_navigation_wraps_and_selection_commits() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        core = await _goto_core(pilot)
        core.cur = 0
        core.refresh()
        await pilot.press("up")  # wrap 0 -> 6 (NEXT)
        assert core.cur == 6
        await pilot.press("down")  # wrap back to 0 (English only)
        assert core.cur == 0
        await pilot.press("space")
        assert core.sel_corpus == 0
        assert core.lock == "corpus"


async def test_core_info_panel_follows_the_cursor() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        core = await _goto_core(pilot)
        core.cur = 4  # Maximum profile row
        core.refresh()
        await pilot.pause()
        assert "MAXIMUM // INFO" in core.render().plain
        core.cur = 1  # Multiple languages
        core.refresh()
        await pilot.pause()
        assert "MULTIPLE LANGUAGES // INFO" in core.render().plain


async def test_core_back_returns_to_calibration() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        core = await _goto_core(pilot)
        core.cur = 5  # BACK
        core.refresh()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, CalibrationScreen)


async def test_changing_one_section_does_not_reanimate_the_other() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        core = await _goto_core(pilot)
        core.sweep_corpus = core.sweep_profile = 10  # both settled
        core.refresh()
        await pilot.pause()

        core.cur = 0  # move onto a corpus row and commit it
        core._activate()
        assert core.sweep_corpus == 0          # corpus info re-animates
        assert core.sweep_profile == 10        # profile gauges untouched

        core.sweep_corpus = 10
        core.cur = 4  # move onto the Maximum profile and commit it
        core._activate()
        assert core.sweep_profile == 0         # profile info + gauges re-animate
        assert core.sweep_corpus == 10         # corpus info untouched


async def test_est_load_sparkline_is_live_and_uses_braille_dots() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        core = await _goto_core(pilot)
        assert len(core.spark) == 26  # live telemetry samples
        core.spark = [0, 2, 4, 6, 8] * 5 + [8]  # force every dot level
        core.refresh()
        rendered = core.render().plain
        assert "EST LOAD" in rendered
        assert all(glyph in rendered for glyph in "⣀⣤⣶⣿")  # braille dot fill, not blocks
        assert not any(glyph in rendered for glyph in "▁▂▃▄▅▆▇")  # no block bars


async def test_core_renders_est_load_and_gauges() -> None:
    async with WizardApp().run_test(size=SIZE) as pilot:
        core = await _goto_core(pilot)
        core.sweep_corpus = core.sweep_profile = 10
        core.refresh()
        rendered = core.render().plain
        assert "EST LOAD" in rendered
        assert "RETRIEVAL" in rendered and "SPEED" in rendered and "FOOTPRINT" in rendered
        assert "◇ CAPABILITIES" in rendered  # step breadcrumb
