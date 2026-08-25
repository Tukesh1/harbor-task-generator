#!/usr/bin/env python3
"""Conductor — run all three stages back-to-back on one proposal, with an
optional human gate between stages.

This is the usual entrypoint for a single task:

    python pipeline/run_pipeline.py <proposal.md>            # fresh run
    python pipeline/run_pipeline.py --from stage2 <slug>     # resume mid-chain

How it behaves, in short:
  * Every stage works inside its own provisioned workdir (runs/<slug>/stageN/).
  * After each stage the task state is snapshotted OUTSIDE that agent workspace,
    at runs/<slug>/snapshots/after-stageN/ — inspection and the next stage's
    seed never depend on the agent keeping its room tidy.
  * Any stage ending other than status=ok stops the chain for human review.
    Between-stage gates ask y/n on stdin; set [gate].mode = "auto" (or pass
    --gate-mode auto) to remove the human — that's how unattended batches run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stage1_author
import stage2_break
import stage3_qa
from lib import runguard
from lib.config import load_config
from lib.orchestration import find_run_dir, gate, init_run, set_stage
from lib.sdk import SessionError, session_error_hint

STAGES = [("stage1", stage1_author.run),
          ("stage2", stage2_break.run),
          ("stage3", stage3_qa.run)]


def _banner(text: str) -> None:
    """Big obvious section separator, so stage boundaries are unmissable when
    you're scrolling a long log."""
    print("\n" + "#" * 72 + f"\n#  {text}\n" + "#" * 72, flush=True)


async def _run_chain(cfg, run_dir: Path, start_idx: int, fresh: bool = False) -> int:
    """Run stages [start_idx:] in order, gating between them.

    Stop conditions, in plain words:
      * agent session blew up (auth/billing/stream)   → exit 1, status=error;
      * a stage ended not-ok (needs_human / failure)  → exit 1, stop for review;
      * human answered 'n' at a gate                  → exit 2;
      * all three stages ok                           → exit 0, deliverable path printed.
    """
    for idx in range(start_idx, len(STAGES)):
        name, fn = STAGES[idx]
        _banner(f"{name.upper()} starting  ({run_dir})")
        try:
            result = await fn(cfg, run_dir, fresh=fresh)
        except SessionError as e:
            set_stage(run_dir, name, status="error", error=str(e))
            _banner(f"{name.upper()} aborted — agent session failed.")
            print(f"{e}\n{session_error_hint(e)}", flush=True)
            return 1
        status = result["status"]
        print(f"\n[{name}] status={status}  snapshot={result.get('snapshot')}", flush=True)
        if status != "ok":
            _banner(f"{name.upper()} did not pass (status={status}) — stopping for human review.")
            print(result.get("summary", ""), flush=True)
            return 1
        # gate before the next stage (and after the final stage = acceptance)
        if not gate(cfg, run_dir, name, result["summary"]):
            _banner(f"Human aborted after {name}.")
            return 2
    _banner("PIPELINE COMPLETE")
    print(f"Deliverable: {run_dir / 'snapshots' / 'after-stage3' / 'task'}", flush=True)
    return 0


def main() -> int:
    """Argparse + config + run-guard, then hand over to the async chain."""
    ap = argparse.ArgumentParser(description="Run the full TB3 task-generation pipeline.")
    ap.add_argument("target", help="proposal .md (fresh run) OR run-dir/slug (with --from)")
    ap.add_argument("--from", dest="from_stage", choices=["stage1", "stage2", "stage3"],
                    default="stage1", help="resume an existing run from this stage")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe each stage's workdir and rebuild from the prior snapshot "
                         "(default: resume — keep task + progress, refresh skills/CLAUDE)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--gate-mode", dest="gate_mode", choices=["stdin", "auto"], default=None,
                    help="override [gate].mode: 'auto' runs all stages with NO human gate "
                         "(used by scripts/run_seeds.py for unattended batches); 'stdin' prompts.")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.gate_mode:
        cfg.gate_mode = args.gate_mode
    runguard.guard(cfg, stage="pipeline", slug=Path(args.target).stem)
    start_idx = [s[0] for s in STAGES].index(args.from_stage)

    if args.from_stage == "stage1":
        run_dir = init_run(cfg, Path(args.target))
    else:
        run_dir = find_run_dir(cfg, args.target)

    return asyncio.run(_run_chain(cfg, run_dir, start_idx, fresh=args.fresh))


if __name__ == "__main__":
    raise SystemExit(main())
