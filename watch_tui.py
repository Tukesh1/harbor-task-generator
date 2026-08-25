#!/usr/bin/env python3
"""TB3 pipeline monitor — SCROLLABLE, FULL-SCREENABLE Textual TUI.

Same data as `watch_run.py` (it imports that file's readers/builders directly),
but the panes are interactive:

  • Tab / Shift-Tab  — move focus between the builder / reviewer / harbor panes
  • Enter            — FULL-SCREEN the focused pane; Enter again or Esc to go back
  • ↑/↓ or j/k       — scroll a line · PgUp/PgDn or Ctrl-u/Ctrl-d — page
  • g / G            — top / bottom · mouse wheel works too

Log panes soft-wrap, so long lines wrap instead of running off-screen.

Usage:
  python watch_tui.py [slug] [--stage stage2]

Design note: all the reading logic (state.json, logs, harbor dirs) lives in
watch_run.py on purpose — one data layer, two frontends. Don't duplicate it here.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import watch_run as W  # data layer (read_state, log readers, panel/content builders)

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, RichLog, Static

REFRESH_SEC = float(getattr(W, "REFRESH_HZ", 1.0)) or 1.0


class LogPane(RichLog):
    """A scrollable log view that appends only NEW lines from the log file each
    refresh, preserving the reader's scroll position. It chases the tail only
    while you're at the bottom — scroll up to pause, scroll back down to resume."""

    def __init__(self, run_dir: Path, stage: str, reviewer: bool, **kw) -> None:
        super().__init__(wrap=True, highlight=False, markup=False, auto_scroll=True, **kw)
        self.run_dir = run_dir
        self.stage = stage
        self.reviewer = reviewer
        self._written = 0

    def _at_bottom(self) -> bool:
        try:
            return self.scroll_offset.y >= self.max_scroll_y - 1
        except Exception:
            return True

    def refresh_log(self, stage: str) -> None:
        """Append only the lines that appeared since our last read. Handles
        file shrinkage (a --fresh restart truncates the log) by clearing."""
        self.stage = stage
        try:
            lines = W.read_log_lines(W.log_path(self.run_dir, stage, self.reviewer))
        except Exception:
            return
        if len(lines) < self._written:        # file shrank/rotated (e.g. --fresh) → reset
            self.clear()
            self._written = 0
        new = lines[self._written:]
        if not new:
            return
        self.auto_scroll = self._at_bottom()   # only chase the tail if already at the bottom
        for ln in new:
            try:
                t = W._log_line_to_rich(ln)
                # _log_line_to_rich sets no_wrap=True (for watch_run's truncating Live tail);
                # in a scrollable pane we want soft-wrap so nothing runs off-screen.
                t.no_wrap = False
                t.overflow = "fold"
                self.write(t)
            except Exception:
                self.write(ln)
        self._written = len(lines)


