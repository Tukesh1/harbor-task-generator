"""Shared fairness-reviewer helpers — used by both Stage 2 and Stage 3.

The reviewer is a read-only, adversarial qualitative checker (its system
prompt lives in agents/fairness-reviewer.md). It judges realism /
expert-solvability / non-contrived difficulty / fail-for-a-fair-reason from
its OWN reading of the task and trajectories, and its verdict IS the fairness
gate. Don't confuse that with the mechanical results (``is_breaking`` /
oracle / no-op) — those are separate, deterministic hard prerequisites that
the stage scripts enforce alongside the verdict.

How harbor analyze fits in: Phase A sends the reviewer NO analyze output
(pure own-judgment). Phase C / final passes attach the multi-model analyze
3-state colors as ADDITIONAL CONTEXT for the reviewer to WEIGH — it's a
fluctuating LLM signal, not a gate. By the time the reviewer sees anything,
the conductor has already hard-stopped on unanimous-red task_specification
and any reward_hacking flag, so what reaches the reviewer is greens +
yellows to adjudicate.
"""

from __future__ import annotations

import json

from .orchestration import extract_json_block
from .sdk import AgentSession

# Reviewer is read-only: NO Bash on purpose (it must not run scripts, re-score trials, or
# rebuild the scorer/oracle) — and with no shell it cannot write task files at all.
REVIEWER_RO_TOOLS = ["Read", "Grep", "Glob", "TodoWrite"]
REVIEWER_RO_DISALLOWED = ["Write", "Edit", "NotebookEdit", "Bash"]

# Re-injected the turn after the SDK compacts the reviewer's context (lib/sdk.py). The
# reviewer keeps NO durable file (it is read-only), so this note must re-establish its
# whole role from scratch. That is safe because (a) the per-round message the conductor
# sends is self-contained (task dir + failing-trajectory dir + analyze findings) and
# (b) the conductor keeps the cross-round verdict history in Python, which compaction
# cannot touch.
REVIEWER_COMPACTION_NOTE = (
    "⚠️ Your context was just compacted/summarized — you have lost earlier detail. You are the "
    "INDEPENDENT TB3 fairness reviewer (read-only; never edit the task). Before judging: re-read your "
    "role + criteria in your system prompt and the TB3 references it names. Judge THIS round ONLY from "
    "the message just below — it is self-contained (task dir + failing-trajectory dir, and possibly "
    "harbor analyze context); do not rely on remembered prior rounds. Mechanical results (trial rewards "
    "/ oracle 1.0 / no-op <1.0) are authoritative; any `harbor analyze` colors are a FLUCTUATING "
    "multi-model LLM signal you WEIGH as input (a 🟡 means models disagreed — often fine), NOT a gate. "
    "Decide on your own reading. Return your strict JSON verdict as the last thing in your reply."
)


_COLOR_ICON = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def _analyze_context_lines(analyze) -> list[str]:
    """Render the Phase-C analyze block: multi-model 3-state colors as extra
    context the reviewer weighs (never a gate). Hard-blocking reds were
    already caught upstream, so only 🟢/🟡 should reach this point."""
    colors = getattr(analyze, "colors", None) or {}
    cstr = "; ".join(f"{k}={_COLOR_ICON.get(v, '')}{v}" for k, v in colors.items()) or "(none parsed)"
    n = getattr(analyze, "n_trials", None)
    models = getattr(analyze, "models", None)
    lines = [
        f"- harbor analyze (ADDITIONAL CONTEXT — fluctuating LLM signal, NOT a gate) ran on "
        f"{n if n is not None else '?'} trajectories"
        + (f" across models {models}" if models else "") + ":",
        f"  per-criterion (🟢 all pass / 🟡 mixed across trajectories / 🔴 all fail): {cstr}",
        f"  summary: {(analyze.summary[:1200] if analyze.summary else '(none)')}",
        f"  (analyze JSON: {analyze.out_path})",
    ]
    yellows = [k for k, v in colors.items() if v == "yellow"]
    if yellows:
        lines.append(f"  NOTE: {yellows} came back 🟡 — some trajectories flagged it, others did not "
                     "(models disagreed). Weigh whether the concern is real for a COMPLETE EXPERT; a "
                     "yellow is NOT an auto-fail and is common for a hard, fully-specified task.")
    return lines


