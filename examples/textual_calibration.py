"""Device calibration bench — step 01 of the Archon Search setup wizard.

A live, animated benchmark that probes the host, measures embed throughput,
memory headroom and disk read, then derives a device factor used to rescale the
estimates on later wizard steps. Mirrors ``Calibration.dc.html``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rich.text import Text
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

from examples.textual_design import (
    PALETTE,
    SPINNER,
    NextScreenRequest,
    box_bottom,
    box_row,
    box_top,
    dashed,
    spread,
    stack,
)


@dataclass(frozen=True)
class Device:
    """A simulated host the bench can profile."""

    key: str
    probe: str
    tok: int          # embed throughput, tok/s
    free: float       # memory headroom, GB free
    io: int           # disk read, MB/s
    budget: str
    ceiling: int      # index into PROFILES the device can sustain


DEVICES = (
    Device("m2", "Apple M2 · 8 cores (4P+4E) · 16 GB unified · macOS", 71, 11.2, 2840, "4/8 cores", 2),
    Device("m4-pro", "Apple M4 Pro · 14 cores (10P+4E) · 36 GB unified · macOS", 132, 28.4, 5210, "7/14 cores", 2),
    Device("win-i5", "Intel Core i5-1135G7 · 4C/8T · 8 GB DDR4 · Windows 11", 29, 4.1, 1480, "2/8 threads", 1),
)

PROFILES = (("Minimal", "0.5 GB"), ("Balanced", "1.5 GB"), ("Maximum", "3.0 GB"))

REFERENCE_TOK = 48                     # M2-class reference for the factor
PHASE_DURATIONS = (14, 26, 18, 18, 12)  # ticks per phase; total 88 → ~6.2 s
TICK_MAX = sum(PHASE_DURATIONS)
LABEL_WIDTH = 18


@dataclass(frozen=True)
class Phase:
    label: str
    kind: str          # "probe" | "measure" | "factor"
    target: float = 0.0
    unit: str = ""
    decimals: int = 0


PHASES = (
    Phase("PROBE HARDWARE", "probe"),
    Phase("EMBED THROUGHPUT", "measure", unit="tok/s"),
    Phase("MEMORY HEADROOM", "measure", unit="GB free", decimals=1),
    Phase("DISK READ", "measure", unit="MB/s"),
    Phase("DERIVE FACTOR", "factor"),
)


def _ease(progress: float) -> float:
    """Quadratic ease-out, matching the mockup's 1-(1-p)^2."""
    return 1 - (1 - progress) ** 2


