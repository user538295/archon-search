"""A runnable Textual prototype for the Archon Search core-selection screen.

Run with:
    uv run --with textual python examples/textual_core_matrix.py
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Footer, Static

from examples.textual_design import PALETTE, TableColumn, TextTable, TitledFrame, bold, italic, text, underlined


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

    def _top_edge(self, label: str) -> str:
        interior = self._width - 2
        title = f"─ {label} "
        return f"┌{title[:interior].ljust(interior, '─')}┐\n"

    def _content_line(self, content: str) -> Text:
        interior = self._width - 2
        return Text(f"│{content[:interior].ljust(interior)}│\n", style="#56d9ff")

    def _choice_line(self, choice: CoreChoice, index: int) -> Text:
        selected = self.language_index == index if choice.kind == "language" else self.profile_index == index
        marker = "[●]" if selected else "[ ]"
        cursor = "▶ " if self.cursor_visible and self.cursor == index else "  "
        label_width = 20 if choice.kind == "language" else 14
        row = f"{cursor}{marker} {choice.label:<{label_width}} {choice.detail}"
        interior = self._width - 2
        style = "bold black on #56d9ff" if self.cursor_visible and self.cursor == index else ("bold #82eea8" if selected else "#6d9bad")
        text = Text("│", style="#56d9ff")
        text.append(row[:interior].ljust(interior), style=style)
        text.append("│\n", style="#56d9ff")
        return text

    def render(self) -> Text:
        text = Text(self._top_edge("YOUR CORPUS"), style="bold #56d9ff")
        text += self._content_line("  What languages will you search?")
        text += self._choice_line(CHOICES[0], 0)
        text += self._choice_line(CHOICES[1], 1)
        text.append(f"└{'─' * (self._width - 2)}┘\n", style="bold #56d9ff")
        text.append(self._top_edge("SELECT SEARCH CORE"), style="bold #56d9ff")
        text += self._content_line("  Profile        Best for                 Download       Memory     Quality")
        text += self._choice_line(CHOICES[2], 2)
        text += self._choice_line(CHOICES[3], 3)
        text += self._choice_line(CHOICES[4], 4)
        text.append(f"└{'─' * (self._width - 2)}┘", style="bold #56d9ff")
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


class FocusPreview(TitledFrame):
    """Model-stack panel composed from the reusable responsive frame brick."""

    def __init__(self) -> None:
        super().__init__("MODEL STACK // ENGLISH", id="preview")
        self.choice = CHOICES[0]

    def show_choice(self, choice: CoreChoice) -> None:
        self.choice = choice
        self.title = f"MODEL STACK // {choice.preview_name}"
        self.set_lines(
            (
                f"  {choice.meters}",
                f"  {choice.line_one}",
                f"  {choice.line_two}",
                f"  {choice.line_three}",
            )
        )


class CoreMatrixApp(App[None]):
    """Live, non-production prototype of the first Archon Search wizard screen."""

    CSS = """
    Screen {
        background: #07111b;
        color: #d8f6ff;
    }

    #masthead, #status, #preview, CoreSelector, #navigation {
        width: 100%;
        margin: 0 2;
    }

    #masthead {
        color: #56d9ff;
        text-style: bold;
        height: 1;
    }

    #status {
        color: #82eea8;
        height: 1;
    }

    CoreSelector {
        height: 12;
        margin: 0 2;
    }

    #preview {
        height: 6;
        color: #56d9ff;
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
        background: #07111b;
        border: none;
        color: #56d9ff;
        height: 1;
        min-width: 16;
    }

    Button:focus {
        background: #56d9ff;
        color: #04131a;
        text-style: bold;
    }

    Footer {
        background: #0d2230;
        color: #d8f6ff;
    }
    """

    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self.selector = CoreSelector()
        self.preview = FocusPreview()
        self.loadout_table = TextTable(
            (TableColumn("Module"), TableColumn("Loadout", weight=3)),
        )
        self.status_text = "CORE MATRIX LINKED"
        self._signal_frames = ("◌", "◔", "◑", "◕", "●")
        self._signal_index = 0
        self._tick_count = 0
        self._boost_ticks = 0
        self._ui_active = False


    @property
    def preview_text(self) -> str:
        return self.preview.render().plain

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(bold("◇ ARCHON SEARCH"), id="masthead")
            yield Static(italic(self.status_text), id="status")
            yield self.selector
            yield self.preview
            with Horizontal(id="navigation"):
                yield NavigationButton("[ ◀ PREVIOUS ]", id="previous")
                yield Static("", id="nav-spacer")
                yield NavigationButton("[ NEXT ▶ ]", id="next")
            yield Footer()

    def on_mount(self) -> None:
        self._ui_active = True
        self._refresh_preview()
        self.call_after_refresh(self._refresh_preview)
        self.selector.focus()
        self.set_interval(0.1, self._tick_signal)

    def on_unmount(self) -> None:
        self._ui_active = False

    def on_unmount(self) -> None:
        self._ui_active = False

    def on_core_selector_changed(self, message: CoreSelector.Changed) -> None:
        if message.committed:
            self.status_text = f"{message.selector.focused_choice.label.upper()} EQUIPPED // CORE MATRIX LINKED"
            self._boost_ticks = 12
        self._refresh_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "previous":
            self.status_text = "FIRST SCREEN // NO PREVIOUS STAGE"
        elif event.button.id == "next":
            self.status_text = "NEXT STAGE ARMED // HARDWARE CHECK"
            self._boost_ticks = 12
        self.query_one("#status", Static).update(italic(self.status_text))

    def _refresh_preview(self) -> None:
        choice = self.selector.focused_choice
        self.preview.show_choice(choice)
        self.loadout_table.set_rows(
            (
                ("Language stack", choice.preview_name),
                ("Download", choice.line_two.removeprefix("Balanced profile  ")),
                ("Next stage", "Hardware acceleration check"),
            )
        )
        self.query_one("#status", Static).update(italic(self.status_text))

    def _tick_signal(self) -> None:
        if not self._ui_active:
            return
        self._tick_count += 1
        if self._boost_ticks or self._tick_count % 3 == 0:
            self._signal_index = (self._signal_index + 1) % len(self._signal_frames)
            masthead = self.query_one("#masthead", Static)
            masthead.update(bold(f"◇ ARCHON SEARCH   {self._signal_frames[self._signal_index]}   SETUP // 01 OF 05"))
        if self._boost_ticks:
            self._boost_ticks -= 1


if __name__ == "__main__":
    CoreMatrixApp().run()
