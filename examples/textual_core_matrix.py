"""A runnable Textual prototype for the Archon Search core-selection screen.

Run with:
    uv run --with textual python examples/textual_core_matrix.py
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button, Static

from examples.textual_design import PALETTE, bold, italic


@dataclass(frozen=True)
class CoreChoice:
    """One selectable corpus language or search-profile choice."""

    kind: str
    label: str
    detail: str
    preview_name: str
    meters: str
    line_one: str
    line_two: str
    line_three: str


CHOICES = (
    CoreChoice(
        "language",
        "English only",
        "~330 MB download · 1–1.5 GB RAM",
        "ENGLISH",
        "■■■□□ QUALITY     ■■■■□ SPEED      ■■■□□ FOOTPRINT",
        "Language stack  English models",
        "Balanced profile  ~330 MB · 1–1.5 GB RAM",
        "Next stage       Hardware acceleration check",
    ),
    CoreChoice(
        "language",
        "Multiple languages",
        "~2.1 GB download · 1–1.5 GB RAM + detector",
        "MULTILINGUAL",
        "■■■□□ QUALITY     ■■■□□ SPEED      ■■■■□ COVERAGE",
        "Language stack  Multilingual models + detector",
        "Balanced profile  ~2.1 GB · 1–1.5 GB RAM",
        "Requirement      License confirmation follows",
    ),
    CoreChoice(
        "profile",
        "Minimal",
        "Personal / <10k docs      150 MB         ~0.5 GB    ■■□□□",
        "MINIMAL",
        "■■□□□ QUALITY     ■■■■■ SPEED      ■■■■■ FOOTPRINT",
        "Embedder        bge-small-en-v1.5",
        "Reranker        MiniLM-L6 · Chunk size 512",
        "Download        ~150 MB · Memory ~0.5 GB",
    ),
    CoreChoice(
        "profile",
        "Balanced",
        "Team / 10k–200k docs     ~330 MB        1–1.5 GB  ■■■□□",
        "BALANCED",
        "■■■□□ QUALITY     ■■■■□ SPEED      ■■■□□ FOOTPRINT",
        "Embedder        bge-base-en-v1.5",
        "Reranker        MiniLM-L12 · Chunk size 512",
        "Download        ~330 MB · Memory 1–1.5 GB",
    ),
    CoreChoice(
        "profile",
        "Maximum",
        "Large / 200k+ docs         ~2.3 GB        2.5–3 GB  ■■■■□",
        "MAXIMUM",
        "■■■■□ QUALITY     ■■□□□ SPEED      ■■□□□ FOOTPRINT",
        "Embedder        bge-large-en-v1.5",
        "Reranker        BGE reranker · Chunk size 1024",
        "Download        ~2.3 GB · Memory 2.5–3 GB",
    ),
)

PROFILE_COLUMNS = (14, 31, 16, 16, 8)
PROFILE_HEADINGS = ("Profile", "Best for", "Download", "Memory", "Quality")
class CoreSelector(Static, can_focus=True):
    """Terminal-native cursor and selection control for the two core decisions."""

    BINDINGS = [
        Binding("up", "cursor_up", "Move up"),
        Binding("down", "cursor_down", "Move down"),
        Binding("space,enter", "select", "Select"),
    ]

    class Changed(Message):
        """Posted whenever the focused or committed core configuration changes."""

        def __init__(self, selector: CoreSelector, committed: bool) -> None:
            super().__init__()
            self.selector = selector
            self.committed = committed

    def __init__(self) -> None:
        super().__init__()
        self.cursor = 0
        self.language_index = 0
        self.profile_index = 3
        self.cursor_visible = True

    @property
    def focused_choice(self) -> CoreChoice:
        return CHOICES[self.cursor]

    def action_cursor_up(self) -> None:
        if self.cursor == 0:
            self._focus_navigation("previous")
            return
        self.cursor_visible = True
        self.cursor -= 1
        self._notify_changed(committed=False)

    def action_cursor_down(self) -> None:
        if self.cursor == len(CHOICES) - 1:
            self._focus_navigation("next")
            return
        self.cursor_visible = True
        self.cursor += 1
        self._notify_changed(committed=False)

    def action_select(self) -> None:
        if self.focused_choice.kind == "language":
            self.language_index = self.cursor
        else:
            self.profile_index = self.cursor
        self._notify_changed(committed=True)

    def _notify_changed(self, *, committed: bool) -> None:
        self.refresh()
        self.post_message(self.Changed(self, committed))


    def _focus_navigation(self, button_id: str) -> None:
        self.cursor_visible = False
        self.refresh()
        self.app.query_one(f"#{button_id}", NavigationButton).focus()

    @property
    def _width(self) -> int:
        """Use the allocated terminal width; no virtual canvas width is imposed."""
        return max(3, self.size.width)

    @property
    def _frame_padding(self) -> str:
        """Inset frame content where the allocated width can accommodate it."""
        return " " if self._width >= 5 else ""

    @property
    def _content_width(self) -> int:
        return self._width - 2 - 2 * len(self._frame_padding)

    def _top_edge(self, label: str) -> str:
        interior = self._width - 2
        title = f"─ {label} "
        return f"┌{title[:interior].ljust(interior, '─')}┐\n"

    def _content_line(self, content: str) -> Text:
        padding = self._frame_padding
        interior = self._content_width
        return Text(f"│{padding}{content[:interior].ljust(interior)}{padding}│\n", style=PALETTE.accent)

    def _choice_line(self, choice: CoreChoice, index: int) -> Text:
        selected = self.language_index == index if choice.kind == "language" else self.profile_index == index
        marker = "[●]" if selected else "[ ]"
        cursor = "▶ " if self.cursor_visible and self.cursor == index else "  "
        label_width = 20 if choice.kind == "language" else 14
        if choice.kind == "profile":
            parts = re.split(r" {2,}", choice.detail)
            values = (f"{cursor}{marker} {choice.label}", *parts)
            row = " ".join(value[:width].ljust(width) for value, width in zip(values, PROFILE_COLUMNS))
        else:
            row = f"{cursor}{marker} {choice.label:<{label_width}} {choice.detail}"
        padding = self._frame_padding
        interior = self._content_width
        style = (
            f"bold {PALETTE.accent_foreground} on {PALETTE.accent}"
            if self.cursor_visible and self.cursor == index
            else f"bold {PALETTE.confirmed}"
            if selected
            else PALETTE.muted
        )
        text = Text(f"│{padding}", style=PALETTE.accent)
        visible_row = row[:interior].ljust(interior)
        text.append(visible_row, style=style)
        for position, glyph in enumerate(visible_row):
            if glyph in "■□":
                start = 1 + len(padding) + position
                text.stylize(PALETTE.meter, start, start + 1)
        text.append(f"{padding}│\n", style=PALETTE.accent)
        return text

    def _info_choice(self, kind: str) -> CoreChoice:
        if self.cursor_visible and self.focused_choice.kind == kind:
            return self.focused_choice
        index = self.language_index if kind == "language" else self.profile_index
        return CHOICES[index]

    def _info_rows(self, choice: CoreChoice) -> tuple[tuple[str, str], ...]:
        if choice.kind == "language":
            details = (choice.line_one, choice.line_two, choice.line_three)
        else:
            details = (choice.line_one, choice.line_two, choice.line_three)
        rows: list[tuple[str, str]] = []
        for detail in details:
            if isinstance(detail, tuple):
                rows.append(detail)
                continue
            label, separator, value = detail.partition("  ")
            rows.append((label, value.lstrip() if separator else ""))
        return tuple(rows)

    def _info_line(self, label: str, value: str) -> Text:
        label_width = 16
        label_text = label[:label_width].ljust(label_width)
        line = self._content_line(f"{label_text} {value}")
        start = 1 + len(self._frame_padding)
        line.stylize(PALETTE.stack_label, start, start + len(label_text))
        if value:
            value_start = start + len(label_text) + 1
            line.stylize(PALETTE.stack_value, value_start, value_start + len(value))
        return line

    def _info_text(self, kind: str) -> Text:
        choice = self._info_choice(kind)
        text = self._content_line(f"{choice.label.upper()} // INFO")
        info_start = 1 + len(self._frame_padding)
        text.stylize(f"bold {PALETTE.confirmed}", info_start, info_start + len(choice.label) + len(" // INFO"))
        for label, value in self._info_rows(choice):
            text += self._info_line(label, value)
        return text

    def render(self) -> Text:
        text = Text(self._top_edge("YOUR CORPUS"), style=f"bold {PALETTE.accent}")
        text += self._content_line("")
        text += self._content_line("What languages will you search?")
        text += self._choice_line(CHOICES[0], 0)
        text += self._choice_line(CHOICES[1], 1)
        text += self._info_text("language")
        text.append(f"└{'─' * (self._width - 2)}┘\n", style=f"bold {PALETTE.accent}")
        text.append(self._top_edge("SELECT SEARCH CORE"), style=f"bold {PALETTE.accent}")
        text += self._content_line("")
        text += self._content_line(
            " ".join(value.ljust(width) for value, width in zip(PROFILE_HEADINGS, PROFILE_COLUMNS))
        )
        text += self._choice_line(CHOICES[2], 2)
        text += self._choice_line(CHOICES[3], 3)
        text += self._choice_line(CHOICES[4], 4)
        text += self._info_text("profile")
        text.append(f"└{'─' * (self._width - 2)}┘", style=f"bold {PALETTE.accent}")
        return text


class NavigationButton(Button):
    """Bottom-bar button with horizontal sibling navigation and vertical return."""

    BINDINGS = [
        Binding("space,enter", "activate", "Activate"),
        Binding("left", "focus_previous", "Previous"),
        Binding("right", "focus_next", "Next"),
        Binding("up,down", "return_to_selector", "Return to options"),
    ]

    def action_activate(self) -> None:
        self.press()

    def action_focus_previous(self) -> None:
        self.app.query_one("#previous", NavigationButton).focus()

    def action_focus_next(self) -> None:
        self.app.query_one("#next", NavigationButton).focus()

    def action_return_to_selector(self) -> None:
        selector = self.app.query_one(CoreSelector)
        selector.cursor = 0 if self.id == "previous" else len(CHOICES) - 1
        selector.cursor_visible = True
        selector.focus()
        selector._notify_changed(committed=False)


class CoreMatrixApp(App[None]):
    """Live, non-production prototype of the first Archon Search wizard screen."""

    ALLOW_SELECT = True

    CSS = """
    Screen {
        background: #181818;
        color: #ffffff;
    }

    #masthead, #status, CoreSelector, #navigation {
        width: 100%;
        margin: 0 2;
    }

    #masthead {
        color: #7fa8d7;
        text-style: bold;
        height: 1;
    }

    #status {
        color: #e89e63;
        height: 1;
    }

    CoreSelector {
        height: 21;
        margin: 0 2;
    }

    #navigation {
        height: 1;
        margin: 0 2;
        align: left middle;
    }

    #nav-spacer {
        width: 1fr;
    }

    Button {
        background: #181818;
        border: none;
        color: #7fa8d7;
        height: 1;
        min-width: 16;
    }

    Button:focus {
        background: #7fa8d7;
        color: #181818;
        text-style: bold;
    }

    """

    BINDINGS = [Binding("q", "quit", "Quit"), Binding("escape", "quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self.selector = CoreSelector()
        self.status_text = "CORE MATRIX LINKED"
        self._signal_frames = ("◌", "◔", "◑", "◕", "●")
        self._signal_index = 0
        self._tick_count = 0
        self._boost_ticks = 0
        self._ui_active = False


    def _masthead_text(self) -> Text:
        masthead = bold("◇ ARCHON SEARCH", color=PALETTE.accent)
        masthead.append(
            f"   {self._signal_frames[self._signal_index]} CORE MATRIX LINKED",
            style=f"bold {PALETTE.meter}",
        )
        masthead.append("   SETUP // 01 OF 05", style=PALETTE.muted)
        return masthead

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._masthead_text(), id="masthead")
            yield Static(italic(self.status_text, color=PALETTE.meter), id="status")
            yield self.selector
            with Horizontal(id="navigation"):
                yield NavigationButton("[ ◀ PREVIOUS ]", id="previous")
                yield Static("", id="nav-spacer")
                yield NavigationButton("[ NEXT ▶ ]", id="next")

    def on_mount(self) -> None:
        self._ui_active = True
        self.selector.focus()
        self.set_interval(0.1, self._tick_signal)

    def on_unmount(self) -> None:
        self._ui_active = False

    def on_core_selector_changed(self, message: CoreSelector.Changed) -> None:
        if message.committed:
            self.status_text = f"{message.selector.focused_choice.label.upper()} EQUIPPED // CORE MATRIX LINKED"
            self._boost_ticks = 12
        self._refresh_status()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "previous":
            self.status_text = "FIRST SCREEN // NO PREVIOUS STAGE"
        elif event.button.id == "next":
            self.status_text = "NEXT STAGE ARMED // HARDWARE CHECK"
            self._boost_ticks = 12
        self.query_one("#status", Static).update(italic(self.status_text, color=PALETTE.meter))

    def _refresh_status(self) -> None:
        self.query_one("#status", Static).update(italic(self.status_text, color=PALETTE.meter))

    def _tick_signal(self) -> None:
        if not self._ui_active:
            return
        self._tick_count += 1
        if self._boost_ticks or self._tick_count % 3 == 0:
            self._signal_index = (self._signal_index + 1) % len(self._signal_frames)
            try:
                masthead = self.query_one("#masthead", Static)
            except NoMatches:
                return
            masthead.update(self._masthead_text())
        if self._boost_ticks:
            self._boost_ticks -= 1


if __name__ == "__main__":
    CoreMatrixApp().run()