class WatchApp(App):
    """The TUI app: header + three focusable panes (builder / reviewer /
    harbor), refreshed on a timer, with Enter-to-fullscreen per pane."""

    TITLE = "TB3 monitor"
    CSS = """
    #hdr        { height: auto; }
    #harbor     { height: auto; max-height: 14; border: round $accent; }
    #harborbody { height: auto; }
    #builder    { height: 2fr; border: round $accent; }
    #reviewer   { height: 1fr; border: round $accent; }
    RichLog:focus, VerticalScroll:focus { border: round $secondary; }
    /* full-screened pane fills all remaining space (overrides the id heights) */
    .-maxed     { height: 1fr !important; max-height: 100% !important; }
    """
    BINDINGS = [
        Binding("enter", "toggle_maximize", "fullscreen"),
        Binding("escape", "minimize", "back"),
        Binding("tab", "focus_next", "pane →"),
        Binding("shift+tab", "focus_previous", "← pane"),
        Binding("j", "line_down", "↓"),
        Binding("k", "line_up", "↑"),
        Binding("g", "go_top", "top"),
        Binding("G", "go_bottom", "bottom"),
        Binding("ctrl+d", "page_down", "pgdn"),
        Binding("ctrl+u", "page_up", "pgup"),
        Binding("r", "force_refresh", "refresh"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, run_dir: Path, stage: str, fixed_stage: bool) -> None:
        super().__init__()
        self.run_dir = run_dir
        self.stage = stage
        self.fixed_stage = fixed_stage
        self._maxed = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="hdr")
        self.builder = LogPane(self.run_dir, self.stage, False, id="builder")
        self.builder.border_title = "builder log  (Enter=fullscreen · Tab=switch · jk/↑↓/PgUp-Dn/gG/mouse)"
        self.reviewer = LogPane(self.run_dir, self.stage, True, id="reviewer")
        self.reviewer.border_title = "reviewer log  (Enter=fullscreen)"
        self.harbor = VerticalScroll(id="harbor", can_focus=True)
        self.harbor.border_title = "harbor / trials  (Enter=fullscreen)"
        self.harborbody = Static(id="harborbody")
        yield self.builder
        yield self.reviewer
        with self.harbor:
            yield self.harborbody
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_all()
        self.builder.focus()
        self.set_interval(REFRESH_SEC, self.refresh_all)

    @property
    def _panels(self) -> tuple:
        return (self.builder, self.reviewer, self.harbor)

    def refresh_all(self) -> None:
        try:
            state = W.read_state(self.run_dir)
        except Exception:
            return
        if not self.fixed_stage:
            self.stage = W.active_stage(state.get("stages", {})) or self.stage
        try:
            self.query_one("#hdr", Static).update(W.make_header(self.run_dir, state))
        except Exception:
            pass
        try:
            self.harborbody.update(W.make_harbor_panel(self.run_dir, self.stage))
        except Exception:
            pass
        self.builder.refresh_log(self.stage)
        self.reviewer.refresh_log(self.stage)
        self.sub_title = f"{self.run_dir.name} · {self.stage}" + ("  [FULLSCREEN]" if self._maxed else "")

    # --- focus / scroll helpers ---------------------------------------------
    def _focused_panel(self):
        f = self.focused
        return f if f in self._panels else self.builder

    # --- full-screen a pane --------------------------------------------------
    def action_toggle_maximize(self) -> None:
        target = self._focused_panel()
        if self._maxed is target:
            self.action_minimize()
            return
        for p in self._panels:
            p.display = (p is target)
            p.set_class(p is target, "-maxed")
        target.focus()
        self._maxed = target
        self.refresh_all()

    def action_minimize(self) -> None:
        if self._maxed is None:
            return
        for p in self._panels:
            p.display = True
            p.remove_class("-maxed")
        self._maxed = None
        self.refresh_all()

    # --- scroll the focused pane --------------------------------------------
    def action_line_down(self) -> None: self._focused_panel().scroll_down()
    def action_line_up(self) -> None: self._focused_panel().scroll_up()
    def action_page_down(self) -> None: self._focused_panel().scroll_page_down()
    def action_page_up(self) -> None: self._focused_panel().scroll_page_up()
    def action_go_top(self) -> None: self._focused_panel().scroll_home()
    def action_go_bottom(self) -> None: self._focused_panel().scroll_end()
    def action_force_refresh(self) -> None: self.refresh_all()


def main() -> int:
    """Entry point: resolve the slug (arg or picker), then hand it to the app.
    Pass --stage to pin a stage; otherwise we follow whichever stage is active."""
    ap = argparse.ArgumentParser(description="TB3 pipeline monitor (scrollable, full-screenable TUI)")
    ap.add_argument("slug", nargs="?", default=None)
    ap.add_argument("--stage", default=None, choices=["stage1", "stage2", "stage3"])
    args = ap.parse_args()

    slug = args.slug or W.choose_slug()
    if not slug:
        print("No runs found under", W.RUNS_ROOT)
        return 1
    run_dir = W.RUNS_ROOT / slug
    if not run_dir.exists():
        print(f"Run not found: {run_dir}")
        return 1
    state = W.read_state(run_dir)
    stage = args.stage or W.active_stage(state.get("stages", {})) or "stage1"
    WatchApp(run_dir, stage, fixed_stage=bool(args.stage)).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