def reviewer_round(cfg, run_dir, task_rel, trials, analyze, what_changed, final=False,
                   oracle=None, nop=None, tqa=None) -> str:
    """Build the per-round message we send to the reviewer.

    The message must be fully SELF-CONTAINED — task dir, failing-trajectory
    dir, and (optionally) the analyze/TQA context — so that even a freshly
    compacted reviewer can judge this round from this message alone, with no
    memory of earlier rounds.

    ``analyze`` is None in Phase A (own judgment only) or a multi-model
    3-state result in Phase C/final (weighed, not gating). ``tqa``, when
    given, is the Task QA SOTA-trajectory check matrix over the same breaking
    trajectories — same 3-state convention, weighed exactly like analyze
    colors.
    """
    head = "FINAL fairness review." if final else "Fairness review."
    model = trials.model or "the SOTA model"
    lines = [
        f"{head}",
        f"- Current task dir: {task_rel} (read everything, incl. solution/solve.sh).",
        f"- Model-breaking check (mechanical, authoritative): {model} scored {trials.rewards} over "
        f"k={trials.k} trials ({trials.passes} passed).",
        f"- Failing-trial job dir: {trials.job_dir}",
    ]
    if final and oracle is not None and nop is not None:
        lines.append(f"- oracle reward = {oracle.reward} (1.0 expected), "
                     f"no-op reward = {nop.reward} (<1.0 expected).")
    if analyze is not None:
        lines += _analyze_context_lines(analyze)
    if tqa is not None and getattr(tqa, "ran", False):
        lines.append(f"- Task QA SOTA-trajectory checks, run over the SAME breaking trajectories "
                     f"(3-state like analyze; a 🔴 would have hard-blocked before reaching you, so "
                     f"these are 🟢/🟡/error): {tqa.colors}. Weigh a 🟡 (some trajectories flagged) "
                     f"like a yellow analyze criterion — context, not a gate. Per-trajectory "
                     f"findings: {tqa.out_dir}/")
    lines.append(f"- What the builder changed this round (excerpt): {what_changed[:1200]}")
    if analyze is not None:
        lines.append("Judge realism, expert-solvability (from instruction.md + the agent-visible "
                     "environment only), non-contrived difficulty, and fail-for-a-fair-reason on your "
                     "OWN reading. The harbor analyze colors above are ADDITIONAL CONTEXT you WEIGH (a "
                     "fluctuating multi-model signal), not a gate — you are the decision-maker. Return "
                     "your strict JSON verdict as the last thing in your reply.")
    else:
        lines.append("Judge realism, expert-solvability (from instruction.md + the agent-visible "
                     "environment only), non-contrived difficulty, and fail-for-a-fair-reason — on your "
                     "OWN reading of the task and the failing trajectory. Return your strict JSON "
                     "verdict as the last thing in your reply.")
    return "\n".join(lines)


def reviewer_feedback(verdict: dict) -> str:
    """Builder-facing feedback for a 'revise' verdict — spell out every failed
    floor plus the required changes, and remind the builder not to buy its way
    to approval by making the task easier."""
    return (
        "The fairness reviewer did NOT approve (verdict=revise).\n"
        f"realistic={verdict.get('realistic')}  expert_solvable={verdict.get('expert_solvable')}  "
        f"non_contrived={verdict.get('non_contrived')}  "
        f"fails_for_fair_reason={verdict.get('fails_for_fair_reason')}\n"
        f"reasons: {json.dumps(verdict.get('reasons', []), indent=2)}\n"
        f"required_changes: {json.dumps(verdict.get('required_changes', []), indent=2)}\n\n"
        "Address EVERY required change WITHOUT reducing difficulty or un-breaking the task, "
        "then end with a status."
    )


async def ask_reviewer(reviewer: AgentSession, msg: str) -> tuple[dict, str]:
    """Send one round to the reviewer; return (parsed verdict dict, full reply).

    If the reply carries no parseable JSON verdict, we do NOT crash — we
    synthesize a strict 'revise' verdict so the failure is loud and the loop
    continues safely instead of an unparsable reply silently counting as an
    approval.
    """
    reply = await reviewer.send(msg)
    verdict = extract_json_block(reply) or {
        "verdict": "revise", "realistic": None, "expert_solvable": None,
        "non_contrived": None, "fails_for_fair_reason": None,
        "reasons": ["Reviewer returned no parseable JSON verdict."],
        "required_changes": ["Re-issue a strict JSON verdict."], "notes": reply[:500],
    }
    return verdict, reply


def approved(v: dict) -> bool:
    """The reviewer fairness gate: approve ONLY if the verdict says approve AND
    all four floors hold (realistic / expert_solvable / non_contrived /
    fails_for_fair_reason). Anything missing or falsy blocks — fail-closed."""
    return (str(v.get("verdict", "")).lower() == "approve"
            and bool(v.get("realistic")) and bool(v.get("expert_solvable"))
            and bool(v.get("non_contrived")) and bool(v.get("fails_for_fair_reason")))


def replies_markdown(replies: list[tuple]) -> str:
    """Render saved reviewer replies (``(phase/round, reply_text)`` tuples) into a
    markdown doc, so the FULL text — not just the parsed verdict — survives into
    the snapshot for the human reviewer to read later."""
    out = ["# Fairness reviewer — full replies\n"]
    for tag, reply in replies:
        out.append(f"\n## {tag}\n\n{reply}\n")
    return "".join(out)