class CalibrationView(Static, can_focus=True):
    """The animated bench. Owns the benchmark clock and renders the whole screen."""

    BINDINGS = [
        Binding("r", "rerun", "Re-run"),
        Binding("d", "cycle_device", "Cycle device"),
        Binding("n,enter,right", "next", "Next"),
    ]

    def __init__(self, speed: float = 1.0) -> None:
        super().__init__()
        self.speed = speed
        self.device_index = 0
        self.tick = 0.0
        self.blink = True

    # --- lifecycle ---------------------------------------------------------
    def on_mount(self) -> None:
        self.set_interval(0.07, self._advance)
        self.set_interval(0.5, self._toggle_blink)

    def _advance(self) -> None:
        if self.tick < TICK_MAX:
            self.tick = min(TICK_MAX, self.tick + self.speed)
            self.refresh()

    def _toggle_blink(self) -> None:
        self.blink = not self.blink
        self.refresh()

    # --- actions -----------------------------------------------------------
    def action_rerun(self) -> None:
        self.tick = 0.0
        self.refresh()

    def action_cycle_device(self) -> None:
        self.device_index = (self.device_index + 1) % len(DEVICES)
        self.tick = 0.0
        self.refresh()

    def action_next(self) -> None:
        if self._done:
            self.post_message(NextScreenRequest())

    # --- derived state -----------------------------------------------------
    @property
    def device(self) -> Device:
        return DEVICES[self.device_index]

    @property
    def _done(self) -> bool:
        return self.tick >= TICK_MAX

    @property
    def _spinner(self) -> str:
        return SPINNER[int(self.tick) % len(SPINNER)]

    def _progress(self, index: int) -> float:
        start = sum(PHASE_DURATIONS[:index])
        return max(0.0, min(1.0, (self.tick - start) / PHASE_DURATIONS[index]))

    # --- rendering ---------------------------------------------------------
    def _phase_row(self, phase: Phase, index: int, width: int) -> Text:
        progress = self._progress(index)
        state = "wait" if progress <= 0 else "run" if progress < 1 else "done"
        glyph = {"wait": "○", "run": self._spinner, "done": "●"}[state]
        glyph_color = {"wait": PALETTE.faint, "run": PALETTE.cyan, "done": PALETTE.green}[state]
        label_color = {"wait": PALETTE.faint, "run": PALETTE.text, "done": PALETTE.dim}[state]
        value_color = {"wait": PALETTE.faint, "run": PALETTE.cyan, "done": PALETTE.text}[state]

        row = Text()
        row.append(f"{glyph} ", style=glyph_color)
        row.append(phase.label.ljust(LABEL_WIDTH) + "  ", style=label_color)

        if phase.kind == "measure":
            filled = round(progress * 10)
            for cell in range(10):
                on = cell < filled
                colour = (PALETTE.green if state == "done" else PALETTE.cyan) if on else PALETTE.cell_off
                row.append("█" if on else "░", style=colour)
            row.append("  ")

        row.append(self._phase_value(phase, progress), style=value_color)
        return box_row(row, width, color=PALETTE.border)

    def _phase_value(self, phase: Phase, progress: float) -> str:
        if progress <= 0:
            return "—"
        if phase.kind == "probe":
            shown = math.ceil(progress * len(self.device.probe))
            return self.device.probe[:shown]
        if phase.kind == "factor":
            return f"×{self.device.tok / REFERENCE_TOK * _ease(progress):.2f}"
        value = phase.target * (0.15 + 0.85 * _ease(progress))
        if progress < 1:
            value += math.sin(self.tick * 1.3) * phase.target * 0.02
        number = f"{value:.{phase.decimals}f}" if phase.decimals else f"{round(value)}"
        return f"{number} {phase.unit}"

    def _phase_target(self, phase: Phase) -> float:
        return {"EMBED THROUGHPUT": self.device.tok, "MEMORY HEADROOM": self.device.free, "DISK READ": self.device.io}.get(phase.label, 0.0)

    def _header(self, width: int) -> Text:
        left = Text("◆ ARCHON SEARCH", style=f"bold {PALETTE.accent}")
        left.append(" :: ", style=PALETTE.border)
        left.append("◉ DEVICE CALIBRATION", style=PALETTE.orange)
        elapsed = min(self.tick, TICK_MAX) * 0.07
        right = Text("ELAPSED ", style=PALETTE.cyan)
        right.append(f"{elapsed:>4.1f}s", style=PALETTE.cyan)
        right.append(" │ ", style=PALETTE.border)
        right.append("SETUP 01/06 · CALIBRATE", style=PALETTE.dim)
        return spread(left, right, width)

    def _result(self, width: int) -> list[Text]:
        lines: list[Text] = [box_top("RESULT", width, color=PALETTE.border)]
        factor_progress = self._progress(4)
        if factor_progress <= 0:
            lines.append(box_row(Text("› awaiting bench results…", style=PALETTE.faint), width, color=PALETTE.border))
            lines.append(box_bottom(width, color=PALETTE.border))
            return lines

        factor = self.device.tok / REFERENCE_TOK * _ease(max(factor_progress, 0.01))
        factor_color = PALETTE.orange if factor_progress >= 1 else PALETTE.cyan
        derive = Text("factor = ", style=PALETTE.faint)
        derive.append(f"{self.device.tok} tok/s", style=PALETTE.text)
        derive.append(" (this device) ÷ ", style=PALETTE.faint)
        derive.append(f"{REFERENCE_TOK} tok/s", style=PALETTE.text)
        derive.append(" (reference M2-class) = ", style=PALETTE.faint)
        derive.append(f"×{factor:.2f}", style=f"bold {factor_color}")
        lines.append(box_row(derive, width, color=PALETTE.border))
        lines.append(dashed(width))

        ceiling_name, ceiling_peak = PROFILES[self.device.ceiling]
        verdict = Text("MAX SAFE LOAD  ", style=f"bold {PALETTE.orange}")
        verdict.append(
            f"{ceiling_peak} peak of {self.device.free} GB free · {self.device.budget} · ceiling: {ceiling_name.upper()}",
            style=PALETTE.text,
        )
        lines.append(box_row(verdict, width, color=PALETTE.border))
        lines.append(box_row(Text("› Stored with the config · later steps scale their estimates by this factor.", style=PALETTE.faint), width, color=PALETTE.border))
        lines.append(box_bottom(width, color=PALETTE.border))
        return lines

    def _command_bar(self, width: int) -> list[Text]:
        left = Text("archon> ", style=f"bold {PALETTE.accent}")
        left.append("calibrate --device  ", style=PALETTE.dim)
        if self._done:
            left.append("● calibrated", style=PALETTE.green)
        else:
            left.append(f"{self._spinner} benchmarking…", style=PALETTE.cyan)
        right = Text("[ ↻ RERUN ]", style=PALETTE.faint)
        right.append("  ")
        right.append("[ NEXT ▶ ]", style=f"bold {PALETTE.orange}" if self._done else PALETTE.faint)
        right.append("  ")
        right.append("█" if self.blink else " ", style=PALETTE.green)
        return [
            box_top("", width, color=PALETTE.border_dim),
            box_row(spread(left, right, max(width - 4, 1)), width, color=PALETTE.border_dim),
            box_bottom(width, color=PALETTE.border_dim),
        ]

    def render(self) -> Text:
        width = max(self.size.width, 8)
        lines: list[Text] = [self._header(width), Text()]

        lines.append(box_top("BENCH", width, color=PALETTE.border))
        for index, phase in enumerate(PHASES):
            resolved = Phase(phase.label, phase.kind, self._phase_target(phase), phase.unit, phase.decimals)
            lines.append(self._phase_row(resolved, index, width))
        lines.append(box_row(Text("› First wizard step (~6 s) · re-runs automatically when hardware changes.", style=PALETTE.faint), width, color=PALETTE.border))
        lines.append(box_bottom(width, color=PALETTE.border))
        lines.append(Text())

        lines.extend(self._result(width))
        lines.append(Text())
        lines.extend(self._command_bar(width))
        return stack(lines)


class CalibrationScreen(Screen):
    """Hosts the calibration bench and forwards its NEXT request to the wizard."""

    def compose(self):
        yield CalibrationView()

    def on_mount(self) -> None:
        self.query_one(CalibrationView).focus()

    def on_next_screen_request(self, message: NextScreenRequest) -> None:
        message.stop()
        from examples.textual_core_matrix import CoreScreen

        self.app.switch_screen(CoreScreen())
