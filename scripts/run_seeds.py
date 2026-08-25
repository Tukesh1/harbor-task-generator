#!/usr/bin/env python3
"""Batch driver: run the whole task-generation pipeline over rows of a seeds CSV,
unattended, N tasks at a time.

The input is a CSV with a slug column, a domain column and a description column
(exact names are flexible — see SLUG_COLS/DOMAIN_COLS/DESC_COLS below). Each
selected row's description is written out as a proposal .md and pushed through
run_pipeline.py END-TO-END (--gate-mode auto, no human gates) as an ISOLATED
subprocess — so one crashed run can never poison the rest of the batch. A
top-level semaphore keeps --max-parallel runs going at once.

Why an isolated subprocess per seed? Simple — memory leaks, wedged docker
containers, hung API streams: any of these can take down one run, and we want
the batch runner to shrug it off and keep going.

The proposal filename is the slugified Slug column, so the pipeline's run dir
(runs/<slug>/) and its per-task lock line up with the same name everywhere —
including the monitors, which find each run by that name.

Monitor a live batch from another terminal:
    python watch_run.py --list          # every run + which are live (best overview)
    python watch_run.py                 # menu to pick among the live runs
    python watch_tui.py <slug>          # scrollable TUI for one run

Examples:
    python scripts/run_seeds.py --seeds-csv seeds.csv                  # all rows, 2 at a time
    python scripts/run_seeds.py --seeds-csv seeds.csv -j 3 --limit 6   # first 6, 3 at a time
    python scripts/run_seeds.py --seeds-csv seeds.csv --slugs a,b      # specific Slugs, in order
    python scripts/run_seeds.py --seeds-csv seeds.csv --domain Systems # Domain substring match
    python scripts/run_seeds.py --seeds-csv seeds.csv --dry-run        # show selection, run nothing
    python scripts/run_seeds.py --seeds-csv seeds.csv --max-cost 120   # abort any single task over $120
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUN_PIPELINE = REPO / "pipeline" / "run_pipeline.py"
RUNS = REPO / "runs"            # where run_pipeline writes runs/<slug>/state.json (default cfg.runs_dir)

# Reuse the pipeline's OWN teardown — the SAME code a single `python pipeline/stageN.py` uses.
# On SIGINT *or* SIGTERM it SIGTERM→(3s)→SIGKILLs this process's ENTIRE descendant tree via a
# recursive `pgrep -P` (PPID) walk: every run_pipeline child AND their grandchildren — the SDK
# `claude` agents, harbor, docker-exec, shells — even ones that called setsid. So stopping the
# batch tears everything down exactly like stopping one stage script (and `kill <pid>` works too,
# not just terminal Ctrl-C). Best-effort import so the runner still works outside a full checkout.
sys.path.insert(0, str(REPO / "pipeline"))
try:
    from lib import runguard as _runguard
except Exception:
    _runguard = None

# Column-name candidates (first non-empty wins) so the same CSV the other pipeline uses works here.
SLUG_COLS = ["Slug", "slug", "Name", "name"]
DOMAIN_COLS = ["Domain", "domain"]
DESC_COLS = ["description / Seed Form Content", "Seed Form Content", "description", "Description"]

# Live child pipelines, so a Ctrl-C in this launcher tears them down instead of orphaning them.
_LIVE: set[subprocess.Popen] = set()
_LIVE_LOCK = threading.Lock()
_PRINT_LOCK = threading.Lock()


def _say(msg: str) -> None:
    """Thread-safe print — with N runs streaming output concurrently, this lock
    is what keeps lines from interleaving mid-print."""
    with _PRINT_LOCK:
        print(msg, flush=True)


def _task_cost(run_dir: Path) -> float:
    """Cumulative USD spent on one task so far = sum of its per-stage cost
    totals from state.json (each stage refreshes cost there live every round).
    Best-effort on purpose: a missing or half-written state file reads as 0.0,
    so a transient read can never trigger a false budget abort."""
    try:
        st = json.loads((run_dir / "state.json").read_text())
    except Exception:
        return 0.0
    total = 0.0
    for entry in (st.get("stages") or {}).values():
        tot = (entry.get("cost") or {}).get("totals") or {}
        g = tot.get("grand_total_usd")
        if isinstance(g, (int, float)):
            total += float(g)
        else:  # grand total not computed yet (no harbor dollars) → fall back to the parts
            total += float(tot.get("sdk_cost_usd") or 0.0) + float(tot.get("harbor_cost_usd") or 0.0)
    return total


def _budget_watchdog(proc: subprocess.Popen, slug: str, run_dir: Path,
                     max_cost: float, stop: threading.Event, state: dict) -> None:
    """Per-task budget guard. Polls the task's cumulative cost every ~20s; the
    moment it crosses max_cost we SIGTERM the run. The child's own runguard
    handler turns that SIGTERM into a full descendant-tree teardown (SDK
    agents, harbor, shells), so ONLY this task stops — cleanly — and its
    siblings keep running."""
    while not stop.is_set():
        if proc.poll() is not None:            # task already finished on its own
            return
        cost = _task_cost(run_dir)
        state["cost"] = cost
        if cost >= max_cost:
            state["aborted"] = True
            _say(f"⚠ BUDGET {slug}: cost ${cost:.2f} ≥ ${max_cost:.0f} cap — aborting this task.")
            try:
                proc.terminate()               # → child runguard tears down its whole tree
            except Exception:
                pass
            return
        stop.wait(20.0)                        # interruptible sleep; exits promptly when the task ends


def slugify(name: str) -> str:
    """Mirror of pipeline/lib/orchestration.slugify (kept local so this script
    runs even outside a full checkout). It matters that both agree EXACTLY:
    the proposal filename derives the run dir AND the lock file name."""
    base = Path(name).stem.lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return base or "task"


def _pick(row: dict, names: list[str], default: str = "") -> str:
    """Return the first non-empty value among the candidate column names —
    lets the same runner accept slightly different CSV headers."""
    for n in names:
        if n in row and (row.get(n) or "").strip():
            return row[n]
    return default


def load_seeds(path: Path) -> list[dict]:
    """Read the seeds CSV into a list of row dicts. Exits loudly if the file is
    missing or empty — better than 'selected 0 seeds' surprises later."""
    if not path.exists():
        sys.exit(f"Seeds CSV not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit(f"Seeds CSV is empty: {path}")
    return rows


def select(rows: list[dict], args) -> list[dict]:
    """Apply the --slugs / --domain / --limit filters, in that order, and
    return the rows that will actually run."""
    out = rows
    if args.slugs:
        wanted = [s.strip() for s in args.slugs.split(",") if s.strip()]
        by_slug = {slugify(_pick(r, SLUG_COLS)): r for r in rows}
        missing = [s for s in wanted if slugify(s) not in by_slug]
        if missing:
            sys.exit(f"Unknown seed Slug(s): {', '.join(missing)}\n"
                     f"Available: {', '.join(sorted(by_slug))}")
        out = [by_slug[slugify(s)] for s in wanted]
    if args.domain:
        out = [r for r in out if args.domain.lower() in (_pick(r, DOMAIN_COLS)).lower()]
    if args.limit is not None:
        out = out[: args.limit]
    return out


def run_one(seed: dict, args, stop_event: threading.Event, *,
            stream: bool, prefix: bool) -> dict:
    """Run ONE seed end-to-end: write its proposal, launch run_pipeline.py as
    an isolated subprocess, tee its output to the batch log (+ console unless
    --quiet), and police the --max-cost budget. Returns a result dict for the
    final summary; never raises — a failed seed is data, not a crash."""
    raw_slug = _pick(seed, SLUG_COLS)
    slug = slugify(raw_slug) if raw_slug else ""
    domain = _pick(seed, DOMAIN_COLS)
    desc = _pick(seed, DESC_COLS)
    res = {"slug": slug or "(missing-slug)", "domain": domain, "exit": None,
           "ok": False, "elapsed_sec": 0.0, "note": ""}

    if not slug:
        res["note"] = f"row has no Slug column ({SLUG_COLS})"
        _say(f"✗ SKIP (no Slug): {seed}")
        return res
    if not desc.strip():
        res["note"] = f"empty description column ({DESC_COLS})"
        _say(f"✗ SKIP {slug}: empty description")
        return res
    if stop_event.is_set():
        res["note"] = "skipped (--stop-on-error after an earlier failure)"
        _say(f"⏭ SKIP {slug}: batch stopping")
        return res

    args.proposals_dir.mkdir(parents=True, exist_ok=True)
    args.batch_logs_dir.mkdir(parents=True, exist_ok=True)
    proposal = args.proposals_dir / f"{slug}.md"
    proposal.write_text(desc, encoding="utf-8")
    batch_log = args.batch_logs_dir / f"{slug}.log"

    cmd = [sys.executable, str(RUN_PIPELINE), str(proposal), "--gate-mode", args.gate_mode]
    if args.config:
        cmd += ["--config", str(args.config)]
    if args.fresh:
        cmd.append("--fresh")

    _say(f"▶ START {slug:<34} ({domain})   log: {batch_log}   watch: python watch_run.py {slug}")
    # Force the child to line-buffer so its progress streams promptly through the pipe
    # (without a tty Python would block-buffer stdout and you'd see nothing until the end).
    child_env = dict(os.environ)
    child_env["PYTHONUNBUFFERED"] = "1"
    tag = f"[{slug}] " if prefix else ""
    started = time.time()
    wd_state = {"aborted": False, "cost": 0.0}   # shared with the per-task budget watchdog (below)
    try:
        with batch_log.open("w", encoding="utf-8") as fh:
            fh.write(f"# {' '.join(cmd)}\n# cwd={REPO}\n\n")
            fh.flush()
            proc = subprocess.Popen(cmd, cwd=REPO, env=child_env, text=True, bufsize=1,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            with _LIVE_LOCK:
                _LIVE.add(proc)
            # Per-task budget guard: a daemon thread polls this task's cumulative cost from
            # runs/<slug>/state.json and SIGTERMs the run (its runguard tears down the whole
            # subtree) the moment it crosses --max-cost — so a stuck task can't run away. Off
            # when --max-cost is unset.
            wd_stop = threading.Event()
            wd_thread = None
            if getattr(args, "max_cost", None):
                wd_thread = threading.Thread(
                    target=_budget_watchdog,
                    args=(proc, slug, RUNS / slug, float(args.max_cost), wd_stop, wd_state),
                    daemon=True)
                wd_thread.start()
            try:
                # TEE: every line goes to the per-seed file AND (unless --quiet) to the
                # console — the same live commentary you get from `python pipeline/stageN.py`,
                # tagged with the slug so concurrent runs stay readable.
                for line in proc.stdout:                       # iterates as the child emits lines
                    fh.write(line)
                    fh.flush()
                    if stream:
                        with _PRINT_LOCK:
                            sys.stdout.write(tag + line)
                            sys.stdout.flush()
                rc = proc.wait()
            finally:
                wd_stop.set()
                if wd_thread is not None:
                    wd_thread.join(timeout=2)
                with _LIVE_LOCK:
                    _LIVE.discard(proc)
    except Exception as exc:                                   # launch failure (rare)
        res["note"] = f"failed to launch: {exc}"
        _say(f"✗ ERROR {slug}: {exc}")
        return res

    res["elapsed_sec"] = round(time.time() - started, 1)
    res["exit"] = rc
    res["ok"] = (rc == 0)
    # run_pipeline exit codes: 0 ok · 1 a stage didn't pass / agent error · 2 human-aborted gate ·
    # 3 runguard lock (another process on this slug). Surface the meaning, not just the number.
    meaning = {0: "complete", 1: "stage failed/needs-human", 2: "gate aborted",
               3: "lock busy (already running?)"}.get(rc, "")
    if wd_state["aborted"]:                       # killed by the per-task budget guard
        res["note"] = (f"hit ${wd_state['cost']:.2f} ≥ ${float(args.max_cost):.0f} --max-cost cap; "
                       f"task aborted (resume later with run_pipeline.py --from <stage> {slug})")
        status = f"BUDGET-ABORT (${wd_state['cost']:.2f})"
    else:
        status = "OK" if res["ok"] else f"FAIL(exit={rc} {meaning})"
    _say(f"◀ DONE  {slug:<34} {status} in {res['elapsed_sec']}s")
    if not res["ok"] and not wd_state["aborted"] and args.stop_on_error:
        stop_event.set()
        _say("  (--stop-on-error: no new seeds will start; in-flight runs continue)")
    return res


def _terminate_live() -> None:
    """SIGTERM every in-flight child pipeline (best-effort) — used by the
    Ctrl-C fallback path when the runguard teardown isn't available."""
    with _LIVE_LOCK:
        procs = list(_LIVE)
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass


