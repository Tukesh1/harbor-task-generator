#!/usr/bin/env python3
"""Stage 1 — author a complete, realistic, fair TB3 task that is likely to
break SOTA, then drive it until the oracle (reference solution) scores 1.0 and
the no-op (empty submission) scores below 1.0.

This stage is deliberately NON-blocking for difficulty: nobody grades how
hard the task is here — Stage 2 owns that fight. What Stage 1 must produce is
a *working* task: real environment, real verifier, genuine solution.

Loop, in plain words: builder writes ./task → Python runs oracle + no-op
through harbor → if they don't pass, send the failure details back to the
builder → repeat up to stage1_max_rounds.

Run standalone:
    python pipeline/stage1_author.py <proposal.md>
or via the conductor (pipeline/run_pipeline.py).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import costs, harbor, runguard
from lib.common import build_claude_md, load_prompt, subs
from lib.config import load_config
from lib.orchestration import (collect_qa_evidence, gate, init_run,
                               provision_stage, set_stage, snapshot)
from lib.sdk import AgentSession, SessionError, session_error_hint

STAGE = "stage1"
SYSTEM = ("You are the Stage 1 author agent in a TB3 task-generation pipeline. "
          "Follow your playbook at .claude/skills/stage1-author-task/SKILL.md and "
          "the rules in ./CLAUDE.md. Build the task under ./task.")


def _tree(root: Path) -> str:
    """Indented file listing of ./task (for the stage report) — so a human can
    eyeball what was authored without opening the workdir."""
    if not root.exists():
        return "(no ./task created)"
    items = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    return "\n".join(f"  task/{i}" for i in items) or "  (empty)"


def _task_has_files(root: Path) -> bool:
    """Cheap guard before burning a harbor run: did the builder actually
    author anything yet? An empty ./task just needs a nudge, not a trial."""
    return root.exists() and any(p.is_file() for p in root.rglob("*"))


def _fix_prompt(oracle: harbor.RunResult, nop: harbor.RunResult) -> str:
    """Feedback sent when validation fails: exactly which of oracle/no-op broke,
    with the exception + verifier stdout attached, and the instruction to fix it
    WITHOUT weakening difficulty."""
    lines = ["The conductor ran validation on ./task:",
             f"- oracle reward = {oracle.reward} (need exactly 1.0)",
             f"- no-op reward  = {nop.reward} (need < 1.0)"]
    if not oracle.passed:
        lines.append("\nORACLE FAILED — your reference solution + verifier do not yield reward 1.0. "
                     "Fix solve.sh and/or the verifier so the genuine solution passes.")
        if oracle.exception:
            lines.append(f"Trial exception: {oracle.exception}")
        if oracle.test_stdout:
            lines.append("Verifier stdout (truncated):\n" + oracle.test_stdout)
        elif oracle.stderr:
            lines.append("harbor stderr (truncated):\n" + oracle.stderr[-3000:])
    if nop.passed:
        lines.append("\nNO-OP PASSED — the task is solved with no work, so the verifier does not "
                     "discriminate. Make it require real work so an empty submission fails.")
    lines.append("\nFix ./task accordingly WITHOUT weakening difficulty or making it contrived, "
                 "then end with a one-line status.")
    return "\n".join(lines)


def _report(run_dir: Path, task_dir: Path, oracle, nop, notes: Path) -> str:
    """Markdown stage report: pass/fail line, authored file tree, and the
    design-notes excerpt (the 'why this task is hard' rationale)."""
    notes_txt = notes.read_text()[:4000] if notes.exists() else "(no design notes written)"
    return (f"# Stage 1 — {run_dir.name}\n\n"
            f"Result: oracle={oracle.reward} (pass) ✅  nop={nop.reward} (fail) ✅\n\n"
            f"## Task files\n{_tree(task_dir)}\n\n"
            f"## Design notes (excerpt)\n{notes_txt}\n")


async def run(cfg, run_dir: Path, fresh: bool = False) -> dict:
    """Stage 1 entrypoint (called by the conductor or this file's main()).

    Provision the workdir from the proposal, start one builder session, and
    loop: validate → fix-feedback → validate, until oracle 1.0 + no-op <1.0
    or the round budget dies. Returns {status: ok|needs_human|error,
    summary, snapshot?} — 'ok' snapshots the task as Stage 2's seed.
    """
    run_dir = Path(run_dir)
    proposal = run_dir / "00-proposal.md"
    workdir = provision_stage(
        cfg, run_dir, STAGE, ["stage1-author-task"],
        seed_task=None,
        inputs={"proposal.md": proposal},
        claude_md=build_claude_md(cfg, "Stage 1 — Author the task"),
        substitutions=subs(cfg),
        fresh=fresh,
    )
    task_dir = workdir / "task"
    notes = workdir / "stage1-design-notes.md"
    log = run_dir / "logs" / f"{STAGE}.builder.log"
    jobs_root = run_dir / "harbor" / STAGE
    jobs_root.mkdir(parents=True, exist_ok=True)
    set_stage(run_dir, STAGE, status="running", workdir=str(workdir))
    cost = costs.StageCost(STAGE)

    try:
        async with AgentSession(cwd=workdir, model=cfg.model, system_append=SYSTEM,
                                log_path=log, label="stage1-builder",
                                permission_mode=cfg.permission_mode,
                                max_turns=cfg.max_turns,
                                stall_timeout_sec=cfg.stall_timeout_sec,
                                stall_retries=cfg.stall_retries,
                                stall_settle_sec=cfg.stall_settle_sec,
                                api_max_retries=cfg.api_max_retries,
                                api_backoff_base_sec=cfg.api_backoff_base_sec,
                                api_backoff_max_sec=cfg.api_backoff_max_sec) as builder:
            await builder.send(load_prompt(cfg, "stage1_kickoff.md"))
            for rnd in range(1, cfg.stage1_max_rounds + 1):
                # Don't waste a harbor run if the agent hasn't authored anything yet.
                if not _task_has_files(task_dir):
                    print(f"[stage1 r{rnd}] ./task is empty — nudging the builder to author it.", flush=True)
                    if rnd == cfg.stage1_max_rounds:
                        summary = "Stage 1 failed: the builder never created any files under ./task."
                        set_stage(run_dir, STAGE, status="needs_human")
                        costs.finalize(cost, run_dir, STAGE, sessions=[builder])
                        return {"status": "needs_human", "summary": summary}
                    await builder.send(
                        "You have not created any files under ./task yet. Author the COMPLETE task "
                        "now (instruction.md, task.toml, environment/, tests/, solution/) per your "
                        "playbook, then end with a status.")
                    continue
                oracle = await asyncio.to_thread(harbor.run_oracle, cfg, task_dir, jobs_root)
                nop = await asyncio.to_thread(harbor.run_nop, cfg, task_dir, jobs_root)
                cost.add_harbor(oracle, nop)
                ok = oracle.passed and not nop.passed
                print(f"[stage1 r{rnd}] oracle={oracle.reward} nop={nop.reward} ok={ok}", flush=True)
                set_stage(run_dir, STAGE, round=rnd, oracle_reward=oracle.reward, nop_reward=nop.reward)
                costs.record_live(cost, run_dir, STAGE, builder)
                if ok:
                    report = _report(run_dir, task_dir, oracle, nop, notes)
                    snap = snapshot(run_dir, STAGE, task_dir, report=report,
                                    extra={"stage1-design-notes.md": notes} if notes.exists() else None)
                    collect_qa_evidence(run_dir, STAGE, snap,
                                        summary={"oracle": oracle.reward, "nop": nop.reward})
                    set_stage(run_dir, STAGE, status="ok", snapshot=str(snap))
                    costs.finalize(cost, run_dir, STAGE, sessions=[builder], snapshot_dir=snap)
                    return {"status": "ok", "summary": report, "snapshot": str(snap)}
                if rnd == cfg.stage1_max_rounds:
                    summary = (f"Stage 1 did NOT reach oracle/nop passing after {rnd} rounds "
                               f"(oracle={oracle.reward}, nop={nop.reward}).")
                    snap = snapshot(run_dir, STAGE, task_dir, report=summary,
                                    extra={"stage1-design-notes.md": notes} if notes.exists() else None)
                    set_stage(run_dir, STAGE, status="needs_human", snapshot=str(snap))
                    costs.finalize(cost, run_dir, STAGE, sessions=[builder], snapshot_dir=snap)
                    return {"status": "needs_human", "summary": summary, "snapshot": str(snap)}
                await builder.send(_fix_prompt(oracle, nop))
    except SessionError as e:
        msg = f"Stage 1 aborted — the agent session failed: {e}\n{session_error_hint(e)}"
        print(f"[stage1] {msg}", flush=True)
        set_stage(run_dir, STAGE, status="error", error=str(e))
        costs.finalize(cost, run_dir, STAGE)
        return {"status": "error", "summary": msg}

    costs.finalize(cost, run_dir, STAGE)
    return {"status": "error", "summary": "unreachable"}


def main() -> int:
    """Standalone entry: lock + guard, init the run dir from the proposal,
    then run stage 1 and (on success) show the end-of-stage gate."""
    ap = argparse.ArgumentParser(description="Stage 1 — author a TB3 task from a proposal.")
    ap.add_argument("proposal", help="path to the proposal .md")
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-gate", action="store_true", help="skip the end-of-stage gate")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe the stage1 workdir and rebuild (default: resume if one exists)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    runguard.guard(cfg, stage=STAGE, slug=Path(args.proposal).stem)
    run_dir = init_run(cfg, Path(args.proposal))
    result = asyncio.run(run(cfg, run_dir, fresh=args.fresh))
    print(f"\n[stage1] status={result['status']}  snapshot={result.get('snapshot')}", flush=True)
    if result["status"] == "ok" and not args.no_gate:
        gate(cfg, run_dir, STAGE, result["summary"])
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
