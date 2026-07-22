"""Shared terminal-native design primitives for the Archon Search setup wizard.

Colours and glyphs mirror the Claude Design handoff bundle (``Calibration.dc.html``
and ``Core Step.dc.html``). These are runnable, non-production UI experiments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from rich.text import Text
from textual.message import Message


@dataclass(frozen=True)
class Palette:
    """The exact colour system used by the handoff HTML mockups."""

    background: str = "#141414"
    panel: str = "#101010"       # command-bar background
    border: str = "#3a3f43"
    border_dim: str = "#2c3033"  # command-bar border
    text: str = "#ECEFF0"
    dim: str = "#8b9195"
    faint: str = "#565b5f"
    accent: str = "#80C0F8"      # ARCHON blue
    on_accent: str = "#141414"   # text on an accent background
    orange: str = "#F09850"
    cyan: str = "#62C9C3"
    green: str = "#86C08A"
    yellow: str = "#E8C87E"
    amber: str = "#F0A860"
    red: str = "#E88A78"
    cell_off: str = "#2a2d30"    # unfilled benchmark cell
    cell_hidden: str = "#242628"  # un-revealed gauge cell


PALETTE = Palette()

# 10-frame braille spinner, indexed by floor(elapsed) % 10 (matches the mockups).
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Braille dot-fill ramp for the EST LOAD bar: blank, one-, two-, three-, four-dot rows.
BRAILLE = " ⣀⣤⣶⣿"


def gauge_color(index: int, total: int = 10) -> str:
    """Green→yellow→amber→red gradient across a ``total``-wide gauge."""
    ratio = index / (total - 1)
    if ratio < 0.4:
        return PALETTE.green
    if ratio < 0.7:
        return PALETTE.yellow
    if ratio < 0.88:
        return PALETTE.amber
    return PALETTE.red


def stack(lines: Iterable[Text]) -> Text:
    """Join rendered lines with newlines into one renderable ``Text``."""
    out = Text()
    for index, line in enumerate(lines):
        if index:
            out.append("\n")
        out.append_text(line)
    return out


def spread(left: Text, right: Text, width: int) -> Text:
    """Lay ``left`` and ``right`` on one line, ``right`` flushed to ``width``."""
    gap = max(width - left.cell_len - right.cell_len, 1)
    line = left.copy()
    line.append(" " * gap)
    line.append_text(right)
    line.truncate(max(width, 1), overflow="crop", pad=True)
    return line


def box_top(
    title: str,
    width: int,
    *,
    color: str = PALETTE.border,
    title_color: str = PALETTE.accent,
    chip: str = "",
    chip_style: str | None = None,
) -> Text:
    """Top border of a titled panel; ``title`` gets ``title_color``, border ``color``.

    An optional right-aligned ``chip`` (styled with ``chip_style``) floats near the
    right edge, matching the LOCKED badge in the mockups.
    """
    interior = max(width - 2, 1)
    left = f"─ {title} "
    if chip:
        chip_seg = f" {chip} "
        fill = max(interior - len(left) - len(chip_seg), 0)
        mid = (left + "─" * fill + chip_seg)[:interior].ljust(interior, "─")
    else:
        mid = left[:interior].ljust(interior, "─")
    line = Text(f"┌{mid}┐", style=color)
    if title:
        start = line.plain.find(title)
        if start != -1:
            line.stylize(title_color, start, start + len(title))
    if chip and chip_style:
        start = line.plain.find(chip)
        if start != -1:
            line.stylize(chip_style, start, start + len(chip))
    return line


def box_bottom(width: int, *, color: str = PALETTE.border) -> Text:
    """Bottom border of a titled panel."""
    interior = max(width - 2, 1)
    return Text(f"└{'─' * interior}┘", style=color)


def box_row(interior: Text, width: int, *, color: str = PALETTE.border) -> Text:
    """Wrap ``interior`` in side borders with a one-cell inset, clipped to ``width``."""
    inner = max(width - 4, 0)
    body = interior.copy()
    body.truncate(inner, overflow="crop", pad=True)
    line = Text("│ ", style=color)
    line.append_text(body)
    line.append(" │", style=color)
    return line


def dashed(width: int, *, inset: int = 2, color: str = PALETTE.border) -> Text:
    """A dashed divider inset from the panel interior."""
    inner = max(width - 4 - 2 * inset, 0)
    line = Text(" " * inset)
    line.append("╌" * inner, style=color)
    return box_row(line, width, color=PALETTE.border)


class NextScreenRequest(Message):
    """Bubbled by a wizard screen asking the app to advance to the next step."""


class PrevScreenRequest(Message):
    """Bubbled by a wizard screen asking the app to return to the previous step."""
