"""Reusable terminal-native design bricks for Archon Search experiments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from rich.text import Text
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static


@dataclass(frozen=True)
class Palette:
    """The exact colour system used by the approved HTML Core Matrix mockup."""

    background: str = "#07111b"
    foreground: str = "#d8f6ff"
    muted: str = "#6d9bad"
    accent: str = "#56d9ff"
    accent_foreground: str = "#04131a"
    confirmed: str = "#82eea8"


PALETTE = Palette()


def text(value: str, *, color: str | None = None) -> Text:
    """Create a plain terminal text atom in the shared foreground colour."""
    return Text(value, style=color or PALETTE.foreground)


def bold(value: str, *, color: str | None = None) -> Text:
    """Create a bold text atom."""
    return Text(value, style=f"bold {color or PALETTE.foreground}")


def italic(value: str, *, color: str | None = None) -> Text:
    """Create an italic text atom."""
    return Text(value, style=f"italic {color or PALETTE.foreground}")


def underlined(value: str, *, color: str | None = None) -> Text:
    """Create an underlined text atom."""
    return Text(value, style=f"underline {color or PALETTE.foreground}")


def meter(value: int, total: int = 5) -> str:
    """Render a clamped square meter suitable for any terminal font."""
    if total < 1:
        raise ValueError("total must be positive")
    value = min(max(value, 0), total)
    return "■" * value + "□" * (total - value)


def frame_lines(title: str, lines: Iterable[str], width: int) -> tuple[str, ...]:
    """Build a responsive UTF-8 frame exactly ``width`` cells wide."""
    width = max(width, 3)
    interior = width - 2
    label = f"─ {title} "
    top = f"┌{label[:interior].ljust(interior, '─')}┐"
    body = tuple(f"│{line[:interior].ljust(interior)}│" for line in lines)
    return (top, *body, f"└{'─' * interior}┘")


class TitledFrame(Static):
    """A responsive UTF-8 panel whose width comes from Textual's layout."""

    def __init__(self, title: str, lines: Iterable[str] = (), *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.title = title
        self.lines = tuple(lines)

    def set_lines(self, lines: Iterable[str]) -> None:
        self.lines = tuple(lines)
        self.refresh()

    def render(self) -> Text:
        width = max(self.size.width, 3)
        return Text("\n".join(frame_lines(self.title, self.lines, width)), style=PALETTE.accent)


@dataclass(frozen=True)
class RadioOption:
    """A single label/detail pair in a reusable terminal radio group."""

    label: str
    detail: str = ""


class RadioGroup(Static, can_focus=True):
    """Keyboard-operated radio group with independent cursor and committed state."""

    class Changed(Message):
        def __init__(self, group: RadioGroup, *, committed: bool) -> None:
            super().__init__()
            self.group = group
            self.committed = committed

    def __init__(self, options: Iterable[RadioOption], selected: int = 0) -> None:
        super().__init__()
        self.options = tuple(options)
        if not self.options:
            raise ValueError("a radio group needs at least one option")
        self.cursor = min(max(selected, 0), len(self.options) - 1)
        self.selected = self.cursor
        self.cursor_visible = True

    def move(self, delta: int) -> bool:
        """Move without wrapping; return whether the cursor changed."""
        target = min(max(self.cursor + delta, 0), len(self.options) - 1)
        if target == self.cursor:
            return False
        self.cursor = target
        self.cursor_visible = True
        self.refresh()
        self.post_message(self.Changed(self, committed=False))
        return True

    def commit(self) -> None:
        self.selected = self.cursor
        self.refresh()
        self.post_message(self.Changed(self, committed=True))

    def hide_cursor(self) -> None:
        self.cursor_visible = False
        self.refresh()

    def show_cursor(self, index: int | None = None) -> None:
        if index is not None:
            self.cursor = min(max(index, 0), len(self.options) - 1)
        self.cursor_visible = True
        self.refresh()

    def render(self) -> Text:
        text = Text()
        for index, option in enumerate(self.options):
            marker = "[●]" if index == self.selected else "[ ]"
            pointer = "▶ " if self.cursor_visible and index == self.cursor else "  "
            content = f"{pointer}{marker} {option.label}  {option.detail}".rstrip()
            style = (
                f"bold {PALETTE.accent_foreground} on {PALETTE.accent}"
                if self.cursor_visible and index == self.cursor
                else f"bold {PALETTE.confirmed}"
                if index == self.selected
                else PALETTE.muted
            )
            text.append(content + "\n", style=style)
        return text


class NavigationBar(Widget):
    """State-only navigation brick for a Previous/Next pair."""

    def __init__(self) -> None:
        super().__init__()
        self.active = "previous"

    def move_horizontal(self, direction: int) -> str:
        if direction:
            self.active = "next" if direction > 0 else "previous"
        return self.active


@dataclass(frozen=True)
class TableColumn:
    """A semantic table column with a proportional share of available width."""

    heading: str
    weight: int = 1
    align: str = "left"


def table_lines(
    columns: tuple[TableColumn, ...], rows: Iterable[tuple[str, ...]], width: int
) -> tuple[str, ...]:
    """Render a clipped, responsive monospaced table with no fixed canvas width."""
    if not columns:
        raise ValueError("a table needs at least one column")
    if any(column.weight < 1 for column in columns):
        raise ValueError("column weights must be positive")
    if any(column.align not in {"left", "right"} for column in columns):
        raise ValueError("column alignment must be left or right")

    width = max(width, len(columns))
    gaps = len(columns) - 1
    available = width - gaps
    total_weight = sum(column.weight for column in columns)
    sizes = [available * column.weight // total_weight for column in columns]
    for index in range(available - sum(sizes)):
        sizes[index % len(sizes)] += 1

    def format_row(values: tuple[str, ...]) -> str:
        cells: list[str] = []
        for index, column in enumerate(columns):
            value = values[index] if index < len(values) else ""
            value = value[: sizes[index]]
            cells.append(value.rjust(sizes[index]) if column.align == "right" else value.ljust(sizes[index]))
        return " ".join(cells)

    return (format_row(tuple(column.heading for column in columns)), *(format_row(row) for row in rows))


class TextTable(Static):
    """Responsive terminal table for metadata rows and loadout readouts."""

    def __init__(self, columns: tuple[TableColumn, ...], rows: Iterable[tuple[str, ...]] = ()) -> None:
        super().__init__()
        self.columns = columns
        self.rows = tuple(rows)

    def set_rows(self, rows: Iterable[tuple[str, ...]]) -> None:
        self.rows = tuple(rows)
        self.refresh()

    def render(self) -> Text:
        return Text("\n".join(table_lines(self.columns, self.rows, max(self.size.width, 3))), style=PALETTE.foreground)
