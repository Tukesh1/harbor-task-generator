#!/usr/bin/env python3
"""Stage 3 — pass all QA checks, then RE-VERIFY that the break survived. Final stage.

Phase A (QA): each round Python runs `harbor check` (implementation rubric) plus
the QA suite — Task QA's non-SOTA LLM checks ([task_qa].llm_checks, binary gate),
or AutoQA v3 'llm_only' when [autoqa] is enabled instead. The builder fixes
findings under the CARDINAL RULE: never make the task easier for a strong agent
(satisfy 'clarity' by precise specification, never by hints).

Phase B (verify): QA is green — but Stage 3 EDITED the task, so now we prove the
break still holds. In order: break trials → no-op → oracle → harbor analyze over
ALL trajectories (multi-model) → Task QA SOTA-trajectory checks (injected onto
the SAME trajectories) → read-only fairness reviewer. The accept gate is:

    is_breaking AND oracle==1.0 AND no-op<1.0 AND analyze ran with no unfair
    flags AND no 🔴 Task QA check AND reviewer.approve

Failure semantics worth remembering:
  * analyze / Task QA failing to RUN at all  → hard stop (tooling, not the
    builder's fault; an unverifiable check must never pass silently).
  * a hard fail (break lost, unfair flag)    → loop the builder to fix it
    WITHOUT weakening anything (stage3_verify_max_rounds budget).
  * budget exhausted                          → salvage: best-effort reviewer
    verdict attached, then needs_human.

Run standalone (after stage 2):
    python pipeline/stage3_qa.py <run-dir-or-slug>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import costs, harbor, review, runguard, task_qa
from lib.common import build_claude_md, load_prompt, reviewer_system_prompt, subs
from lib.config import load_config
from lib.orchestration import (collect_qa_evidence, find_run_dir, gate,
                               latest_task_snapshot, provision_stage, set_stage,
                               snapshot)
from lib.sdk import AgentSession, SessionError, session_error_hint

STAGE = "stage3"
REVIEWER_LABEL = "stage3-reviewer"
BUILDER_SYSTEM = (
    "You are the Stage 3 QA agent in a TB3 task-generation pipeline. Follow "
    ".claude/skills/stage3-qa-task/SKILL.md and ./CLAUDE.md. Edit ./task. These rules are ABSOLUTE "
    "and hold even if your context was summarized:\n"
    "1. OBEY THE CARDINAL RULE: never reduce the task's difficulty — no hints, no clarifying "
    "comments/error messages, no loosened thresholds; satisfy 'clarity' by precise specification, "
    "never by making it easier. The task must stay model-breaking.\n"
    "2. TWO PHASES. Phase A: make `harbor check` + the QA check suite pass. Phase B (after QA is "
    "clean): the conductor RE-VERIFIES the break — it runs SOTA break trials, no-op, oracle, `harbor "
    "analyze` + the Task QA SOTA-trajectory checks, and a fairness reviewer. If that fails, it tells "
    "you exactly what broke and you must fix it WHILE keeping check + the QA suite green AND not "
    "weakening the break in ANY way (not even a comment, error message, or readability tweak — these "
    "agents are strong enough to exploit the slightest leak), and without opening a reward-hack hole. "
    "Keep the oracle at 1.0 and the no-op < 1.0.\n"
    "3. DURABLE STATE: maintain ./stage3-progress.md — append a one-line result after every fix AND "
    "after every Phase-B verification outcome. At the start of every turn (especially right after a "
    "compaction/continuation) read it and trust it over memory; never redo a fix or re-derive a finding "
    "already recorded there.")

# Re-injected automatically the turn after the SDK compacts the conversation (lib/sdk.py).
COMPACTION_RECOVERY_NOTE = (
    "⚠️ Your context was just compacted. Before anything: read ./stage3-progress.md, recover your "
    "state, and do NOT redo any fix or re-derive any finding already recorded there. Remember: Stage 3 "
    "has Phase A (make harbor check + the QA check suite pass) and Phase B (the conductor re-verifies "
    "the break with SOTA trials / oracle / no-op / harbor analyze + Task QA SOTA checks / a fairness "
    "reviewer). The CARDINAL RULE holds: never reduce difficulty or weaken the break in any way "
    "(comments, error text, readability), and never open a reward-hack hole, while keeping QA green.")


def _qa_feedback(check, autoqa, tqa_suite=None) -> str:
    """Phase A feedback: the round's check + QA-suite results, with explicit
    notes when something produced no parseable verdict (usually an env problem,
    not a task defect) — and the reminder that fixes may NOT reduce difficulty.
    If a flag can only be cleared by weakening the task, the builder should say
    so instead of complying."""
    lines = ["QA round results:",
             f"- harbor check: {check.summary}",
             f"  failing criteria: {check.failed_criteria}",
             f"  (raw JSON: {check.out_path}; parse_ok={check.parse_ok})"]
    if not check.parse_ok:
        lines.append("  NOTE: the check JSON could not be parsed into criteria — read the raw "
                     "JSON yourself and address whatever it flags.")
    if autoqa is not None:
        lines.append(f"- AutoQA: {autoqa.summary}")
        lines.append(f"  (raw JSON: {autoqa.out_path}; parse_ok={autoqa.parse_ok})")
        for issue in autoqa.issues:
            lines.append(f"    • {issue}")
        if not autoqa.parse_ok:
            lines.append("  NOTE: AutoQA produced no parseable result — this is usually an env/deps "
                         "problem (interpreter/cwd), not a task defect. Read the raw JSON / flag it.")
    if tqa_suite is not None:
        lines.append(f"- Task QA checks: {tqa_suite.summary}")
        lines.append(f"  (per-check JSON under: {tqa_suite.out_dir}/)")
        for issue in tqa_suite.issues:
            lines.append(f"    • {issue}")
        if not tqa_suite.ran:
            lines.append("  NOTE: NO Task QA check produced a verdict — usually an env problem "
                         "(task QA CLI install / QA_JUDGE_OPENAI_API_KEY), not a task defect. Read the "
                         "raw JSON / flag it.")
    lines.append("\nFix ./task to clear the failing harbor-check criteria and QA-check issues, and DO "
                 "NOT reduce difficulty (satisfy 'clarity' by precise specification, never by hints "
                 "or loosened thresholds). If a QA flag would require weakening the task, do NOT "
                 "comply — explain why in your status instead. End with a status.")
    return "\n".join(lines)


def _verify_feedback(attempt, max_attempts, trials, oracle, nop, verdict, a_blocks=None,
                     tqa=None) -> str:
    """Phase B hard-fail feedback: QA is green but the break didn't survive QA edits.
    The builder must fix it WITHOUT weakening the break in any way and keep QA green."""
    lines = [
        f"PHASE B — break re-verification FAILED (verify attempt {attempt}/{max_attempts}). "
        "`harbor check` + the QA suite are GREEN, but the task must ALSO stay genuinely model-breaking, "
        "fair, and non-hackable after your QA edits. Fix the issue(s) below WITHOUT weakening the break "
        "in ANY way — no clarifying comments, error messages, readability/decompile hints, or loosened "
        "thresholds; satisfy clarity only by precise specification — and keep `harbor check` + the QA "
        "suite passing, the oracle at 1.0, and the no-op < 1.0. Log the change in ./stage3-progress.md.",
    ]
    if trials is not None and not trials.is_breaking:
        lines.append(f"- NOT MODEL-BREAKING: {trials.model or 'the SOTA model'} scored {trials.rewards} "
                     f"({trials.passes}/{trials.n} passed). A QA edit made it solvable — restore the "
                     "conceptual difficulty without weakening the agent-visible surface.")
    if oracle is not None and not oracle.passed:
        lines.append(f"- ORACLE regressed (reward={oracle.reward}, need 1.0) — repair solve.sh/verifier.")
    if nop is not None and nop.passed:
        lines.append(f"- NO-OP passes (reward={nop.reward}) — make the task require real work again.")
    if a_blocks:
        lines.append(f"- harbor analyze (official TB3 check) flagged the task UNFAIR/hackable: {a_blocks}. "
                     "'task_specification' → the spec is inconsistent with the verifier or not derivable "
                     "from instruction.md + the agent-visible env (fix the spec precisely, no hints); "
                     "'reward_hacking' → close the verifier hole; 'non_clericalness' → the model "
                     "substantively solved the task and was failed on FORM (formatting/precision/naming/"
                     "ordering) — move the difficulty into the concept, do NOT keep a clerical gotcha "
                     "as the break. Do NOT reduce difficulty.")
    if tqa is not None and tqa.red_checks:
        lines.append(f"- Task QA SOTA-trajectory checks flagged 🔴 on EVERY breaking trajectory: "
                     f"{tqa.red_checks}. 'false-negatives' → the break leans on a value/convention the "
                     "agent provably could NOT derive from instruction.md + the agent-visible env, or "
                     "brittle tests reject valid solutions — make it derivable or fix the test; "
                     "'false-positives' → an incorrect solution can pass — close the verifier hole; "
                     "'valid-constraint-allowance' → the tests forbid a legitimate approach the "
                     f"instruction allows. Per-trajectory findings: {tqa.out_dir}/")
    if verdict:
        lines.append("- Fairness reviewer (adjudicating the harbor analyze findings) said REVISE:")
        lines.append(review.reviewer_feedback(verdict))
    lines.append("End with a status.")
    return "\n".join(lines)


def _salvage_review_msg(task_rel: str, reason: str) -> str:
    """Only used when Stage 3 never reached a Phase-B verify (e.g. QA never
    converged): ask the reviewer for a best-effort qualitative read so the
    human still gets a verdict attached to the salvage."""
    return (
        "SALVAGE fairness review. Stage 3 could not converge to a fully-passing state "
        f"({reason}), so no fresh failing-trajectory / harbor analyze artifacts are available for this "
        f"pass. Give your best qualitative read of the CURRENT task at {task_rel} (read everything, incl. "
        "solution/solve.sh): is it still realistic, expert-solvable from instruction.md + the "
        "agent-visible environment, non-contrived, plausibly model-breaking, and non-hackable? Flag the "
        "residual risk for the human reviewer. Return your strict JSON verdict (with `analyze_review`, "
        "which here may note that analyze was not re-run) as the last thing in your reply.")


def _stage2_fairness(run_dir: Path):
    """(verdicts_file, final_verdict_dict) — pull the Stage 2 fairness review
    (final = its Phase C verdict) out of the after-stage2 snapshot. Stage 3
    consolidates that 'final review of the agent' into the deliverable, which
    is why the file-path contract between the two stages matters."""
    vfile = run_dir / "snapshots" / "after-stage2" / "stage2-reviewer-verdicts.json"
    final = None
    if vfile.exists():
        try:
            vs = json.loads(vfile.read_text())
            if isinstance(vs, list) and vs:
                final = next((v for v in reversed(vs) if v.get("_phase") == "C"), vs[-1])
        except Exception:
            final = None
    return (vfile if vfile.exists() else None), final


def _qa_evidence(cfg, run_dir, snap, check, autoqa, verify=None, also_extra=None,
                 tqa_suite=None) -> str:
    """Bundle everything a human reviewer needs into <snapshot>/qa/:
    harbor check + the QA suite (Task QA and/or AutoQA) + this stage's Phase-B
    verify result + our own fairness verdicts/replies (``also_extra``) + the
    cross-stage Stage 2 fairness verdict. harbor/stage3/* (analyze JSONs, tqa
    output dirs) is copied automatically by collect_qa_evidence."""
    vfile, fairness = _stage2_fairness(run_dir)
    summary = {
        "harbor_check": {"passed": check.passed, "failing": check.failed_criteria} if check else None,
        "autoqa": ({"mode": cfg.autoqa_mode, "overall": autoqa.overall,
                    "acceptable": autoqa.acceptable, "issues": autoqa.issues}
                   if autoqa else "disabled"),
        "task_qa": ({"acceptable": tqa_suite.acceptable, "issues": tqa_suite.issues}
                     if tqa_suite else "disabled"),
        "phaseB_verify": verify,
        "stage2_fairness_verdict": fairness,
    }
    also = dict(also_extra or {})
    if vfile:
        also["stage2-reviewer-verdicts.json"] = vfile
    return collect_qa_evidence(run_dir, STAGE, snap, summary=summary, also=(also or None))


async def run(cfg, run_dir: Path, fresh: bool = False) -> dict:
    """Stage 3 entrypoint: the Phase A (QA) / Phase B (re-verify) loop.

    One builder + one reviewer session, a break-trial cache, and the same
    terminal-path discipline as Stage 2 (flush → snapshot → evidence → costs).
    Returns {status: ok|needs_human|error, summary, snapshot?}; on 'ok' the
    deliverable is snapshots/after-stage3/task.
    """
    run_dir = Path(run_dir)
    seed = latest_task_snapshot(run_dir, "stage2")
    if not seed.exists():
        return {"status": "error", "summary": f"no stage2 snapshot at {seed}; run stage 2 first."}

    notes_src = run_dir / "snapshots" / "after-stage1" / "stage1-design-notes.md"
    workdir = provision_stage(
        cfg, run_dir, STAGE, ["stage3-qa-task"],
        seed_task=seed,
        inputs={"proposal.md": run_dir / "00-proposal.md", "stage1-design-notes.md": notes_src},
        claude_md=build_claude_md(cfg, "Stage 3 — Pass QA without weakening the task"),
        substitutions=subs(cfg),
        fresh=fresh,
    )
    task_dir = workdir / "task"
    task_rel = "./task"
    jobs_root = run_dir / "harbor" / STAGE
    jobs_root.mkdir(parents=True, exist_ok=True)
    blog = run_dir / "logs" / f"{STAGE}.builder.log"
    rlog = run_dir / "logs" / f"{STAGE}.reviewer.log"
    set_stage(run_dir, STAGE, status="running", workdir=str(workdir))
    cost = costs.StageCost(STAGE)
    # Reuse break trials when the task is byte-identical (fingerprint-keyed) so a Phase-B
    # re-verify that doesn't change the task doesn't pay to re-break (and analyze + Task QA
    # judge the same trajectories). ANY edit under task/ misses the cache and re-runs.
    bcache = harbor.BreakCache(cfg.reuse_break_trajectories)

    qa_report = {"rounds": [], "verify": []}
    verdicts: list[dict] = []
    reviewer_replies: list[tuple] = []
    verdicts_file = workdir / "stage3-reviewer-verdicts.json"
    replies_file = workdir / "stage3-reviewer-replies.md"

    def _flush_qa():
        (workdir / "stage3-qa-report.json").write_text(json.dumps(qa_report, indent=2, default=str))
        if verdicts:
            verdicts_file.write_text(json.dumps(verdicts, indent=2, default=str))
        if reviewer_replies:
            replies_file.write_text(review.replies_markdown(reviewer_replies))

    def _also_extra() -> dict:
        d = {}
        if verdicts_file.exists():
            d["stage3-reviewer-verdicts.json"] = verdicts_file
        if replies_file.exists():
            d["stage3-reviewer-replies.md"] = replies_file
        return d

    last_check = last_autoqa = last_tqa_suite = None
    last_verify = None        # summary of the last Phase-B verify (for salvage)
    verify_attempts = 0

    async with AgentSession(cwd=workdir, model=cfg.model, system_append=BUILDER_SYSTEM,
                            log_path=blog, label="stage3-builder",
                            permission_mode=cfg.permission_mode, max_turns=cfg.max_turns,
                            stall_timeout_sec=cfg.stall_timeout_sec, stall_retries=cfg.stall_retries,
                            stall_settle_sec=cfg.stall_settle_sec,
                            api_max_retries=cfg.api_max_retries,
                            api_backoff_base_sec=cfg.api_backoff_base_sec,
                            api_backoff_max_sec=cfg.api_backoff_max_sec,
                            compaction_recovery_note=COMPACTION_RECOVERY_NOTE) as builder, \
               AgentSession(cwd=workdir, model=cfg.model, system_append=reviewer_system_prompt(cfg),
                            log_path=rlog, label=REVIEWER_LABEL,
                            permission_mode=cfg.permission_mode, max_turns=cfg.max_turns,
                            stall_timeout_sec=cfg.stall_timeout_sec, stall_retries=cfg.stall_retries,
                            stall_settle_sec=cfg.stall_settle_sec,
                            api_max_retries=cfg.api_max_retries,
                            api_backoff_base_sec=cfg.api_backoff_base_sec,
                            api_backoff_max_sec=cfg.api_backoff_max_sec,
                            allowed_tools=review.REVIEWER_RO_TOOLS,
                            disallowed_tools=review.REVIEWER_RO_DISALLOWED,
                            compaction_recovery_note=review.REVIEWER_COMPACTION_NOTE) as reviewer:

        await builder.send(load_prompt(cfg, "stage3_kickoff.md"))
        # Prime the reviewer in the BACKGROUND; awaited just before its first use (Phase B).
        reviewer_primed = asyncio.create_task(reviewer.send(load_prompt(cfg, "reviewer_kickoff.md")))

        def _infra_abort(rnd: int, tr) -> dict:
            """All Phase-B break trials errored — infra failure (NOT 'not breaking')."""
            if not reviewer_primed.done():
                reviewer_primed.cancel()
            msg = (f"Stage 3 aborted at round {rnd}: all Phase-B break trials ERRORED — no model "
                   f"signal, an infrastructure failure (NOT 'task no longer breaks'). "
                   f"{tr.error_summary}. Fix the infra and re-run. Failing job dir: {tr.job_dir}")
            print(f"[stage3 r{rnd}] ALL TRIALS ERRORED — infra failure, aborting. "
                  f"{tr.error_summary}", flush=True)
            _flush_qa()
            snap = snapshot(run_dir, STAGE, task_dir, report=msg,
                            extra={"stage3-qa-report.json": workdir / "stage3-qa-report.json",
                                   **_also_extra()})
            _qa_evidence(cfg, run_dir, snap, last_check, last_autoqa, verify=last_verify,
                         also_extra=_also_extra(), tqa_suite=last_tqa_suite)
            set_stage(run_dir, STAGE, status="error", error=msg, snapshot=str(snap))
            costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
            return {"status": "error", "summary": msg, "snapshot": str(snap)}

        for rnd in range(1, cfg.stage3_max_rounds + 1):
            # ---- Phase A: QA (harbor check + the QA check suite) ----
            check = await asyncio.to_thread(
                harbor.run_check, cfg, task_dir, run_dir / "harbor" / STAGE / f"check-{rnd}.json")
            autoqa = None
            if cfg.autoqa_enabled:
                autoqa = await asyncio.to_thread(
                    harbor.run_autoqa, cfg, task_dir, run_dir / "harbor" / STAGE / f"autoqa-{rnd}.json")
            tqa_suite = None
            if cfg.tqa_enabled and cfg.tqa_llm_checks:
                tqa_suite = await asyncio.to_thread(
                    task_qa.run_llm_checks, cfg, task_dir,
                    run_dir / "harbor" / STAGE / f"tqa-llm-{rnd}")
            cost.add_harbor(check, autoqa, tqa_suite)
            last_check, last_autoqa, last_tqa_suite = check, autoqa, tqa_suite
            autoqa_ok = (autoqa is None) or autoqa.acceptable
            tqa_suite_ok = (tqa_suite is None) or tqa_suite.acceptable
            qa_ok = check.passed and autoqa_ok and tqa_suite_ok
            print(f"[stage3 r{rnd}] check_pass={check.passed} "
                  f"autoqa={'(off)' if autoqa is None else autoqa.overall} "
                  f"tqa={'(off)' if tqa_suite is None else ('ok' if tqa_suite.acceptable else 'FAIL')} "
                  f"qa_ok={qa_ok}", flush=True)
            qa_report["rounds"].append({
                "round": rnd, "check_pass": check.passed, "check_fails": check.failed_criteria,
                "autoqa_overall": (autoqa.overall if autoqa else None),
                "autoqa_acceptable": (autoqa.acceptable if autoqa else None),
                "autoqa_issues": (autoqa.issues if autoqa else []),
                "tqa_acceptable": (tqa_suite.acceptable if tqa_suite else None),
                "tqa_issues": (tqa_suite.issues if tqa_suite else [])})
            _flush_qa()
            set_stage(run_dir, STAGE, round=rnd, phase="A", check_pass=check.passed,
                      autoqa=(autoqa.overall if autoqa else "off"),
                      tqa=("off" if tqa_suite is None
                           else ("ok" if tqa_suite.acceptable else "fail")))
            costs.record_live(cost, run_dir, STAGE, builder, reviewer)

            if not qa_ok:
                if rnd == cfg.stage3_max_rounds:
                    break
                await builder.send(_qa_feedback(check, autoqa, tqa_suite))
                continue

            # ---- Phase B: re-verify the break (QA is clean) ----
            # Order: break trials -> no-op -> oracle -> harbor analyze -> reviewer adjudicates.
            verify_attempts += 1
            set_stage(run_dir, STAGE, round=rnd, phase="B", verify_attempt=verify_attempts)
            costs.record_live(cost, run_dir, STAGE, builder, reviewer)
            staged, _reused = await asyncio.to_thread(bcache.run_staged, cfg, task_dir, jobs_root)
            if not _reused:
                for r in staged.runs:
                    cost.add_harbor(r)
            trials = staged.result
            if trials.all_errored:
                return _infra_abort(rnd, trials)

            oracle = nop = analyze = tqa = None
            tqa_attempted = False
            verdict: dict = {}
            if trials.is_breaking:   # break held → restore-check + multi-model matrix analyze + review
                nop = await asyncio.to_thread(harbor.run_nop, cfg, task_dir, jobs_root)
                oracle = await asyncio.to_thread(harbor.run_oracle, cfg, task_dir, jobs_root)
                cost.add_harbor(nop, oracle)
                # Analyze ALL the break-model trajectories — probe + confirm = every k trial, not
                # just the confirm subset — and, at accept-time (oracle 1.0 / no-op <1.0), run the
                # SAME multi-model matrix as Stage-2 Phase C so analyze sees the cross-model spread.
                matrix_jobs = [(trials.model or "break-model", staged.probe.job_dir)]
                if staged.escalated and staged.confirm is not None:
                    matrix_jobs.append((trials.model or "break-model", staged.confirm.job_dir))
                # run_dirs of EVERY analyzed model, so the Task QA SOTA checks judge the SAME
                # multi-model trajectory set analyze sees (parity + stricter only-🔴 bar).
                sota_run_dirs = [t.run_dir for t in trials.trials if t.run_dir and not t.errored]
                if oracle.passed and not nop.passed:        # only pay for extra models at accept-time
                    for spec in cfg.analyze_matrix:
                        tag = spec.get("tag") or spec.get("model") or "extra"
                        tr, _mreused = await asyncio.to_thread(
                            bcache.run_single, cfg, task_dir, jobs_root, spec.get("k", 3),
                            f"matrix-{tag.replace('/', '_')}", spec.get("agent"), spec.get("model"),
                            spec.get("kwargs"))
                        if not _mreused:
                            cost.add_harbor(tr)
                        matrix_jobs.append((spec.get("model") or tag, tr.job_dir))
                        sota_run_dirs += [t.run_dir for t in tr.trials if t.run_dir and not t.errored]
                        print(f"[stage3 r{rnd}] matrix {tag}: rewards={tr.rewards} "
                              f"breaking={tr.is_breaking}", flush=True)
                analyze = await asyncio.to_thread(
                    harbor.run_analyze_matrix, cfg, matrix_jobs,
                    run_dir / "harbor" / STAGE / f"analyze-final-{rnd}.json",
                    cfg.analyze_matrix_failing)
                cost.add_harbor(analyze)
                # Task QA SOTA-trajectory checks over the SAME multi-model trajectory set analyze
                # used (injected — no fresh SOTA trial), at accept-time only. 3-state like analyze:
                # only 🔴 (EVERY trajectory flagged) hard-blocks, 🟡 is reviewer context.
                tqa_attempted = bool(cfg.tqa_enabled and cfg.tqa_stage3_sota_checks
                                     and oracle.passed and not nop.passed)
                if tqa_attempted:
                    tqa = await asyncio.to_thread(
                        task_qa.run_sota_check_matrix, cfg, task_dir, sota_run_dirs,
                        run_dir / "harbor" / STAGE / f"tqa-sota-final-{rnd}",
                        cfg.tqa_stage3_sota_checks)
                    cost.add_harbor(tqa)
                    print(f"[stage3 r{rnd}] tqa ran={tqa.ran} colors={tqa.colors} "
                          f"red={tqa.red_checks} (n_traj={tqa.n_trajectories})", flush=True)
                await reviewer_primed
                verdict, reply = await review.ask_reviewer(
                    reviewer, review.reviewer_round(cfg, run_dir, task_rel, trials, analyze,
                                                    "Stage 3 final state (post-QA)", final=True,
                                                    oracle=oracle, nop=nop, tqa=tqa))
                verdict["_phase"] = "stage3-B"; verdict["_round"] = rnd
                verdicts.append(verdict); reviewer_replies.append((f"Phase B round {rnd}", reply))

            # harbor analyze (official TB3 check): must RUN; then the 3-state gate — only a 🔴
            # hard_red criterion (task_specification/non_clericalness; unanimous fail across ALL
            # analyzed trajectories) or ANY reward_hacking fail hard-blocks; a 🟡 (mixed) goes to
            # the reviewer to adjudicate (identical to Stage-2 Phase C). The Task QA SOTA checks
            # gate the same way: 🔴 (every breaking trajectory flagged) blocks, 🟡 → reviewer.
            a_ran = bool(analyze and analyze.ran)
            a_blocks = analyze.gate_blocks(cfg.analyze_hard_red, cfg.analyze_hard_anyfail) if a_ran else []
            tqa_ran = bool(tqa and tqa.ran)
            tqa_red = tqa.red_checks if tqa_ran else []
            tqa_error = tqa.error_checks if tqa else []   # checks unverifiable (all trajectories errored)
            gate_ok = (trials.is_breaking and oracle is not None and oracle.passed
                       and nop is not None and not nop.passed
                       and a_ran and not a_blocks
                       and (not tqa_attempted or (tqa_ran and not tqa_red and not tqa_error))
                       and review.approved(verdict))
            last_verify = {
                "round": rnd, "verify_attempt": verify_attempts,
                "breaking": trials.is_breaking, "rewards": trials.rewards,
                "oracle": (oracle.reward if oracle else None),
                "nop": (nop.reward if nop else None),
                "analyze_ran": a_ran, "analyze_blocks": a_blocks,
                # full per-trajectory criterion outcomes + explanations live in
                # qa/harbor/analyze-final-*.json; this is the rollup view.
                "analyze_colors": (analyze.colors if analyze else None),
                "analyze_models": (analyze.models if analyze else None),
                "analyze_n_trials": (analyze.n_trials if analyze else None),
                "tqa_ran": tqa_ran, "tqa_colors": (tqa.colors if tqa else None),
                "tqa_red": tqa_red, "tqa_error": tqa_error,
                "fairness_verdict": {k: verdict.get(k) for k in
                                     ("verdict", "realistic", "expert_solvable",
                                      "non_contrived", "fails_for_fair_reason")},
                "verdict": verdict.get("verdict"),
                "fails_for_fair_reason": verdict.get("fails_for_fair_reason"),
                "gate_ok": gate_ok}
            qa_report["verify"].append(last_verify)
            _flush_qa()
            # Publish the verify outcome to the state ledger so the monitor (and anyone
            # reading state.json) sees the real Phase-B result, not just qa_report.
            set_stage(run_dir, STAGE, breaking=trials.is_breaking,
                      oracle_reward=(oracle.reward if oracle else None),
                      nop_reward=(nop.reward if nop else None),
                      verdict=verdict.get("verdict"), gate_ok=gate_ok)
            print(f"[stage3 r{rnd}] verify breaking={trials.is_breaking} "
                  f"oracle={oracle.reward if oracle else '-'} nop={nop.reward if nop else '-'} "
                  f"analyze_ran={a_ran} analyze_blocks={a_blocks} "
                  f"tqa={'(off)' if not tqa_attempted else (tqa.colors if tqa_ran else 'NOT-RUN')} "
                  f"verdict={verdict.get('verdict') if verdict else '-'} gate_ok={gate_ok}", flush=True)

            # Hard stop: analyze was required (task breaks) but failed to run after retries —
            # tooling/infra, not builder-fixable. Stop loudly instead of shipping without it.
            if trials.is_breaking and not a_ran:
                summary = ("Stage 3: harbor analyze (REQUIRED hard gate) failed to run — "
                           f"{analyze.summary if analyze else '(not run)'}. Fix tooling, then resume.")
                print(f"[stage3 r{rnd}] {summary}", flush=True)
                snap = snapshot(run_dir, STAGE, task_dir, report=summary,
                                extra={"stage3-qa-report.json": workdir / "stage3-qa-report.json",
                                       **_also_extra()})
                _qa_evidence(cfg, run_dir, snap, last_check, last_autoqa, verify=last_verify,
                             also_extra=_also_extra(), tqa_suite=last_tqa_suite)
                set_stage(run_dir, STAGE, status="needs_human", snapshot=str(snap))
                costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
                return {"status": "needs_human", "summary": summary, "snapshot": str(snap)}

            # Hard stop: Task QA SOTA checks configured but unverifiable — EITHER nothing ran,
            # OR a specific check got no valid verdict on ANY trajectory (all errored). An
            # unverifiable check must NEVER pass silently as green → fail-closed (retry already
            # attempted per trajectory).
            if tqa_attempted and (not tqa_ran or tqa_error):
                why = (f"check(s) {tqa_error} produced no valid verdict on ANY trajectory (all errored)"
                       if tqa_error else "no (check, trajectory) produced a verdict")
                summary = ("Stage 3: Task QA SOTA-trajectory checks (REQUIRED gate) unverifiable — "
                           f"{why}. {tqa.summary[:300] if tqa else '(not run)'}. "
                           "Check task QA CLI install / QA_JUDGE_OPENAI_API_KEY, then resume.")
                print(f"[stage3 r{rnd}] {summary}", flush=True)
                snap = snapshot(run_dir, STAGE, task_dir, report=summary,
                                extra={"stage3-qa-report.json": workdir / "stage3-qa-report.json",
                                       **_also_extra()})
                _qa_evidence(cfg, run_dir, snap, last_check, last_autoqa, verify=last_verify,
                             also_extra=_also_extra(), tqa_suite=last_tqa_suite)
                set_stage(run_dir, STAGE, status="needs_human", snapshot=str(snap))
                costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
                return {"status": "needs_human", "summary": summary, "snapshot": str(snap)}

            if gate_ok:
                report = (f"# Stage 3 — {run_dir.name}\n\n"
                          f"harbor check: PASS ✅\n"
                          f"AutoQA ({cfg.autoqa_mode}): "
                          f"{last_autoqa.overall if last_autoqa else 'disabled'} ✅\n"
                          f"Task QA LLM checks: "
                          f"{'all passed' if (last_tqa_suite and last_tqa_suite.acceptable) else 'disabled'} ✅\n"
                          f"Break re-verify: rewards={trials.rewards} "
                          f"({trials.passes}/{trials.n} passed) ✅\n"
                          f"Oracle={oracle.reward} ✅  No-op={nop.reward} ✅\n"
                          f"harbor analyze: ran ✅  no unfair flags ✅\n"
                          f"Task QA SOTA checks: "
                          f"{tqa.colors if tqa_ran else 'disabled'} (no 🔴) ✅\n"
                          f"Fairness reviewer: {verdict.get('verdict')} "
                          f"(fails_for_fair_reason={verdict.get('fails_for_fair_reason')})\n"
                          f"analyze_review: {verdict.get('analyze_review')}\n\n"
                          f"This is the deliverable: snapshots/after-stage3/task\n")
                snap = snapshot(run_dir, STAGE, task_dir, report=report,
                                extra={"stage3-qa-report.json": workdir / "stage3-qa-report.json",
                                       **_also_extra()})
                qa_dir = _qa_evidence(cfg, run_dir, snap, check, autoqa, verify=last_verify,
                                      also_extra=_also_extra(), tqa_suite=tqa_suite)
                set_stage(run_dir, STAGE, status="ok", snapshot=str(snap), qa_evidence=str(qa_dir))
                costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
                return {"status": "ok", "summary": report, "snapshot": str(snap)}

            # Phase-B HARD fail → loop the builder unless the budget is exhausted.
            if verify_attempts >= cfg.stage3_verify_max_rounds or rnd == cfg.stage3_max_rounds:
                break
            await builder.send(_verify_feedback(verify_attempts, cfg.stage3_verify_max_rounds,
                                                trials, oracle, nop, verdict, a_blocks, tqa=tqa))

        # ---- Non-convergence → SALVAGE with a reviewer verdict, then needs_human ----
        if not verdicts:  # Phase B never produced a verdict (QA never cleared, or never broke)
            try:
                if not reviewer_primed.done():
                    await reviewer_primed
                reason = ("QA did not pass within the round budget" if last_verify is None
                          else "the break/fairness verification did not pass within the budget")
                verdict, reply = await review.ask_reviewer(reviewer, _salvage_review_msg(task_rel, reason))
                verdict["_phase"] = "stage3-salvage"
                verdicts.append(verdict); reviewer_replies.append(("Salvage review", reply))
                _flush_qa()
            except Exception as e:
                print(f"[stage3] salvage reviewer call failed: {e}", flush=True)
        salvage = verdicts[-1] if verdicts else {}
        summary = (f"# Stage 3 — {run_dir.name} (NOT accepted)\n\n"
                   f"Did not converge to QA-clean + breaking + fair within {cfg.stage3_max_rounds} "
                   f"rounds / {cfg.stage3_verify_max_rounds} verify attempts.\n"
                   f"Last verify: {json.dumps(last_verify, default=str)}\n"
                   f"Salvage reviewer verdict: {salvage.get('verdict')} "
                   f"(realistic={salvage.get('realistic')}, expert_solvable={salvage.get('expert_solvable')}, "
                   f"non_contrived={salvage.get('non_contrived')}, "
                   f"fails_for_fair_reason={salvage.get('fails_for_fair_reason')})\n"
                   f"analyze_review: {salvage.get('analyze_review')}\n"
                   f"reasons: {json.dumps(salvage.get('reasons', []), default=str)}\n")
        if not reviewer_primed.done():
            reviewer_primed.cancel()
        snap = snapshot(run_dir, STAGE, task_dir, report=summary,
                        extra={"stage3-qa-report.json": workdir / "stage3-qa-report.json",
                               **_also_extra()})
        qa_dir = _qa_evidence(cfg, run_dir, snap, last_check, last_autoqa, verify=last_verify,
                              also_extra=_also_extra(), tqa_suite=last_tqa_suite)
        set_stage(run_dir, STAGE, status="needs_human", snapshot=str(snap), qa_evidence=str(qa_dir))
        costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
        return {"status": "needs_human", "summary": summary, "snapshot": str(snap)}


def main() -> int:
    """Standalone entry: lock + guard, resolve the run dir, run the stage,
    then (on success and unless --no-gate) the final human gate."""
    ap = argparse.ArgumentParser(description="Stage 3 — QA a TB3 task without reducing difficulty.")
    ap.add_argument("run", help="run dir or slug (under runs/)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe the stage3 workdir and re-seed from the stage2 snapshot "
                         "(default: resume — keep task + progress, refresh skills/CLAUDE)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    runguard.guard(cfg, stage=STAGE, slug=str(args.run))
    run_dir = find_run_dir(cfg, args.run)
    try:
        result = asyncio.run(run(cfg, run_dir, fresh=args.fresh))
    except SessionError as e:
        msg = f"Stage 3 aborted — the agent session failed: {e}\n{session_error_hint(e)}"
        print(f"[stage3] {msg}", flush=True)
        set_stage(run_dir, STAGE, status="error", error=str(e))
        return 1
    print(f"\n[stage3] status={result['status']}  snapshot={result.get('snapshot')}", flush=True)
    if result["status"] == "ok" and not args.no_gate:
        gate(cfg, run_dir, STAGE, result["summary"])
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
