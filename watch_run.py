#!/usr/bin/env python3
"""TB3 pipeline monitor — live dashboard built on rich.

Watch any run WITHOUT touching it: everything on screen is read from disk
(state.json + logs + harbor job dirs), so the monitor can sit in another
terminal (or another machine sharing the checkout) and just follow along.

Screen layout while watching:

  ┌─ header (stage statuses + live cost) ────────────────────┐
  ├─ <stage> · builder  (fills available height) ────────────┤
  ├─ <stage> · reviewer (fixed 5 lines) ─────────────────────┤
  ├─ Harbor trials (latest job, per-trial rewards) ──────────┤
  └──────────────────────────────────────────────────────────┘

Usage:
  python watch_run.py                   # pick a live run (menu if several run in parallel)
  python watch_run.py <slug>            # specific run
  python watch_run.py --list            # list all runs + which are live, then exit
  python watch_run.py --log             # streaming tail (plain, no Live)
  python watch_run.py --log --reviewer  # follow reviewer log
  python watch_run.py --lines N         # cap builder lines shown

Note: this file is ALSO the data layer for watch_tui.py (it imports these
helpers directly), so keep the state/log readers here generic and reusable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

RUNS_ROOT = Path(__file__).resolve().parent / "runs"
LOCKS_DIR = RUNS_ROOT / ".locks"     # per-task flock files written by lib/runguard.py

# Reuse the pipeline's SINGLE authoritative harbor-cost reader so the monitor and the
# pipeline never diverge (a divergent second scraper caused earlier cost bugs).
sys.path.insert(0, str(Path(__file__).resolve().parent / "pipeline"))
try:
    from lib.costs import harbor_stage_cost_usd as _lib_harbor_cost
except Exception:
    _lib_harbor_cost = None
REFRESH_HZ = 2
REVIEWER_LINES = 5
HARBOR_TRIAL_LINES = 3

console = Console()

# ── State helpers ─────────────────────────────────────────────────────────────
def read_state(run_dir: Path) -> dict:
    """Read a run's state.json; {} when missing/corrupt — the monitor must
    never crash just because a file is mid-write."""
    p = run_dir / "state.json"
    try: return json.loads(p.read_text()) if p.exists() else {}
    except Exception: return {}

def all_run_dirs() -> list[Path]:
    """Every run dir that has a state.json, most-recently-updated first."""
    if not RUNS_ROOT.exists(): return []
    return sorted(
        (d for d in RUNS_ROOT.iterdir() if d.is_dir() and (d / "state.json").exists()),
        key=lambda d: (d / "state.json").stat().st_mtime, reverse=True,
    )

def describe_run(d: Path) -> str:
    """One-line rich summary of a run for the selector / --list: liveness dot,
    name, current stage + status, and its running cost."""
    stages = read_state(d).get("stages", {})
    cur = active_stage(stages) or "—"
    cur_st = stages.get(cur, {}) if cur in stages else {}
    status = cur_st.get("status", "—")
    ph = f" Ph-{cur_st['phase']} r{cur_st.get('round','?')}" if cur_st.get("phase") else ""
    cost = stage_cost_str(d, cur, cur_st) if cur in stages else ""
    live = "[green]● live[/]" if lock_holder(d.name)[0] else "[dim]○ idle[/]"
    # parentheses, not [brackets] — rich would parse [stage3 ok] as a markup tag and drop it.
    return f"{live}  [bold]{d.name}[/]  ({cur} {status}{ph})" + (f"  [dim]{cost}[/]" if cost else "")


def choose_slug() -> str | None:
    """Pick which run to watch. One live run → use it directly. Several →
    numbered menu. None live → fall back to the most recently updated run
    (so you can still inspect finished runs)."""
    live = running_runs()
    if len(live) == 1:
        return live[0].name
    if len(live) >= 2:
        candidates = live
        header = f"[bold]{len(live)} runs live[/] — select one to watch:"
    else:
        dirs = all_run_dirs()
        if not dirs:
            return None
        if len(dirs) == 1:
            return dirs[0].name
        candidates = dirs
        header = "[dim]No live runs — select a recent run to view:[/]"
    console.print(header)
    for i, d in enumerate(candidates, 1):
        console.print(f"  [bold]{i}[/]  {describe_run(d)}")
    while True:
        try:
            ans = input(f"Run number [1-{len(candidates)}], or slug (Enter = 1): ").strip()
        except EOFError:
            return candidates[0].name
        if not ans:
            return candidates[0].name
        if ans.isdigit() and 1 <= int(ans) <= len(candidates):
            return candidates[int(ans) - 1].name
        if (RUNS_ROOT / ans).is_dir():
            return ans
        console.print("[red]invalid choice — enter a number or a valid slug[/]")

def active_stage(stages: dict) -> str | None:
    """The stage the run is currently on — 'running' wins; else the latest
    stage present in state (stage3 > stage2 > stage1)."""
    for s in ("stage3", "stage2", "stage1"):
        if stages.get(s, {}).get("status") == "running": return s
    for s in ("stage3", "stage2", "stage1"):
        if s in stages: return s
    return None

def lock_holder(slug: str) -> tuple[bool, str]:
    """Per-RUN liveness via the runguard per-task lock (runs/.locks/<slug>.lock).

    The lock file records ``PID=<n> stage=… slug=… since=… cwd=…`` of the process
    working this run. We check that PID is alive (non-intrusively — we never touch the
    flock, so we can't accidentally block a starting run). Returns (is_live, holder_info).
    A stale file whose PID is gone reads as not-live (flock auto-released on death).
    """
    p = LOCKS_DIR / f"{slug}.lock"
    try:
        info = p.read_text(errors="replace").strip()
    except Exception:
        return False, ""
    m = re.search(r"PID=(\d+)", info)
    if not m:
        return False, info
    try:
        os.kill(int(m.group(1)), 0)        # signal 0 = liveness probe, no signal sent
        return True, info
    except ProcessLookupError:
        return False, info                 # holder died → stale lock file
    except PermissionError:
        return True, info                  # alive, just not ours (won't happen, same user)
    except OSError:
        return False, info


def running_runs() -> list[Path]:
    """All runs with a live lock holder, most-recently-updated first."""
    return [d for d in all_run_dirs() if lock_holder(d.name)[0]]
def read_log_lines(path: Path) -> list[str]:
    if not path.exists(): return []
    try: return path.read_text(errors="replace").splitlines()
    except Exception: return []

def log_path(run_dir: Path, stage: str, reviewer: bool = False) -> Path:
    label = "reviewer" if reviewer else "builder"
    return run_dir / "logs" / f"{stage}.{label}.log"

# ── Cost helpers ──────────────────────────────────────────────────────────────
_COST_RE = re.compile(r"turn done \(cost_usd~([\d.e+\-]+)")

def sdk_cost_from_log(path: Path) -> float | None:
    """Latest SDK session cost by scraping the builder/reviewer log.

    `turn done (cost_usd~X ...)` logs the SDK's CUMULATIVE session total each turn
    (X climbs monotonically), so the current cost is the LAST value — NOT the sum.
    Summing the snapshots inflated the figure ~Nx (the old `session_total~` bug).
    """
    if not path.exists(): return None
    latest = None
    try:
        for line in path.read_text(errors="replace").splitlines():
            m = _COST_RE.search(line)
            if m:
                try: latest = float(m.group(1))
                except ValueError: pass
    except Exception: pass
    return round(latest, 4) if latest is not None else None

def harbor_cost_from_jobs(run_dir: Path, stage: str) -> float | None:
    """Live harbor dollars for a stage — delegates to the pipeline's single
    authoritative reader (lib.costs.harbor_stage_cost_usd) so monitor and
    pipeline can never diverge (a divergent second scraper caused real cost
    bugs earlier). Only needed as a fallback before the stage starts writing
    its structured running cost into state.json each round."""
    if _lib_harbor_cost is None:
        return None
    try:
        return _lib_harbor_cost(run_dir / "harbor" / stage)
    except Exception:
        return None

def stage_cost_str(run_dir: Path, sname: str, st: dict) -> str:
    """Compact one-string cost for a stage chip in the header. Prefers the
    structured cost block from state.json; falls back to scraping logs + job
    dirs before the first live write lands."""
    cost_dict = st.get("cost")
    if cost_dict and isinstance(cost_dict, dict):
        t = cost_dict.get("totals", {})
        grand = t.get("grand_total_usd", 0)
        complete = t.get("grand_total_complete", False)
        az = t.get("analyze_unmetered", 0)
        # exact $ when complete; '+' only for a genuine gap; analyze (harbor never prices it)
        # gets an explicit footnote rather than a vague '+'.
        note = "" if complete else "+"
        if az:
            note += f" (+{az} analyze)"
        return f"≈${grand:.2f}{note}"
    bld = sdk_cost_from_log(log_path(run_dir, sname))
    rev = sdk_cost_from_log(log_path(run_dir, sname, reviewer=True))
    hb  = harbor_cost_from_jobs(run_dir, sname)
    if bld is None: return ""
    sdk = bld + (rev or 0.0)
    grand = sdk + (hb or 0.0)
    suffix = "" if hb is not None else "+"
    return f"≈${grand:.2f}{suffix}"

# ── Harbor helpers ────────────────────────────────────────────────────────────
def latest_harbor_job(run_dir: Path, stage: str) -> Path | None:
    """The most recently created job dir under runs/<slug>/harbor/<stage>/."""
    d = run_dir / "harbor" / stage
    if not d.exists(): return None
    jobs = sorted((p for p in d.iterdir() if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return jobs[0] if jobs else None

def find_trial_dirs(job_dir: Path) -> list[Path]:
    """The per-trial dirs inside a harbor job (a trial = a dir holding a
    config.json), in run order."""
    trials = []
    for ts_dir in sorted(p for p in job_dir.iterdir() if p.is_dir()):
        for trial in sorted(p for p in ts_dir.iterdir() if p.is_dir()):
            if (trial / "config.json").exists():
                trials.append(trial)
    return trials

def read_trial_reward(trial_dir: Path) -> float | None:
    """A finished trial's reward, parsed from result.json (falling back to
    verifier/reward.txt). None means the trial hasn't scored yet → shown as
    'running …' on the panel."""
    rp = trial_dir / "result.json"
    if rp.exists():
        try:
            data = json.loads(rp.read_text(errors="replace"))
            vr = data.get("verifier_result") or {}
            rw = vr.get("rewards", {}) if isinstance(vr, dict) else {}
            if isinstance(rw, dict):
                val = rw.get("reward")
                if val is None and rw:
                    nums = [v for v in rw.values() if isinstance(v, (int, float))]
                    val = nums[0] if len(nums) == 1 else None
                if isinstance(val, (int, float)): return float(val)
        except Exception: pass
    rwt = trial_dir / "verifier" / "reward.txt"
    if rwt.exists():
        try: return float(rwt.read_text().strip())
        except Exception: pass
    return None

def trial_log_tail(trial_dir: Path, n: int = 3) -> list[str]:
    log = trial_dir / "trial.log"
    if not log.exists(): return []
    try:
        lines = [l for l in log.read_text(errors="replace").splitlines() if l.strip()]
        return lines[-n:]
    except Exception: return []

# ── Rich text builders ────────────────────────────────────────────────────────
def _log_line_to_rich(line: str) -> Text:
    """Colorize one agent-log line (think/say/tool/turn-done/errors/rewards) —
    shared with watch_tui, so the two monitors always look the same."""
    t = Text(line, no_wrap=True, overflow="fold")
    if "] think:"       in line: t.stylize("magenta")
    elif "] say:"        in line: t.stylize("cyan")
    elif "] tool "       in line: t.stylize("dim")
    elif "(waiting"      in line: t.stylize("dim yellow")
    elif "session started" in line or "session closed" in line: t.stylize("bold")
    elif "turn done"     in line: t.stylize("green")
    elif "ERROR" in line or "Invalid API" in line: t.stylize("bold red")
    elif any(k in line for k in ("oracle=","rewards=","nop=","breaking=")): t.stylize("bold yellow")
    elif "verdict=" in line: t.stylize("magenta")
    return t

def _trial_line_to_rich(line: str) -> Text:
    t = Text(line.rstrip(), no_wrap=True, overflow="ellipsis")
    if "Running command:" in line or "outputs captured" in line or "Skipping" in line:
        t.stylize("dim")
    elif "error" in line.lower(): t.stylize("red")
    elif any(k in line.lower() for k in ("reward","pass","fail")): t.stylize("bold yellow")
    return t

# ── Panel builders ────────────────────────────────────────────────────────────
def make_header(run_dir: Path, state: dict) -> Panel:
    """The top panel: run name + liveness, per-stage status chips (with
    phase/round + cost), and a stage-specific detail line."""
    stages = state.get("stages", {})
    s1, s2, s3 = stages.get("stage1",{}), stages.get("stage2",{}), stages.get("stage3",{})
    cur = active_stage(stages)
    alive = lock_holder(run_dir.name)[0]   # per-RUN liveness (not a global process grep)

    STATUS = {"ok": "[green]ok ✓[/]", "running": "[yellow]running[/]",
              "needs_human": "[bold magenta]needs_human ⚠[/]",
              "error": "[bold red]error ✗[/]"}
    def chip(name, st):
        if not st: return f"[dim]{name}: —[/]"
        s = STATUS.get(st.get("status",""), st.get("status",""))
        extra = ""
        if name == "stage2" and st.get("phase"):
            ph, rnd = st["phase"], st.get("round","?")
            extra = f" Ph-{ph} r{rnd}"
        cost = stage_cost_str(run_dir, name, st)
        cost_str = f"  [dim]{cost}[/]" if cost else ""
        return f"[bold]{name}[/]: {s}{extra}{cost_str}"

    proc = "[green]● live[/]" if alive else "[red]○ stopped[/]"
    title_line = f"[bold]{run_dir.name}[/]  {proc}  [dim]{state.get('created','')}[/]"
    stages_line = "  │  ".join([chip("stage1",s1), chip("stage2",s2), chip("stage3",s3)])

    detail_parts = []
    if cur == "stage2":
        if "rewards" in s2:
            rws = s2["rewards"]
            rw_str = "[" + ", ".join(("[green]0[/]" if r==0 else f"[red]{r}[/]") for r in rws) + "]"
            breaking = "[green]YES[/]" if s2.get("breaking") else "[red]NO[/]"
            detail_parts.append(f"rewards: {rw_str}  breaking: {breaking}")
        if "oracle_reward" in s2:
            o, n = s2["oracle_reward"], s2.get("nop_reward")
            detail_parts.append(f"oracle: [{'green' if o and o>=1.0 else 'red'}]{o}[/]  "
                                 f"nop: [{'red' if n and n>=1.0 else 'green'}]{n}[/]")
    elif cur == "stage1":
        o, n = s1.get("oracle_reward"), s1.get("nop_reward")
        r = s1.get("round","—")
        detail_parts.append(f"round: {r}/6  oracle: {o}  nop: {n}")
    elif cur == "stage3":
        ck = "[green]✓[/]" if s3.get("check_pass") else "[red]✗[/]"
        if s3.get("breaking") is None:   # Phase A (QA) — break re-verify not run yet
            detail_parts.append(f"check: {ck}  [dim](Phase B break re-verify pending)[/]")
        else:
            brk = "[green]YES[/]" if s3.get("breaking") else "[red]NO[/]"
            o, n = s3.get("oracle_reward"), s3.get("nop_reward")
            vd = s3.get("verdict")
            vdc = {"approve": "[green]approve[/]", "revise": "[red]revise[/]"}.get(vd, f"[dim]{vd}[/]")
            detail_parts.append(
                f"check: {ck}  breaking: {brk}  "
                f"oracle: [{'green' if o and o>=1.0 else 'red'}]{o}[/]  "
                f"nop: [{'red' if n and n>=1.0 else 'green'}]{n}[/]  reviewer: {vdc}")

    body = Text.from_markup(title_line + "\n" + stages_line)
    if detail_parts:
        body.append("\n")
        body.append_text(Text.from_markup("  ".join(detail_parts)))
    return Panel(body, padding=(0,1))

def make_builder_panel(run_dir: Path, stage: str, max_lines: int) -> Panel:
    lines = read_log_lines(log_path(run_dir, stage))
    total = len(lines)
    shown = lines[-max_lines:]
    body = Text()
    for i, l in enumerate(shown):
        body.append_text(_log_line_to_rich(l))
        if i < len(shown) - 1: body.append("\n")
    return Panel(body, title=f"[bold]{stage} · builder[/]  [dim]({total} lines)[/]",
                 padding=(0,1))

def make_reviewer_panel(run_dir: Path, stage: str) -> Panel | None:
    if stage == "stage1": return None
    rpath = log_path(run_dir, stage, reviewer=True)
    lines = read_log_lines(rpath)
    if not lines: return Panel(Text("[dim](no reviewer activity yet)[/dim]", no_wrap=True),
                               title=f"[bold]{stage} · reviewer[/]", padding=(0,1))
    total = len(lines)
    shown = lines[-REVIEWER_LINES:]
    body = Text()
    for i, l in enumerate(shown):
        body.append_text(_log_line_to_rich(l))
        if i < len(shown) - 1: body.append("\n")
    return Panel(body, title=f"[bold]{stage} · reviewer[/]  [dim]({total} lines)[/]",
                 padding=(0,1))

_JOB_LABELS = {
    "break":  ("Break trials", "yellow"),
    "oracle": ("Oracle run",   "green"),
    "nop":    ("No-op run",    "blue"),
    "check":  ("Check",        "cyan"),
    "analyze":("Analyze",      "magenta"),
}

def make_harbor_panel(run_dir: Path, stage: str) -> Panel:
    """Per-trial view of the LATEST harbor job in this stage: reward once
    scored, 'running …' before that, plus a couple of log tail lines each."""
    job_dir = latest_harbor_job(run_dir, stage)
    if not job_dir:
        return Panel(Text("[dim]No harbor jobs yet[/dim]", no_wrap=True),
                     title="[bold]Harbor[/]", padding=(0,1))

    # derive run type from job dir name prefix (e.g. "break-20260603-..." → "break")
    job_type = job_dir.name.split("-")[0]
    label, color = _JOB_LABELS.get(job_type, (job_type.capitalize(), "white"))
    panel_title = f"[bold]Harbor[/]  [{color}]{label}[/{color}]  [dim]{job_dir.name}[/dim]"

    trials = find_trial_dirs(job_dir)
    body = Text()
    if not trials:
        body.append_text(Text("[dim]containers starting up …[/dim]"))
    else:
        for i, td in enumerate(trials, 1):
            reward = read_trial_reward(td)
            if reward is not None:
                status = f"[green]done  reward={reward:.4f}[/green]"
            else:
                status = "[yellow]running …[/yellow]"
            body.append_text(Text.from_markup(
                f"  [bold]trial {i}[/]  [dim]{td.name}[/]  {status}"))
            for tl in trial_log_tail(td, HARBOR_TRIAL_LINES):
                body.append("\n    ")
                body.append_text(_trial_line_to_rich(tl))
            if i < len(trials): body.append("\n")
    return Panel(body, title=panel_title, padding=(0,1))

# Panel height constants (content lines, excluding panel border/title which add 2)
HEADER_H   = 4   # title + stages + cost + detail
REVIEWER_H = REVIEWER_LINES
HARBOR_H   = 7   # title + up to 3 trials × 2 lines each

def build_renderable(run_dir: Path, state: dict, stage: str):
    """Assemble the whole screen for one refresh tick. The builder pane gets
    whatever height is left after header/reviewer/harbor take theirs."""
    from rich.layout import Layout

    has_reviewer = (stage != "stage1")

    # Fixed sizes (content + 2 for panel border)
    header_size   = HEADER_H   + 2
    reviewer_size = (REVIEWER_H + 2) if has_reviewer else 0
    harbor_size   = HARBOR_H   + 2

    # Builder gets everything else
    total = console.size.height
    builder_size = max(4, total - header_size - reviewer_size - harbor_size)
    builder_lines = max(3, builder_size - 2)  # minus panel border

    layout = Layout()
    if has_reviewer:
        layout.split_column(
            Layout(make_header(run_dir, state),              name="header",   size=header_size),
            Layout(make_builder_panel(run_dir, stage, builder_lines), name="builder",  size=builder_size),
            Layout(make_reviewer_panel(run_dir, stage),      name="reviewer", size=reviewer_size),
            Layout(make_harbor_panel(run_dir, stage),        name="harbor",   size=harbor_size),
        )
    else:
        layout.split_column(
            Layout(make_header(run_dir, state),              name="header",  size=header_size),
            Layout(make_builder_panel(run_dir, stage, builder_lines), name="builder", size=builder_size),
            Layout(make_harbor_panel(run_dir, stage),        name="harbor",  size=harbor_size),
        )
    return layout

# ── Streaming tail (--log) ────────────────────────────────────────────────────
def follow_log(path: Path, n: int):
    """Plain `tail -f` equivalent with syntax coloring — the --log mode when
    you don't want the full-screen dashboard."""
    if not path.exists():
        print(f"Log not found: {path}"); return
    lines = read_log_lines(path)
    for l in lines[-n:]:
        console.print(_log_line_to_rich(l))
    size = path.stat().st_size
    try:
        while True:
            time.sleep(0.3)
            new_size = path.stat().st_size
            if new_size > size:
                with path.open(errors="replace") as f:
                    f.seek(size); chunk = f.read()
                for l in chunk.splitlines():
                    console.print(_log_line_to_rich(l))
                size = new_size
    except KeyboardInterrupt: print()

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="TB3 pipeline monitor")
    ap.add_argument("slug",       nargs="?", default=None)
    ap.add_argument("--list",     action="store_true", help="list all runs + liveness, then exit")
    ap.add_argument("--log",      action="store_true", help="streaming tail (no Live)")
    ap.add_argument("--reviewer", action="store_true", help="tail reviewer log (with --log)")
    ap.add_argument("--stage",    default=None, choices=["stage1","stage2","stage3"])
    ap.add_argument("--lines",    type=int, default=None,
                    help="max builder lines to show (default: fills terminal)")
    args = ap.parse_args()

    if args.list:
        dirs = all_run_dirs()
        if not dirs:
            print("No runs found under", RUNS_ROOT); sys.exit(1)
        live = running_runs()
        console.print(f"[bold]{len(dirs)} run(s)[/], [green]{len(live)} live[/]:")
        for d in dirs:
            console.print(f"  {describe_run(d)}")
        return

    slug = args.slug or choose_slug()
    if not slug:
        print("No runs found under", RUNS_ROOT); sys.exit(1)

    run_dir = RUNS_ROOT / slug
    if not run_dir.exists():
        print(f"Run not found: {run_dir}"); sys.exit(1)

    state = read_state(run_dir)
    stage = args.stage or active_stage(state.get("stages", {})) or "stage1"

    if args.log:
        follow_log(log_path(run_dir, stage, args.reviewer), args.lines or 50)
        return

    with Live(console=console, refresh_per_second=1/REFRESH_HZ, screen=True) as live:
        try:
            while True:
                state = read_state(run_dir)
                if not args.stage:
                    stage = active_stage(state.get("stages", {})) or stage
                live.update(build_renderable(run_dir, state, stage))
                time.sleep(REFRESH_HZ)
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    main()
