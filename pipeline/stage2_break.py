#!/usr/bin/env python3
"""Stage 2 — make the task genuinely model-breaking, and keep it fair. The hard stage.

Three phases, each a Python-owned loop with the builder agent on one side and
harbor trials on the other:

  Phase A (break + fairness)  — harden until the SOTA model fails EVERY trial
                                AND the independent fairness reviewer approves.
                                No give-up option: each round the builder must
                                design a DIFFERENT conceptual fork.
  Phase B (restore)           — bring the oracle back to 1.0 and the no-op
                                below 1.0, WITHOUT losing the break.
  Phase C (accept gate)       — the expensive one, once: multi-model trials +
                                harbor analyze over all trajectories + Task QA
                                SOTA checks + final reviewer pass. Hard blocks
                                loop the builder; greens/yellows go to the
                                reviewer to adjudicate.

Two things this module is strict about (they define the stage's soul):
  * ONLY the conductor's trials decide "breaking" — never the builder's own
    harness, never its self-assessment.
  * Errored trials are NO SIGNAL (infra), not breaks; and any reward-hack
    finding disqualifies regardless of difficulty.

The conductor owns both persistent sessions here (builder + read-only
reviewer, re-messaged each round so it judges deltas).

Run standalone (after stage 1):
    python pipeline/stage2_break.py <run-dir-or-slug>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import costs, dataset, harbor, review, runguard, task_qa
from lib.common import build_claude_md, load_prompt, reviewer_system_prompt, subs
from lib.config import load_config
from lib.orchestration import (collect_qa_evidence, find_run_dir, gate,
                               latest_task_snapshot, provision_stage, set_stage,
                               snapshot)
from lib.sdk import AgentSession, SessionError, session_error_hint

STAGE = "stage2"
BUILDER_SYSTEM = (
    "You are the Stage 2 hardening agent in a TB3 task-generation pipeline. The following rules are "
    "ABSOLUTE and override any contrary instinct — they hold even if your context was summarized and "
    "you have lost earlier detail:\n"
    "1. YOUR ONLY JOB is to make the task FAIL EVERY trial of the SOTA model the conductor runs. The "
    "bar is the conductor's GPT-5.5 trial — NOT your local harness. Your harness proving a naive/default "
    "solver fails does NOT mean GPT-5.5 fails: it reasons toward your oracle, not the naive path. A "
    "passing trial means it is still too easy — keep going.\n"
    "2. DO NOT run local scripts/harnesses to 'verify' a break, and NEVER self-certify — never declare "
    "the task breaking/done/'no action needed'/submitted on your own validation. Only the conductor's "
    "trial is the verdict. Reason HONESTLY about whether your lever truly defeats GPT-5.5 (high "
    "confidence from honest reasoning is fine); then COMMIT your chosen lever to ./task and hand back — "
    "do not re-check work you already did.\n"
    "3. NO QA/polish. Do NOT fix/reword docs, instruction.md, comments, or 'update the docs to match'; "
    "do NOT add or verify canaries (IGNORE the canary cardinal rule in CLAUDE.md this stage — Stage 3 "
    "enforces it), regenerate fixtures for cleanliness, complete task.toml metadata (beyond the "
    "difficulty/realism justification), or run `harbor check`/`analyze`. Stage 3 owns ALL of that. The "
    "ONLY upkeep you owe: keep the oracle at 1.0 and the no-op <1.0, and don't open a reward-hack hole.\n"
    "4. BREAK WITH CREATIVITY ACROSS EVERY ARENA — not by scaling the recipe (more entities/steps/bigger "
    "numbers stays solvable). EVERY task is breakable with enough understanding; if a lever gets SOLVED you "
    "simply have not found the right angle yet — NEVER conclude a task 'can't be broken', think harder and "
    "differently. Reason across ALL the arenas: the difficulty crux + what is SPECIAL about THIS variant that "
    "a strong-but-generalist agent gets wrong; the FULL lever menu (the lever catalog + your SKILL); what "
    "these agents reliably get RIGHT (don't fight that) vs where they are UNCERTAIN and slip (exploit that). "
    "UNDERSTAND why most levers fail — this is reality to design around, NOT a rule to satisfy: a SOTA model "
    "IMPLEMENTS any rule you can state precisely AND self-tests aggressively; if it can build a check that "
    "would catch its OWN mistake, it self-corrects and the task does NOT break. So breaks LAND only when the "
    "model cannot cheaply tell it is wrong: its OWN checks (metric/audit/uniqueness/property tests) come up "
    "GREEN on the wrong answer, and the catch is discoverable in the AGENT-VISIBLE DATA (a diligent expert "
    "finds it), never hidden only in tests/. The STRONGEST shape is an OMISSION — the model ships a "
    "complete-LOOKING answer and stops, never realizing a further step a responsible expert would take "
    "('make the default move wrong' is one way; 'make it stop one expert step short with its own checks "
    "green' is usually stronger). You MAY rework the task freely and play right at the fair/unfair BOUNDARY; "
    "the bar is a responsible, accountable, well-paid expert who acts diligently and SOLVES it — keep it "
    "solvable for them, and make harbor analyze pass by documenting THAT expert's path in task.toml's "
    "difficulty_explanation. Keep designing a different angle every round; there is no 'give up'. See your "
    "SKILL '## Think across every arena' + the lever catalog for the full method.\n"
    "5. YOU MAY FUNDAMENTALLY REWORK THE TASK. The proposal and the Stage-1 difficulty crux are a "
    "STARTING POINT, not a cage. Adhere to the proposal's spirit when you can, but if breaking the model "
    "requires it, you may significantly restructure the task — change the premise, swap the crux, replace "
    "the scenario/data/verifier, redesign the environment, or remove Stage-1 leaks (a verbatim reference "
    "solution, the exact grader, a benchmark mirroring the held-out metric) — as long as the result stays "
    "FAIR: a complete domain expert could still solve it from instruction.md + the agent-visible "
    "environment, the scenario is realistic (niche is fine if justified), and difficulty is intrinsic, "
    "not contrived. Straying from the proposal to get a fair, model-breaking task is fair game.\n"
    "6. DURABLE STATE: maintain ./stage2-progress.md — after every experiment/lever/edit append a "
    "one-line result. At the start of EVERY turn (especially right after a compaction/continuation) "
    "read it and TRUST it over memory; never re-run an experiment or re-derive a fact already there.\n"
    "Follow .claude/skills/stage2-break-task/SKILL.md and ./CLAUDE.md for detail. Edit the task under ./task.")

# Re-injected automatically the turn after the SDK compacts the conversation (lib/sdk.py).
COMPACTION_RECOVERY_NOTE = (
    "⚠️ Your context was just compacted/summarized — you have likely lost earlier detail. Before doing "
    "ANYTHING: (1) read ./stage2-progress.md and recover your state from it; (2) do NOT re-run any "
    "experiment or re-derive any fact already recorded there; (3) your ONLY job is to make the task "
    "FAIL the SOTA model via a conceptual break. The bar is the conductor's GPT-5.5 trial, NOT your local "
    "harness — do not run scripts to 'verify' a break and do not self-certify. Do NOT 'update the docs', "
    "verify canaries/metadata, or run harbor check (that is Stage 3). (4) Keep the mental model: EVERY task "
    "is breakable — NEVER conclude otherwise; think creatively across all arenas (the crux + what is special "
    "about THIS variant, the full lever menu, where a strong generalist agent slips). The strongest break is "
    "an OMISSION the model doesn't realize it needs, with its OWN checks GREEN on the wrong answer and the "
    "catch discoverable in the agent-visible DATA (not hidden in tests/); you may rework freely and play at "
    "the fair/unfair boundary, keeping it solvable for a diligent accountable expert (document that path in "
    "difficulty_explanation). Re-read your SKILL '## Think across every arena' + the lever catalog. Continue "
    "breaking from where the progress file leaves off.")
REVIEWER_LABEL = "stage2-reviewer"
# Reviewer tool restrictions + helpers (ask/round/feedback/approved + the compaction
# recovery note) live in lib/review.py, shared with Stage 3.


def _break_feedback(trials: harbor.TrialsResult) -> str:
    """Feedback for a round where the model still PASSES. Runs a fixed per-round
    protocol: accept that the bar is the conductor's trial (not the builder's own
    harness); read the passing trajectory and find its least-certain step; run the
    capability-symmetry honesty check; commit a conceptual fork. The builder reads
    the raw trajectories itself — we deliberately send paths, not a pre-digest.

    Note there is NO give-up / escalation option here, on purpose: the builder
    designs a different fair fork every round until the model fails or the
    (agent-invisible) round cap hits."""
    model = trials.model or "the SOTA model"
    passing = [t for t in trials.trials if t.passed]
    lines = [
        f"STILL NOT MODEL-BREAKING. {model} passed: rewards={trials.rewards} → "
        f"{trials.passes}/{trials.n} PASSED. The bar is THIS trial — not your local harness. Your "
        f"harness proving a naive solver fails does NOT mean {model} fails; it reasons toward your "
        "oracle, not the naive path.",
        "",
        "Run this protocol THIS turn (do not skip steps, do not re-run old checks):",
        "1. Read ./stage2-progress.md; do not redo anything already recorded there.",
        f"2. Open the passing {model} trajectory (paths below) and find the ONE step it was least "
        "certain about — where it guessed, assumed, hand-waved, or could have gone wrong but didn't. "
        "First decide GENUINE vs HACK: did it actually solve it, or REWARD-HACK (hardcoded/echoed "
        "outputs, gamed a metric/threshold/parsing hole, wrote files that satisfy the check without "
        "doing the work, or reached solution/ or tests/)? A hack is a VERIFIER BUG — close the hole / "
        "tighten anti-cheat + outcome-verification (do NOT add difficulty), then re-confirm oracle 1.0 "
        "/ no-op <1.0.",
        "3. In ONE sentence each, write: the model's DEFAULT confident move, and your oracle's WINNING "
        "insight.",
        "4. SELF-VERIFIABILITY CHECK — reason honestly about whether the model could catch its OWN "
        "mistake (this is understanding, not a checkbox): (a) is the winning insight a single retrievable/"
        "stateable idea? If yes, the model just implements it → NOT a break. (b) Could the model build a "
        "check (property test, backtest, recompute, uniqueness/sanity check) that comes up RED on its wrong "
        "answer? If yes, it self-corrects → NOT a break — you need its OWN checks GREEN on the wrong answer. "
        "(c) Is the catch discoverable in the agent-visible DATA (fair) rather than only in tests/ (unfair)? "
        "Prefer an OMISSION — a step the model won't realize it needs (SKILL '## Think across every arena' + "
        "levers L/M/N) — over 'make the stated rule wrong'. If your lever can't clear (a)+(b), pick a "
        "DIFFERENT arena/angle before editing (NEVER conclude the task is unbreakable — it isn't).",
        "5. Make the model's default move CONFIDENTLY WRONG with NO cheap self-check, and COMMIT the edit "
        "to ./task. Do NOT scale the recipe (more entities/steps/bigger numbers stays solvable). Do NOT "
        "run local harnesses to 'verify' — reason it through honestly; you may hand back with high "
        "confidence from honest reasoning alone.",
        "6. Predict the new failure mode in ONE sentence before handing back.",
        "7. Be ADVERSARIAL — push to the borderline of fair (the reviewer adjudicates). Floor: a complete "
        "expert could still solve it from instruction.md + the env; niche is fine if justified — document "
        "why in task.toml's difficulty_explanation + design notes. Do NOT touch canaries/docs/metadata "
        "(Stage 3's job).",
        "8. YOU MAY FUNDAMENTALLY REWORK THE TASK. The proposal/crux is a starting point, not a cage. If "
        "the current framing keeps being solvable, change it: swap the crux, restructure the scenario, "
        "replace data/verifier, redesign the environment, or remove Stage-1 leaks (a verbatim reference "
        "solution, the exact grader, a held-out-equivalent benchmark). Straying from the proposal is fair "
        "game as long as the result stays FAIR (expert-solvable from instruction.md + the env; realistic, "
        "niche-ok; intrinsic difficulty). Keep the oracle 1.0 / no-op <1.0.",
        "",
        "There is NO give-up option: keep designing a DIFFERENT conceptual fork every round. Do not repeat "
        "a defeated lever; build on what the trajectory taught you.",
        "",
        "Passing-run trajectories to study (read these yourself — no pre-digest):",
    ]
    if passing:
        for i, t in enumerate(passing, 1):
            d = t.run_dir
            lines.append(f"  trial {i} (reward={t.reward}):")
            if d is not None:
                lines.append(f"    readable transcript  : {d / 'agent' / 'codex.txt'}")
                lines.append(f"    structured trajectory: {d / 'agent' / 'trajectory.json'}")
                lines.append(f"    full trial dir       : {d}  "
                             "(also has agent/sessions/.../rollout-*.jsonl and verifier/)")
            elif t.trajectory:
                lines.append(f"    trajectory: {t.trajectory}")
    else:
        lines.append(f"  (no passing trajectory captured; trial job dir: {trials.job_dir})")
    lines += [
        "",
        f"End with: genuine-vs-hack for each pass, the default move + oracle insight, your "
        f"capability-symmetry verdict, the conceptual lever applied, files changed, and the predicted "
        f"failure mode.",
    ]
    return "\n".join(lines)


def _final_fix_feedback(verdict, oracle, nop, trials, a_blocks=None, tqa=None) -> str:
    """Phase C feedback when the final gate failed: name EVERY failing leg
    (break / oracle / no-op / analyze hard-blocks / TQA reds / reviewer verdict)
    and demand fixes WITHOUT weakening the break in any way.

    ``verdict`` may be None/empty when the reviewer was never consulted because
    the gate died earlier on a mechanical or analyze hard-block."""
    lines = ["FINAL GATE FAILED — fix this WITHOUT reducing difficulty or weakening the break "
             "in any way (no clarifying comments, error text, readability hints, or loosened "
             "thresholds), and keep the oracle at 1.0 and the no-op < 1.0."]
    if not trials.is_breaking:
        lines.append(f"- The task is NO LONGER model-breaking: rewards={trials.rewards} "
                     f"({trials.passes}/{trials.n} passed). Re-harden the conceptual fork.")
    if not oracle.passed:
        lines.append(f"- Oracle regressed (reward={oracle.reward}, need 1.0) — repair solve.sh/verifier.")
    if nop.passed:
        lines.append(f"- No-op now passes (reward={nop.reward}) — make the task require real work again.")
    if a_blocks:
        lines.append(f"- harbor analyze (official TB3 check, run across MULTIPLE models) flagged the "
                     f"task UNFAIR/hackable UNANIMOUSLY: {a_blocks}. If 'task_specification' (🔴 every "
                     "model's trajectory failed it): the spec is inconsistent with the verifier or not "
                     "derivable from instruction.md + the agent-visible env — fix the spec (precision, "
                     "not hints). If 'reward_hacking': close the verifier hole. If 'non_clericalness': "
                     "the model substantively solved the task and was failed on FORM (formatting/"
                     "precision/naming/ordering) — move the difficulty into the concept and make the "
                     "output contract mechanically checkable, do NOT keep a clerical gotcha as the break.")
    if tqa is not None and tqa.red_checks:
        lines.append(f"- Task QA SOTA-trajectory checks flagged 🔴 on EVERY breaking trajectory: "
                     f"{tqa.red_checks}. If 'false-negatives': the break leans on a value/convention "
                     "the agent provably could NOT derive from instruction.md + the agent-visible env "
                     "(confirmed in its trajectory), or the tests reject valid solutions on brittle "
                     "matching — make the requirement derivable (precision, not hints) or fix the "
                     f"brittle test. Per-trajectory findings: {tqa.out_dir}/")
    if verdict and (str(verdict.get("verdict", "")).lower() != "approve" or not review.approved(verdict)):
        lines.append("- Fairness reviewer said REVISE:")
        lines.append(review.reviewer_feedback(verdict))
    lines.append("End with a status.")
    return "\n".join(lines)


def _oracle_restore(oracle: harbor.RunResult, nop: harbor.RunResult, broke_after_fix=False) -> str:
    """Phase B feedback when oracle/no-op aren't restored yet: show current
    rewards, attach the verifier output if the oracle regressed, and remind
    the builder that restoring validation must NOT make the task easier."""
    lines = ["Phase B — restore validation while staying model-breaking.",
             f"- oracle reward = {oracle.reward} (need 1.0)",
             f"- no-op reward  = {nop.reward} (need < 1.0)"]
    if not oracle.passed:
        lines.append("ORACLE FAILED — your hardening broke the reference solution. Repair "
                     "solve.sh / the verifier so the genuine solution scores 1.0 again.")
        if oracle.exception:
            lines.append(f"Trial exception: {oracle.exception}")
        if oracle.test_stdout:
            lines.append("Verifier stdout (truncated):\n" + oracle.test_stdout)
    if nop.passed:
        lines.append("NO-OP PASSED — the empty submission now scores ≥1.0; make the task require real work again.")
    if broke_after_fix:
        lines.append("NOTE: a previous oracle fix made the task solvable by GPT-5.5 again. Restore "
                     "oracle/nop WITHOUT making it easier for SOTA.")
    lines.append("CRITICAL: do not reduce difficulty. End with a status.")
    return "\n".join(lines)


async def run(cfg, run_dir: Path, fresh: bool = False) -> dict:
    """Stage 2 entrypoint: the full Phase A → B → C state machine.

    Owns both agent sessions (builder + reviewer), the break-trial cache, and
    every terminal path. Each phase ends in one of:
      * advanced to the next phase,
      * needs_human (round budget exhausted / gate tooling failed),
      * error    (all trials errored = infra, or a session failure).
    Terminal paths all do the same dance: flush artifacts → snapshot → bundle
    QA evidence → finalize costs. Returns {status, summary, snapshot}.
    """
    run_dir = Path(run_dir)
    seed = latest_task_snapshot(run_dir, "stage1")
    if not seed.exists():
        return {"status": "error", "summary": f"no stage1 snapshot at {seed}; run stage 1 first."}

    notes_src = run_dir / "snapshots" / "after-stage1" / "stage1-design-notes.md"
    workdir = provision_stage(
        cfg, run_dir, STAGE, ["stage2-break-task"],
        seed_task=seed,
        inputs={"proposal.md": run_dir / "00-proposal.md", "stage1-design-notes.md": notes_src},
        claude_md=build_claude_md(cfg, "Stage 2 — Make it model-breaking (fairly)"),
        substitutions=subs(cfg),
        fresh=fresh,
    )
    resumed = (workdir / "stage2-progress.md").exists() and not fresh
    print(f"[stage2] {'RESUMED existing workdir (kept task + progress)' if resumed else 'fresh workdir'}"
          f": {workdir}", flush=True)
    task_dir = workdir / "task"
    task_rel = "./task"
    jobs_root = run_dir / "harbor" / STAGE
    jobs_root.mkdir(parents=True, exist_ok=True)
    blog = run_dir / "logs" / f"{STAGE}.builder.log"
    rlog = run_dir / "logs" / f"{STAGE}.reviewer.log"
    set_stage(run_dir, STAGE, status="running", workdir=str(workdir))
    cost = costs.StageCost(STAGE)
    # Reuse break trials across phases when the task is byte-identical (fingerprint-keyed):
    # the same gpt-5.5 break otherwise re-runs in Phase A (last breaking round), Phase B
    # (re-confirm), and Phase C (accept gate) with no edit in between. A hit reuses the SAME
    # trajectories so analyze + Task QA judge identical run dirs at zero extra trial cost.
    bcache = harbor.BreakCache(cfg.reuse_break_trajectories)

    verdicts: list[dict] = []
    reviewer_replies: list[tuple] = []   # (tag, full reply text) — persisted in full
    break_report: dict = {"phaseA": [], "phaseB": [], "final": None}
    verdicts_file = workdir / "stage2-reviewer-verdicts.json"
    replies_file = workdir / "stage2-reviewer-replies.md"

    def _flush_artifacts():
        verdicts_file.write_text(json.dumps(verdicts, indent=2, default=str))
        (workdir / "stage2-break-report.json").write_text(json.dumps(break_report, indent=2, default=str))
        if reviewer_replies:
            replies_file.write_text(review.replies_markdown(reviewer_replies))

    # Files copied into every snapshot so each is self-contained (analyze JSON is bundled
    # separately by collect_qa_evidence from harbor/stage2/*.json).
    def _snap_extra() -> dict:
        extra = {"stage2-reviewer-verdicts.json": verdicts_file,
                 "stage2-break-report.json": workdir / "stage2-break-report.json"}
        if replies_file.exists():
            extra["stage2-reviewer-replies.md"] = replies_file
        return extra

    async with AgentSession(cwd=workdir, model=cfg.model, system_append=BUILDER_SYSTEM,
                            log_path=blog, label="stage2-builder",
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

        last_reply = await builder.send(load_prompt(cfg, "stage2_builder_kickoff.md"))
        # Prime the reviewer (pre-load TB3 docs) in the BACKGROUND so the first GPT-5.5
        # trial isn't gated behind it. Trials lead; the reviewer is only engaged on a
        # breaking round — we await this prime right before that first verdict.
        reviewer_primed = asyncio.create_task(reviewer.send(load_prompt(cfg, "reviewer_kickoff.md")))

        def _infra_abort(phase: str, rnd: int, tr) -> dict:
            """All break trials in this round errored — an infrastructure failure
            (bad/expired API key, OOM, timeout), NOT a model break. Abort loudly so
            the user fixes the infra instead of chasing a phantom break."""
            if not reviewer_primed.done():
                reviewer_primed.cancel()
            msg = (f"Stage 2 aborted in Phase {phase} round {rnd}: all break trials ERRORED — no "
                   f"model signal, this is an infrastructure failure (NOT a break). "
                   f"{tr.error_summary}. Common causes: invalid/expired API key, OOM, or timeout. "
                   f"Fix the infra and re-run. Failing job dir: {tr.job_dir}")
            print(f"[stage2.{phase} r{rnd}] ALL TRIALS ERRORED — infra failure, aborting. "
                  f"{tr.error_summary}", flush=True)
            _flush_artifacts()
            snap = snapshot(run_dir, STAGE, task_dir, report=msg, extra=_snap_extra())
            collect_qa_evidence(run_dir, STAGE, snap)
            set_stage(run_dir, STAGE, status="error", error=msg, snapshot=str(snap))
            costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
            return {"status": "error", "summary": msg, "snapshot": str(snap)}

        # ---- Phase A: break + fairness --------------------------------
        approved = False
        for rnd in range(1, cfg.stage2_break_max_rounds + 1):
            staged, _reused = await asyncio.to_thread(bcache.run_staged, cfg, task_dir, jobs_root)
            if not _reused:
                for r in staged.runs:
                    cost.add_harbor(r)
            trials = staged.result
            print(f"[stage2.A r{rnd}] probe={staged.probe.rewards}"
                  + (f" +confirm={staged.confirm.rewards}" if staged.escalated else "")
                  + f" breaking={trials.is_breaking}", flush=True)
            set_stage(run_dir, STAGE, phase="A", round=rnd, rewards=trials.rewards,
                      breaking=trials.is_breaking, escalated=staged.escalated)
            costs.record_live(cost, run_dir, STAGE, builder, reviewer)
            break_report["phaseA"].append({"round": rnd, "probe": staged.probe.rewards,
                                           "confirm": (staged.confirm.rewards if staged.escalated else None),
                                           "breaking": trials.is_breaking, "job_dir": str(trials.job_dir)})
            _flush_artifacts()
            if trials.all_errored:                       # infra failure, not a break or a pass
                return _infra_abort("A", rnd, trials)
            if not trials.is_breaking:
                if rnd == cfg.stage2_break_max_rounds:
                    break
                last_reply = await builder.send(_break_feedback(trials))
                continue
            # breaking → reviewer judges fairness on its OWN reading. NO harbor analyze in Phase A
            # (analyze is a Phase-C-only, multi-model signal) — recapture main's reviewer freedom.
            await reviewer_primed  # ensure the background prime finished before the first verdict
            verdict, reply = await review.ask_reviewer(
                reviewer, review.reviewer_round(cfg, run_dir, task_rel, trials, None, last_reply))
            verdict["_phase"] = "A"; verdict["_round"] = rnd
            verdicts.append(verdict); reviewer_replies.append((f"Phase A round {rnd}", reply))
            _flush_artifacts()
            print(f"[stage2.A r{rnd}] reviewer verdict={verdict.get('verdict')}", flush=True)
            if review.approved(verdict):
                approved = True
                break
            last_reply = await builder.send(review.reviewer_feedback(verdict))

        if not reviewer_primed.done():  # never hit a breaking round → drain the bg prime
            reviewer_primed.cancel()

        if not approved:
            summary = (f"Stage 2 Phase A did not reach breaking+fair within "
                       f"{cfg.stage2_break_max_rounds} rounds — this NEEDS HUMAN CREATIVITY (the task is "
                       f"not 'unbreakable'; every task is breakable with the right angle — the agent ran "
                       f"out of rounds, not options). See logs + verdicts for the angles already tried.")
            snap = snapshot(run_dir, STAGE, task_dir, report=summary, extra=_snap_extra())
            collect_qa_evidence(run_dir, STAGE, snap)
            dataset.capture_break_failure(cfg, run_dir, STAGE, status="needs_human",
                                          summary=summary, verdicts=verdicts,
                                          break_report=break_report, task_dir=task_dir,
                                          snapshot_dir=snap)
            set_stage(run_dir, STAGE, status="needs_human", snapshot=str(snap))
            costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
            return {"status": "needs_human", "summary": summary, "snapshot": str(snap)}

        # ---- Phase B: restore oracle/nop, stay breaking ----------------
        phaseB_ok = False
        last_trials = None
        for rnd in range(1, cfg.stage2_oracle_max_rounds + 1):
            oracle = await asyncio.to_thread(harbor.run_oracle, cfg, task_dir, jobs_root)
            nop = await asyncio.to_thread(harbor.run_nop, cfg, task_dir, jobs_root)
            cost.add_harbor(oracle, nop)
            print(f"[stage2.B r{rnd}] oracle={oracle.reward} nop={nop.reward}", flush=True)
            set_stage(run_dir, STAGE, phase="B", round=rnd,
                      oracle_reward=oracle.reward, nop_reward=nop.reward)
            costs.record_live(cost, run_dir, STAGE, builder, reviewer)
            if oracle.passed and not nop.passed:
                staged, _reused = await asyncio.to_thread(bcache.run_staged, cfg, task_dir, jobs_root)
                if not _reused:
                    for r in staged.runs:
                        cost.add_harbor(r)
                last_trials = staged.result
                break_report["phaseB"].append({"round": rnd, "oracle": oracle.reward, "nop": nop.reward,
                                               "rewards": last_trials.rewards,
                                               "breaking": last_trials.is_breaking})
                _flush_artifacts()
                print(f"[stage2.B r{rnd}] re-break rewards={last_trials.rewards} "
                      f"breaking={last_trials.is_breaking}", flush=True)
                if last_trials.all_errored:              # infra failure, can't confirm the break
                    return _infra_abort("B", rnd, last_trials)
                if last_trials.is_breaking:
                    phaseB_ok = True
                    break
                if rnd == cfg.stage2_oracle_max_rounds:
                    break
                last_reply = await builder.send(_break_feedback(last_trials)
                                                + "\n(Oracle/no-op are passing; keep them passing while you re-harden.)")
            else:
                break_report["phaseB"].append({"round": rnd, "oracle": oracle.reward, "nop": nop.reward})
                _flush_artifacts()
                if rnd == cfg.stage2_oracle_max_rounds:
                    break
                last_reply = await builder.send(_oracle_restore(oracle, nop))

        if not phaseB_ok:
            summary = (f"Stage 2 Phase B failed to restore oracle/no-op while staying breaking "
                       f"within {cfg.stage2_oracle_max_rounds} rounds.")
            snap = snapshot(run_dir, STAGE, task_dir, report=summary, extra=_snap_extra())
            collect_qa_evidence(run_dir, STAGE, snap)
            dataset.capture_break_failure(cfg, run_dir, STAGE, status="needs_human",
                                          summary=summary, verdicts=verdicts,
                                          break_report=break_report, task_dir=task_dir,
                                          snapshot_dir=snap)
            set_stage(run_dir, STAGE, status="needs_human", snapshot=str(snap))
            costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
            return {"status": "needs_human", "summary": summary, "snapshot": str(snap)}

        # ---- Phase C: final ACCEPT gate (multi-model analyze + reviewer) + fix loop -------
        # Per round: re-confirm the gpt-5.5 break (staged 1->3) + oracle/no-op. Only once those
        # hold ("accept-time") do we pay for the MULTI-MODEL matrix: run the extra models, run
        # `harbor analyze` over ALL trajectories, and aggregate the official 3-state colors.
        #   • analyze fails to RUN on every model           -> hard stop (tooling/keys).
        #   • 🔴 task_specification / ANY reward_hacking fail -> HARD block, loop the builder.
        #   • otherwise (greens + yellows)                   -> the reviewer adjudicates (with the
        #     3-state colors as context); its verdict is the gate.
        final_ok = False
        oracle = nop = None
        verdict: dict = {}
        analyze = None
        a_ran = False
        a_blocks: list = []
        tqa = None
        tqa_ran = False
        tqa_red: list = []
        tqa_error: list = []
        for frnd in range(1, cfg.stage2_final_max_rounds + 1):
            staged, _reused = await asyncio.to_thread(bcache.run_staged, cfg, task_dir, jobs_root)
            if not _reused:
                for r in staged.runs:
                    cost.add_harbor(r)
            last_trials = staged.result
            if last_trials.all_errored:
                return _infra_abort("C", frnd, last_trials)
            oracle = await asyncio.to_thread(harbor.run_oracle, cfg, task_dir, jobs_root)
            nop = await asyncio.to_thread(harbor.run_nop, cfg, task_dir, jobs_root)
            cost.add_harbor(oracle, nop)
            mech_ok = last_trials.is_breaking and oracle.passed and not nop.passed
            set_stage(run_dir, STAGE, phase="C", round=frnd, rewards=last_trials.rewards,
                      breaking=last_trials.is_breaking, oracle_reward=oracle.reward,
                      nop_reward=nop.reward)
            print(f"[stage2.C r{frnd}] breaking={last_trials.is_breaking} oracle={oracle.reward} "
                  f"nop={nop.reward} mech_ok={mech_ok}", flush=True)

            if not mech_ok:
                # Not at accept-time — skip the expensive matrix; loop the builder to restore.
                analyze = None; a_ran = False; a_blocks = []
                tqa = None; tqa_ran = False; tqa_red = []; tqa_error = []
                break_report["final"] = {"round": frnd, "rewards": last_trials.rewards,
                                         "oracle": oracle.reward, "nop": nop.reward,
                                         "breaking": last_trials.is_breaking, "matrix_ran": False}
                _flush_artifacts(); costs.record_live(cost, run_dir, STAGE, builder, reviewer)
                if frnd == cfg.stage2_final_max_rounds:
                    break
                last_reply = await builder.send(_final_fix_feedback(None, oracle, nop, last_trials))
                continue

            # Accept-time: build the analyze set = the gpt-5.5 break trajectories PLUS each extra
            # model's k trials, then analyze ALL of them for the cross-model 3-state coloring.
            # Collect the per-trial run_dirs of EVERY model in lockstep, so the Task QA SOTA
            # checks below judge the SAME multi-model trajectory set analyze sees (not just the
            # gpt-5.5 break trials) — true parity, and a stricter only-🔴 bar (more trajectories
            # must agree to block).
            matrix_jobs = [(last_trials.model or "break-model", staged.probe.job_dir)]
            if staged.escalated and staged.confirm is not None:
                matrix_jobs.append((last_trials.model or "break-model", staged.confirm.job_dir))
            sota_run_dirs = [t.run_dir for t in last_trials.trials if t.run_dir and not t.errored]
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
                print(f"[stage2.C r{frnd}] matrix {tag}: rewards={tr.rewards} "
                      f"breaking={tr.is_breaking}", flush=True)
            analyze = await asyncio.to_thread(
                harbor.run_analyze_matrix, cfg, matrix_jobs,
                run_dir / "harbor" / STAGE / f"analyze-final-{frnd}.json",
                cfg.analyze_matrix_failing)
            cost.add_harbor(analyze)
            a_ran = analyze.ran
            a_blocks = (analyze.gate_blocks(cfg.analyze_hard_red, cfg.analyze_hard_anyfail)
                        if a_ran else [])
            # Task QA SOTA-trajectory checks (false-negatives, …) over the SAME multi-model
            # trajectory set analyze just used — injected via TASK_QA_SOTA_RUN_DIR (no fresh
            # SOTA trial). 3-state like analyze: only 🔴 (EVERY trajectory flagged) hard-blocks;
            # 🟡 is reviewer context.
            tqa = None
            tqa_attempted = bool(cfg.tqa_enabled and cfg.tqa_stage2_checks)
            if tqa_attempted:
                tqa = await asyncio.to_thread(
                    task_qa.run_sota_check_matrix, cfg, task_dir, sota_run_dirs,
                    run_dir / "harbor" / STAGE / f"tqa-sota-final-{frnd}",
                    cfg.tqa_stage2_checks)
                cost.add_harbor(tqa)
            tqa_ran = bool(tqa and tqa.ran)
            tqa_red = tqa.red_checks if tqa_ran else []
            tqa_error = tqa.error_checks if tqa else []   # checks unverifiable (all trajectories errored)
            break_report["final"] = {"round": frnd, "rewards": last_trials.rewards,
                                     "oracle": oracle.reward, "nop": nop.reward,
                                     "breaking": last_trials.is_breaking, "matrix_ran": True,
                                     "analyze_ran": a_ran, "analyze_colors": (analyze.colors or {}),
                                     "analyze_models": analyze.models,
                                     "analyze_n_trials": analyze.n_trials, "hard_blocks": a_blocks,
                                     "tqa_ran": tqa_ran,
                                     "tqa_colors": (tqa.colors if tqa else None),
                                     "tqa_red": tqa_red, "tqa_error": tqa_error}
            _flush_artifacts()
            print(f"[stage2.C r{frnd}] analyze ran={a_ran} colors={analyze.colors} "
                  f"hard_blocks={a_blocks} (models={analyze.models}, n={analyze.n_trials})", flush=True)
            if tqa is not None:
                print(f"[stage2.C r{frnd}] tqa ran={tqa_ran} colors={tqa.colors} "
                      f"red={tqa_red} (n_traj={tqa.n_trajectories})", flush=True)

            # Hard stop: the matrix produced NO parseable analyze result on ANY model (tooling/keys).
            if not a_ran:
                summary = ("Stage 2: harbor analyze (multi-model matrix) failed to run on every "
                           f"model — {analyze.summary[:400]}. Fix tooling/keys, then resume.")
                print(f"[stage2.C r{frnd}] {summary}", flush=True)
                snap = snapshot(run_dir, STAGE, task_dir, report=summary, extra=_snap_extra())
                collect_qa_evidence(run_dir, STAGE, snap)
                set_stage(run_dir, STAGE, status="needs_human", snapshot=str(snap))
                costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
                return {"status": "needs_human", "summary": summary, "snapshot": str(snap)}

            # Hard stop: Task QA SOTA checks were configured but EITHER produced no verdict at
            # all, OR a specific check produced no valid verdict on ANY trajectory (all errored).
            # Either way it's unverifiable → fail-closed (an unverifiable check must NEVER pass
            # silently as green), like analyze-must-run. Retry already attempted per trajectory.
            if tqa_attempted and (not tqa_ran or tqa_error):
                why = (f"check(s) {tqa_error} produced no valid verdict on ANY trajectory (all errored)"
                       if tqa_error else "no (check, trajectory) produced a verdict")
                summary = ("Stage 2: Task QA SOTA-trajectory checks (REQUIRED gate) unverifiable — "
                           f"{why}. {tqa.summary[:300] if tqa else '(not run)'}. "
                           "Check task QA CLI install / QA_JUDGE_OPENAI_API_KEY, then resume.")
                print(f"[stage2.C r{frnd}] {summary}", flush=True)
                snap = snapshot(run_dir, STAGE, task_dir, report=summary, extra=_snap_extra())
                collect_qa_evidence(run_dir, STAGE, snap)
                set_stage(run_dir, STAGE, status="needs_human", snapshot=str(snap))
                costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
                return {"status": "needs_human", "summary": summary, "snapshot": str(snap)}

            # HARD block: 🔴 task_specification/non_clericalness, any reward_hacking, or a 🔴
            # Task QA SOTA check (unanimous across trajectories) → builder fixes (no reviewer).
            if a_blocks or tqa_red:
                costs.record_live(cost, run_dir, STAGE, builder, reviewer)
                if frnd == cfg.stage2_final_max_rounds:
                    break
                last_reply = await builder.send(
                    _final_fix_feedback(None, oracle, nop, last_trials, a_blocks, tqa=tqa))
                continue

            # No hard block (greens + yellows) → reviewer adjudicates, WITH the 3-state context.
            await reviewer_primed
            verdict, reply = await review.ask_reviewer(
                reviewer, review.reviewer_round(cfg, run_dir, task_rel, last_trials, analyze,
                                                "final state", final=True, oracle=oracle, nop=nop,
                                                tqa=tqa))
            verdict["_phase"] = "C"; verdict["_round"] = frnd
            verdicts.append(verdict); reviewer_replies.append((f"Phase C round {frnd}", reply))
            break_report["final"]["verdict"] = verdict.get("verdict")
            _flush_artifacts()
            final_ok = review.approved(verdict)
            print(f"[stage2.C r{frnd}] reviewer verdict={verdict.get('verdict')} "
                  f"final_ok={final_ok}", flush=True)
            costs.record_live(cost, run_dir, STAGE, builder, reviewer)
            if final_ok or frnd == cfg.stage2_final_max_rounds:
                break
            last_reply = await builder.send(review.reviewer_feedback(verdict))

        colors = (analyze.colors if analyze else None) or {}
        report = (f"# Stage 2 — {run_dir.name}\n\n"
                  f"Breaking ({last_trials.model}): rewards={last_trials.rewards} "
                  f"({last_trials.passes}/{last_trials.n} passed) "
                  f"{'✅' if last_trials.is_breaking else '❌'}\n"
                  f"Oracle={oracle.reward} {'✅' if oracle.passed else '❌'}  "
                  f"No-op={nop.reward} {'✅' if not nop.passed else '❌'}\n"
                  f"harbor analyze (matrix, models={analyze.models if analyze else None}, "
                  f"n={analyze.n_trials if analyze else 0}): colors={colors} hard_blocks={a_blocks} "
                  f"{'✅' if a_ran and not a_blocks else '❌'}\n"
                  f"Task QA SOTA checks ({cfg.tqa_stage2_checks if cfg.tqa_enabled else 'off'}): "
                  f"colors={tqa.colors if tqa else None} red={tqa_red} "
                  f"{'✅' if (not cfg.tqa_enabled or not cfg.tqa_stage2_checks or (tqa_ran and not tqa_red)) else '❌'}\n"
                  f"Final reviewer verdict: {verdict.get('verdict')} "
                  f"(realistic={verdict.get('realistic')}, expert_solvable={verdict.get('expert_solvable')}, "
                  f"non_contrived={verdict.get('non_contrived')}, "
                  f"fails_for_fair_reason={verdict.get('fails_for_fair_reason')})\n\n"
                  f"reasons: {json.dumps(verdict.get('reasons', []), indent=2)}\n")
        snap = snapshot(run_dir, STAGE, task_dir, report=report, extra=_snap_extra())
        collect_qa_evidence(run_dir, STAGE, snap, summary={
            "breaking_rewards": last_trials.rewards, "oracle": oracle.reward, "nop": nop.reward,
            "analyze_colors": colors, "analyze_models": (analyze.models if analyze else None),
            "tqa_colors": (tqa.colors if tqa else None), "tqa_red": tqa_red, "tqa_error": tqa_error,
            "final_fairness_verdict": {k: verdict.get(k) for k in
                                       ("verdict", "realistic", "expert_solvable", "non_contrived",
                                        "fails_for_fair_reason", "notes")}})
        if final_ok:
            set_stage(run_dir, STAGE, status="ok", snapshot=str(snap))
            costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
            return {"status": "ok", "summary": report, "snapshot": str(snap)}
        dataset.capture_break_failure(cfg, run_dir, STAGE, status="needs_human",
                                      summary=report, verdicts=verdicts,
                                      break_report=break_report, task_dir=task_dir,
                                      snapshot_dir=snap)
        set_stage(run_dir, STAGE, status="needs_human", snapshot=str(snap))
        costs.finalize(cost, run_dir, STAGE, sessions=[builder, reviewer], snapshot_dir=snap)
        return {"status": "needs_human",
                "summary": report + f"\nFinal gate did not fully pass within "
                           f"{cfg.stage2_final_max_rounds} rounds — human review needed.",
                "snapshot": str(snap)}


def main() -> int:
    """Standalone entry: lock + guard, resolve the run dir, run the stage,
    then (on success and unless --no-gate) the end-of-stage human gate."""
    ap = argparse.ArgumentParser(description="Stage 2 — make a TB3 task model-breaking (fairly).")
    ap.add_argument("run", help="run dir or slug (under runs/)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe the stage2 workdir and re-seed from the stage1 snapshot "
                         "(default: resume — keep the hardened task + progress, refresh skills/CLAUDE)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    runguard.guard(cfg, stage=STAGE, slug=str(args.run))
    run_dir = find_run_dir(cfg, args.run)
    try:
        result = asyncio.run(run(cfg, run_dir, fresh=args.fresh))
    except SessionError as e:
        msg = f"Stage 2 aborted — the agent session failed: {e}\n{session_error_hint(e)}"
        print(f"[stage2] {msg}", flush=True)
        set_stage(run_dir, STAGE, status="error", error=str(e))
        return 1
    print(f"\n[stage2] status={result['status']}  snapshot={result.get('snapshot')}", flush=True)
    if result["status"] == "ok" and not args.no_gate:
        gate(cfg, run_dir, STAGE, result["summary"])
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
