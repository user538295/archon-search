"""Core matrix — step 02 of the Archon Search setup wizard, and the wizard app.

Pick the corpus (what you search) and the search core (embedder/reranker profile).
A live telemetry sparkline projects engine load; gauge bars sweep whenever the
highlighted configuration changes. Mirrors ``Core Step.dc.html``.

Run the whole wizard from the repo root with:
    uv run --with textual python -m examples.textual_core_matrix
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

from examples.textual_design import (
    BRAILLE,
    PALETTE,
    NextScreenRequest,
    PrevScreenRequest,
    box_bottom,
    box_row,
    box_top,
    dashed,
    gauge_color,
    spread,
    stack,
)


@dataclass(frozen=True)
class Corpus:
    label: str
    meta: str
    stack: str
    dl: str
    mem: str
    note: str


@dataclass(frozen=True)
class Profile:
    name: str
    best: str
    dl: str
    mem: str
    emb: str
    rer: str
    chunk: str
    lat: str
    retr: int
    speed: int
    foot: int


CORPUS = (
    Corpus("English only", "~330 MB", "English models", "~330 MB", "1–1.5 GB",
           "› Single-language pipeline · fastest indexing and lowest overhead."),
    Corpus("Multiple languages", "~2.1 GB", "Multilingual + language detector", "~2.1 GB", "1–1.5 GB + detector",
           "› Cross-lingual recall · adds a language-detection pass per query."),
)

PROFILES = (
    Profile("Minimal", "Personal · <10k docs", "150 MB", "~0.5 GB", "bge-small-en-v1.5", "— none —", "384 tok", "~90 ms", 4, 10, 2),
    Profile("Balanced", "Team · 10k–200k docs", "~330 MB", "1–1.5 GB", "bge-base-en-v1.5", "MiniLM-L12", "512 tok", "~150 ms", 7, 6, 5),
    Profile("Maximum", "Large · 200k+ docs", "~2.3 GB", "2.5–3 GB", "bge-large-en-v1.5", "bge-reranker-large", "768 tok", "~320 ms", 10, 2, 9),
)

STEP_LABELS = ("CALIBRATE", "CORE", "CAPABILITIES", "REVIEW", "INSTALL", "ONLINE")
NAV = 7            # 0,1 corpus · 2,3,4 profiles · 5 BACK · 6 NEXT
SPARK_WIDTH = 26
NAV_BACK = 5
NAV_NEXT = 6

BEST_WIDTH = 22
DL_WIDTH = 11
MEM_WIDTH = 12


class CoreView(Static, can_focus=True):
    """The core-matrix panel. Owns telemetry + gauge animation and all rendering."""

    def __init__(self) -> None:
        super().__init__()
        self.cur = 3               # start on the committed "Balanced" profile
        self.sel_corpus = 0
        self.sel_profile = 1
        self.sweep_corpus = 10     # 0..10 corpus-info reveal (10 = settled)
        self.sweep_profile = 10    # 0..10 profile-info + gauge reveal (10 = settled)
        self.load = 27
        self.spark: list[int] = []
        self.lock: str | None = None
        self.blink = True
        self._lock_timer = None

    # --- lifecycle ---------------------------------------------------------
    def on_mount(self) -> None:
        base = self._base_load()
        self.spark = [max(0, min(8, round(base + random.random() * 1.6 - 0.8))) for _ in range(SPARK_WIDTH)]
        self.set_interval(0.12, self._telemetry)
        self.set_interval(0.034, self._sweep_tick)
        self.set_interval(0.5, self._toggle_blink)

    def _telemetry(self) -> None:
        recomputing = self.sweep_corpus < 10 or self.sweep_profile < 10
        value = self._base_load() + (random.random() * 2.4 - 1.2) + (2.6 if recomputing else 0)
        value = max(0, min(8, round(value)))
        self.spark = (self.spark + [value])[-SPARK_WIDTH:]
        self.load = round(sum(self.spark) / (len(self.spark) * 8) * 88 + 6)
        self.refresh()

    def _sweep_tick(self) -> None:
        changed = False
        if self.sweep_corpus < 10:
            self.sweep_corpus += 1
            changed = True
        if self.sweep_profile < 10:
            self.sweep_profile += 1
            changed = True
        if changed:
            self.refresh()

    def _toggle_blink(self) -> None:
        self.blink = not self.blink
        self.refresh()

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    # --- input -------------------------------------------------------------
    def on_key(self, event) -> None:
        key = event.key
        if key in ("up", "left"):
            self._set_cur((self.cur - 1) % NAV)
        elif key in ("down", "right"):
            self._set_cur((self.cur + 1) % NAV)
        elif key in ("space", "enter"):
            self._activate()
        else:
            return
        event.stop()
        event.prevent_default()

    def _set_cur(self, target: int) -> None:
        before_corpus, before_profile = self._corpus_idx(), self._profile_idx()
        self.cur = target
        # Re-animate only the section whose highlighted item actually changed.
        if self._corpus_idx() != before_corpus:
            self._start_sweep("corpus")
        if self._profile_idx() != before_profile:
            self._start_sweep("profile")
        self.refresh()

    def _activate(self) -> None:
        if self.cur <= 1:
            self.sel_corpus = self.cur
            self._flash("corpus")
            self._start_sweep("corpus")
        elif self.cur <= 4:
            self.sel_profile = self.cur - 2
            self._flash("profile")
            self._start_sweep("profile")
        elif self.cur == NAV_BACK:
            self.post_message(PrevScreenRequest())
        # ponytail: NAV_NEXT (step 03 CAPABILITIES) is not built in this prototype — inert on purpose.
        self.refresh()

    def _start_sweep(self, which: str) -> None:
        # Reset one section's clock; the always-on _sweep_tick refills it.
        if which == "corpus":
            self.sweep_corpus = 0
        else:
            self.sweep_profile = 0

    def _flash(self, which: str) -> None:
        self.lock = which
        if self._lock_timer is not None:
            self._lock_timer.stop()
        self._lock_timer = self.set_timer(0.9, self._clear_lock)

    def _clear_lock(self) -> None:
        self.lock = None
        self.refresh()

    # --- derived state -----------------------------------------------------
    def _profile_idx(self) -> int:
        return self.cur - 2 if 2 <= self.cur <= 4 else self.sel_profile

    def _corpus_idx(self) -> int:
        return self.cur if self.cur <= 1 else self.sel_corpus

    def _base_load(self) -> float:
        foot = PROFILES[self._profile_idx()].foot
        return 1.2 + foot * 0.5 + (0.9 if self._corpus_idx() == 1 else 0)

    def _row_styles(self, is_cur: bool, is_sel: bool) -> tuple[str, str]:
        if is_cur:
            return f"bold {PALETTE.on_accent} on {PALETTE.accent}", f"{PALETTE.on_accent} on {PALETTE.accent}"
        return (PALETTE.text if is_sel else PALETTE.dim), PALETTE.faint

    def _bar(self, value: int, sweep: int) -> list[tuple[str, str]]:
        cells: list[tuple[str, str]] = []
        for i in range(10):
            revealed = i < sweep
            on = i < value
            if not revealed:
                cells.append(("░", PALETTE.cell_hidden))
            elif on and i == sweep - 1 and sweep < 10:
                cells.append(("█", PALETTE.text))  # charging leading edge
            elif on:
                cells.append(("█", gauge_color(i)))
            else:
                cells.append(("░", PALETTE.border))
        return cells

    # --- rendering ---------------------------------------------------------
    def _sparkline(self) -> Text:
        """Live load sparkline; each sample is a braille dot-fill glyph (0..4 rows)."""
        line = Text()
        for value in self.spark:
            level = max(0, min(4, round(value / 2)))  # 0..8 sample -> 0..4 dot-rows
            colour = PALETTE.red if value >= 7 else PALETTE.yellow if value >= 5 else PALETTE.green
            line.append(BRAILLE[level], style=colour)
        return line

    def _header(self, width: int) -> Text:
        left = Text("◆ ARCHON SEARCH", style=f"bold {PALETTE.accent}")
        left.append(" :: ", style=PALETTE.border)
        left.append("◉ CORE MATRIX LINKED", style=PALETTE.orange)
        right = Text("EST LOAD ", style=PALETTE.cyan)
        right.append_text(self._sparkline())
        right.append(f" {self.load}%", style=PALETTE.cyan)
        right.append(" │ ", style=PALETTE.border)
        right.append("SETUP 02/06", style=PALETTE.dim)
        return spread(left, right, width)

    def _breadcrumb(self, width: int) -> Text:
        line = Text()
        for index, label in enumerate(STEP_LABELS):
            if index == 0:
                line.append(f"● {label}", style=PALETTE.green)
            elif index == 1:
                line.append(f"◆ {label}", style=f"bold {PALETTE.accent}")
            else:
                line.append(f"◇ {label}", style=PALETTE.faint)
            if index < len(STEP_LABELS) - 1:
                line.append(" ─── ", style=PALETTE.border)
        line.truncate(max(width, 1), overflow="crop", pad=True)
        return line

    def _corpus_row(self, index: int, width: int) -> Text:
        corpus = CORPUS[index]
        is_cur, is_sel = self.cur == index, self.sel_corpus == index
        main, sub = self._row_styles(is_cur, is_sel)
        row = Text()
        row.append(f"{'›' if is_cur else ' '} ", style=main)
        row.append(f"{'(●)' if is_sel else '( )'} ", style=main)
        row.append(corpus.label, style=main)
        inner = max(width - 4, 0)
        fill = inner - row.cell_len - len(corpus.meta)
        row.append(" " * max(fill, 1), style=main)
        row.append(corpus.meta, style=sub)
        return box_row(row, width, color=PALETTE.border)

    def _profile_row(self, index: int, width: int) -> Text:
        profile = PROFILES[index]
        is_cur, is_sel = self.cur == index + 2, self.sel_profile == index
        main, sub = self._row_styles(is_cur, is_sel)
        row = Text()
        row.append(f"{'›' if is_cur else ' '} ", style=main)
        row.append(f"{'(●)' if is_sel else '( )'} ", style=main)
        row.append(profile.name, style=main)
        right = Text(profile.best[:BEST_WIDTH].ljust(BEST_WIDTH), style=sub)
        right.append(f" {profile.dl.rjust(DL_WIDTH)} {profile.mem.rjust(MEM_WIDTH)}", style=sub)
        inner = max(width - 4, 0)
        fill = inner - row.cell_len - right.cell_len
        row.append(" " * max(fill, 1), style=main)
        row.append_text(right)
        return box_row(row, width, color=PALETTE.border)

    def _profile_head(self, width: int) -> Text:
        head = Text(" " * 6, style=PALETTE.accent)  # marker + radio columns
        head.append("Profile", style=PALETTE.accent)
        right = "Best for".ljust(BEST_WIDTH) + " " + "Download".rjust(DL_WIDTH) + " " + "Memory".rjust(MEM_WIDTH)
        inner = max(width - 4, 0)
        head.append(" " * max(inner - head.cell_len - len(right), 1))
        head.append(right, style=PALETTE.accent)
        return box_row(head, width, color=PALETTE.border)

    def _info_name(self, name: str, width: int) -> Text:
        return box_row(Text(f"{name.upper()} // INFO", style=f"bold {PALETTE.orange}"), width, color=PALETTE.border)

    def _kv_line(self, pairs: tuple[tuple[str, str], ...], width: int, computing: bool) -> Text:
        value_style = f"bold {PALETTE.accent}" if computing else PALETTE.text
        line = Text()
        for index, (key, value) in enumerate(pairs):
            if index:
                line.append("    ")
            line.append(f"{key} ", style=PALETTE.faint)
            line.append(value, style=value_style)
        return box_row(line, width, color=PALETTE.border)

    def _gauge_line(self, label: str, value: int, width: int) -> Text:
        line = Text(label.ljust(12), style=PALETTE.faint)
        for glyph, colour in self._bar(value, self.sweep_profile):
            line.append(glyph, style=colour)
        return box_row(line, width, color=PALETTE.border)

    def _corpus_box(self, width: int) -> list[Text]:
        computing = self.sweep_corpus < 10
        corpus = CORPUS[self._corpus_idx()]
        lock = "● LOCKED" if self.lock == "corpus" else ""
        lines = [box_top("CORPUS", width, chip=lock, chip_style=f"bold {PALETTE.orange}")]
        lines.append(box_row(Text("What will you search?", style=PALETTE.dim), width, color=PALETTE.border))
        lines.append(self._corpus_row(0, width))
        lines.append(self._corpus_row(1, width))
        lines.append(dashed(width))
        lines.append(self._info_name(corpus.label, width))
        lines.append(self._kv_line((("Language stack", corpus.stack), ("Download", corpus.dl), ("Peak memory", corpus.mem)), width, computing))
        lines.append(box_row(Text(corpus.note, style=PALETTE.dim), width, color=PALETTE.border))
        lines.append(box_bottom(width))
        return lines

    def _core_box(self, width: int) -> list[Text]:
        computing = self.sweep_profile < 10
        profile = PROFILES[self._profile_idx()]
        lock = "● LOCKED" if self.lock == "profile" else ""
        lines = [box_top("SEARCH CORE", width, chip=lock, chip_style=f"bold {PALETTE.orange}")]
        lines.append(self._profile_head(width))
        for index in range(len(PROFILES)):
            lines.append(self._profile_row(index, width))
        lines.append(dashed(width))
        lines.append(self._info_name(profile.name, width))
        lines.append(self._kv_line(
            (("Embedder", profile.emb), ("Reranker", profile.rer), ("Chunk size", profile.chunk), ("CPU query", f"{profile.lat} est.")),
            width, computing,
        ))
        lines.append(self._gauge_line("RETRIEVAL", profile.retr, width))
        lines.append(self._gauge_line("SPEED", profile.speed, width))
        lines.append(self._gauge_line("FOOTPRINT", profile.foot, width))
        lines.append(box_row(Text("› Estimates rescaled by the device factor measured in CALIBRATE (step 01).", style=PALETTE.faint), width, color=PALETTE.border))
        lines.append(box_bottom(width))
        return lines

    def _command_bar(self, width: int) -> list[Text]:
        left = Text("archon> ", style=f"bold {PALETTE.accent}")
        left.append("configure core ", style=PALETTE.dim)
        left.append("· ", style=PALETTE.border)
        left.append("corpus=", style=PALETTE.dim)
        left.append(CORPUS[self.sel_corpus].label, style=PALETTE.text)
        left.append(" core=", style=PALETTE.dim)
        left.append(PROFILES[self.sel_profile].name, style=PALETTE.text)

        def nav(text: str, is_cur: bool, accent: str) -> Text:
            style = f"bold {PALETTE.on_accent} on {PALETTE.accent}" if is_cur else accent
            return Text(text, style=style)

        right = Text("↑↓←→ · ⏎ select  ", style=PALETTE.faint)
        right.append_text(nav("[ ◀ BACK ]", self.cur == NAV_BACK, PALETTE.faint))
        right.append("  ")
        right.append_text(nav("[ NEXT ▶ ]", self.cur == NAV_NEXT, PALETTE.orange))
        right.append("  ")
        if self.has_focus:
            right.append("● LIVE ", style=PALETTE.green)
        else:
            right.append("◯ CLICK TO FOCUS ", style=PALETTE.faint)
        right.append("█" if self.blink else " ", style=PALETTE.green if self.has_focus else PALETTE.faint)
        return [
            box_top("", width, color=PALETTE.border_dim),
            box_row(spread(left, right, max(width - 4, 1)), width, color=PALETTE.border_dim),
            box_bottom(width, color=PALETTE.border_dim),
        ]

    def render(self) -> Text:
        width = max(self.size.width, 8)
        lines: list[Text] = [self._header(width), Text(), self._breadcrumb(width), Text()]
        lines.extend(self._corpus_box(width))
        lines.append(Text())
        lines.extend(self._core_box(width))
        lines.append(Text())
        lines.extend(self._command_bar(width))
        return stack(lines)


class CoreScreen(Screen):
    """Hosts the core matrix and forwards its BACK request to the wizard."""

    def compose(self) -> ComposeResult:
        yield CoreView()

    def on_mount(self) -> None:
        self.query_one(CoreView).focus()

    def on_prev_screen_request(self, message: PrevScreenRequest) -> None:
        message.stop()
        from examples.textual_calibration import CalibrationScreen

        self.app.switch_screen(CalibrationScreen())


class WizardApp(App[None]):
    """The two-step Archon Search setup wizard: calibrate → core matrix."""

    ALLOW_SELECT = True

    CSS = """
    Screen { background: #141414; color: #ECEFF0; }
    CalibrationView, CoreView { width: 100%; height: auto; padding: 1 2; }
    """

    BINDINGS = [Binding("q", "quit", "Quit"), Binding("escape", "quit", "Quit")]

    def on_mount(self) -> None:
        from examples.textual_calibration import CalibrationScreen

        self.push_screen(CalibrationScreen())


if __name__ == "__main__":
    WizardApp().run()