def main() -> int:
    """Argparse + selection + the thread pool. Stopping the batch (Ctrl-C or
    kill) tears down EVERYTHING via runguard's process-tree handler — the same
    code path a single stage script uses."""
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seeds-csv", type=Path, default=REPO / "seeds.csv",
                   help="CSV with Slug / Domain / description columns (default: ./seeds.csv).")
    p.add_argument("--max-parallel", "-j", type=int, default=2,
                   help="how many seeds run END-TO-END concurrently (top-level semaphore; default 2).")
    p.add_argument("--gate-mode", choices=["auto", "stdin"], default="auto",
                   help="'auto' (default) = NO human gate between stages; 'stdin' would prompt "
                        "(unusable in a parallel batch — kept only for a single-seed debug run).")
    p.add_argument("--config", type=Path, default=None, help="pipeline config passed to run_pipeline.py.")
    p.add_argument("--proposals-dir", type=Path, default=REPO / "runs" / "_seed_proposals",
                   help="where the per-seed proposal .md files are written (under runs/ = gitignored).")
    p.add_argument("--batch-logs-dir", type=Path, default=REPO / "runs" / "_seed_logs",
                   help="where each seed's combined run_pipeline stdout/stderr is captured.")
    p.add_argument("--limit", type=int, help="take only the first N selected seeds.")
    p.add_argument("--slugs", help="comma-separated Slugs to run (in that order).")
    p.add_argument("--domain", help="only seeds whose Domain contains this substring.")
    p.add_argument("--fresh", action="store_true",
                   help="pass --fresh to run_pipeline (wipe each stage workdir, rebuild from snapshot).")
    p.add_argument("--stop-on-error", action="store_true",
                   help="on the first failing seed, start no new ones (in-flight runs finish).")
    p.add_argument("--quiet", action="store_true",
                   help="don't stream each run's live output to the console (still captured to "
                        "--batch-logs-dir); show only START/DONE lines + the summary. Default streams.")
    p.add_argument("--max-cost", type=float, default=None, metavar="USD",
                   help="per-task budget ceiling in USD: auto-abort any single task whose cumulative "
                        "cost (sum of its stage costs in runs/<slug>/state.json) crosses this, so a "
                        "stuck task can't run away (e.g. --max-cost 120). Polled ~every 20s; the abort "
                        "is a clean runguard teardown of that task ONLY — other tasks keep running. "
                        "Off by default.")
    p.add_argument("--dry-run", action="store_true", help="print the selection + commands, run nothing.")
    args = p.parse_args()

    if args.max_parallel < 1:
        sys.exit("--max-parallel must be >= 1")
    if args.max_cost is not None and args.max_cost <= 0:
        sys.exit("--max-cost must be > 0 (USD)")
    if not RUN_PIPELINE.exists():
        sys.exit(f"run_pipeline.py not found at {RUN_PIPELINE}")

    rows = load_seeds(args.seeds_csv)
    selected = select(rows, args)
    if not selected:
        sys.exit("No seeds selected.")
    names = [slugify(_pick(r, SLUG_COLS)) or "(missing)" for r in selected]
    budget_note = f", max-cost=${args.max_cost:.0f}/task" if args.max_cost else ""
    _say(f"Selected {len(selected)} seed(s) [{args.max_parallel} at a time, gate={args.gate_mode}"
         f"{budget_note}]: {', '.join(names)}")

    if args.dry_run:
        for r in selected:
            slug = slugify(_pick(r, SLUG_COLS)) or "(missing)"
            desc_len = len((_pick(r, DESC_COLS)).strip())
            _say(f"  • {slug:<34} ({_pick(r, DOMAIN_COLS)})  desc={desc_len} chars  "
                 f"→ proposal {args.proposals_dir / (slug + '.md')}")
        _say("\n(dry run — nothing executed)")
        return 0

    # Prefix each streamed line with its slug only when output can actually interleave
    # (>1 run able to run at once) — a lone run streams clean, just like running a stage directly.
    stream = not args.quiet
    prefix = stream and args.max_parallel > 1 and len(selected) > 1
    if stream:
        _say("(streaming live output below" + (" — lines tagged [slug]" if prefix else "")
             + "; use --quiet for just START/DONE + summary)")

    # Stopping the batch must tear down EVERYTHING — same as stopping a single stage script.
    # Install the pipeline's own SIGINT/SIGTERM handler so it SIGTERM→SIGKILLs the whole
    # descendant tree (run_pipeline children + their claude/harbor/docker/shells). Falls back to
    # the KeyboardInterrupt handler below if runguard couldn't be imported.
    if _runguard is not None:
        _runguard.install_signal_handlers()

    stop_event = threading.Event()
    results: list[dict] = []
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=args.max_parallel) as pool:
            futs = {pool.submit(run_one, seed, args, stop_event, stream=stream, prefix=prefix): seed
                    for seed in selected}
            for fut in as_completed(futs):
                results.append(fut.result())
    except KeyboardInterrupt:
        _say("\n[run_seeds] Ctrl-C — terminating in-flight pipelines …")
        stop_event.set()
        _terminate_live()
        return 130

    ok = sum(1 for r in results if r["ok"])
    _say(f"\n{'='*78}\nBATCH SUMMARY  ({round(time.time()-t0,1)}s total, {args.max_parallel} at a time)\n{'='*78}")
    for r in sorted(results, key=lambda r: r["slug"]):
        mark = "✓" if r["ok"] else "✗"
        tail = f"  exit={r['exit']}" if r["exit"] is not None else ""
        note = f"  [{r['note']}]" if r["note"] else ""
        _say(f"  {mark} {r['slug']:<34} {r['elapsed_sec']:>7}s{tail}{note}")
    _say(f"\n{ok}/{len(results)} succeeded. Deliverables: runs/<slug>/snapshots/after-stage3/task")
    _say("Overview / liveness:  python watch_run.py --list")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
