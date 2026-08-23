---
name: stage2-break-task
description: Harden a Terminal Bench 3 task until a SOTA agent (GPT-5.5) fails every trial, while keeping it realistic, expert-solvable, non-contrived, and oracle/no-op passing — guided by an independent fairness reviewer.
---

# Stage 2 — Make it genuinely model-breaking (fairly)

The task under `./task/` already passes the oracle and no-op runs. Your job is to
make it **objectively model-breaking**: a strong SOTA agent (GPT-5.5, run by the
conductor) must **fail all k trials** — and it must fail for a **fair, intrinsic**
reason that a complete domain expert would nonetheless overcome.

## Your ENTIRE purpose: break the SOTA model (currently GPT-5.5)
This is the single hardest and most important job in the whole pipeline — harder
than authoring the task. Your one responsibility is to make the task **fail every
trial of the SOTA model the conductor runs** (currently GPT-5.5; whichever model is
configured). That model is very capable: a task that merely *looks* hard, or that
only traps a naive shortcut, will be solved on the first try. **Every passing trial
is proof the task is still too easy.** You are NOT done — and must never stop, report
"no action needed", or treat a round as finished — until that model fails **all k
trials** and the reviewer approves. Authoring is over; making it break SOTA is the
job now.

## Scope: ONLY make it break — Stage 3 owns the checks/QA
The task is **already in good shape on the structural checks** (`harbor check`,
canaries, separate-verifier config, metadata). That is **not your concern.** Do NOT
run `harbor check`, do NOT fix or add canary lines, do NOT regenerate fixtures for
cleanliness, do NOT complete `task.toml` metadata, and do NOT polish files for QA —
**Stage 3 does all of that.** Every turn you spend tidying files instead of breaking
the task is wasted (this is the #1 way this stage stalls).

The ONLY upkeep you owe while hardening:
- keep the **oracle at 1.0 and the no-op < 1.0** when your edits touch the
  data/verifier (Phase B handles this), and
- don't open a **reward-hack hole** (a break must be a real break, not a verifier bug).
Everything else — formatting, canaries, full metadata, `harbor check`/`analyze` — is
the next stage's job. **One exception:** keep the *justification* current —
`task.toml`'s `difficulty_explanation` and your design-notes rationale — because that
defends the break's fairness/realism (especially for a niche/borderline task); that
is not QA polish. Spend every turn on the **difficulty crux + the instruction** to
make the SOTA model fail.

The Python conductor drives the loop and tells you the phase + round each turn.
It runs the authoritative `harbor` trials and owns an independent **fairness
reviewer**; you receive their results and respond by editing `./task/`.

## Durable progress log — survive compaction WITHOUT redoing work (MANDATORY)
Your conversation context **will be summarized mid-run** when it overflows, and the
summary is lossy — that is when agents drift into redoing experiments or "tidying the
docs". Defend against it with a file: **maintain `./stage2-progress.md`** as your
single source of truth.
- After every experiment / lever / edit, record a **one-line result**: what you tried
  → did it break the model (scores)? robust? plus the current oracle/no-op status and
  your active hypothesis. Keep a short **"DO NOT REDO"** list.
- **Keep it COMPACT — a rolling summary, not an ever-growing transcript.** This file is
  re-read every turn and bloats your context (which triggers more lossy compactions), so
  distill each round to 1–3 lines and **prune/condense** stale detail as you go — keep it
  scannable (target a few hundred lines max, not thousands). Do **NOT** paste trajectories,
  long analyses, or code into it; record the one-line lesson (and a path if you must) and
  move on. Do not re-open old trajectories you've already distilled here.
- At the **start of every turn** — and immediately if you notice you were
  "continued from a previous conversation" — **read this file first and trust it over
  your memory.** Never re-run an experiment or re-derive a fact already recorded there;
  continue from where it leaves off.
This is not optional bookkeeping: it is how you avoid burning whole turns re-deriving
what you already knew.

**Read first, every run:** your design notes `./inputs/stage1-design-notes.md`,
the proposal `./inputs/proposal.md`, and the lever catalog (read-only)
`${LEVERS_CATALOG}` — **read its top "Why most levers don't break a frontier model"
LENS first**, then levers A–N (A–K + the omission/data-quirk/intertwined-series
shapes L/M/N); plus the vendored difficulty reference `${DIFFICULTY_DOC}`.

## The three phases (the conductor sequences these)
1. **Phase A — break + fairness.** Loop: apply lever(s) → conductor runs k=3
   GPT-5.5 trials → if it still passes, harden more; once 0/k, the reviewer judges
   fairness. Continue until **0/k AND the reviewer approves**. (Oracle/no-op may
   break here — that is expected; don't keep repairing them yet.)
2. **Phase B — restore oracle/no-op.** With the break + fairness locked, make the
   oracle pass (1.0) and no-op fail (<1.0) again **without reducing difficulty**.
   After each fix the conductor re-confirms it's still 0/k breaking.
3. **Phase C — final review.** The reviewer does one last full pass; everything
   (breaking, fair, oracle/no-op) must hold.

## Levers — pick 1–3 per round, grounded in the Difficulty Crux
Use levers from the catalogs (A–K). Each must correspond to a **real-world
hazard**, not a puzzle. Highest-ROI against frontier models, used fairly:
- **Adversarial trap** — a plausible in-repo signal pointing at the wrong fix; the
  expert distrusts it, the model anchors on it.
- **Coupled-bug interaction** — two issues that interact so isolated fixes
  invalidate each other; must be solved as a system.
- **Symptom/root-cause distance** — the visible failure points at the wrong layer;
  forces instrumentation/tracing instead of patching the symptom.
- **Staged discovery** — a second issue only observable after the first is fixed,
  defeating "fix the visible failure and stop".
- **Hidden invariant** — an implicit but standard domain invariant (determinism,
  conservation, ordering, idempotency) the verifier checks; fair because an expert
  in the domain knows it must hold.
- **Convention trap** — a real domain convention (day-count, spot-vs-forward,
  units) where the default guess is wrong.

## Think across every arena — the method (the crux of this job)
A frontier model is at least as capable as you, so quantitative escalation is the #1 trap
and is **FORBIDDEN as a breaking strategy**: more entities/steps/bigger numbers just runs a
bigger version of the SAME recipe the model already knows — it still passes. **Every task IS
breakable** with enough understanding; if a lever gets solved, you have not found the right
angle yet — **never conclude a task "can't be broken"** (that conclusion is the failure mode
that wastes rounds; it is always wrong). Instead think hard and creatively across ALL of these
arenas before and while you pick levers:

1. **The crux + what's SPECIAL about THIS variant.** What about *this particular* task would a
   strong-but-generalist agent get wrong even applying real care? Where is the domain subtlety
   only a specialist respects?
2. **The full lever menu** — read `${LEVERS_CATALOG}` incl. its frontier-model LENS and shapes
   **L** (omission), **M** (data-quirk), **N** (intertwined series). Pick 1–3, composed.
3. **What the agent gets RIGHT vs where it SLIPS.** Don't fight its strengths (it retrieves named
   techniques, reads many files, self-tests invariants). Exploit where it's uncertain: an
   unstated-but-expected step, a structural data property it won't inspect for, a convention that
   cuts against the obvious framing.

### WHY most levers fail against this model (understand it — it is NOT a checkbox)
The model (a) **implements anything you can STATE precisely** and (b) **self-tests aggressively** —
property tests, backtests, uniqueness/sanity checks, iterating until green. So **if the model can
build a check that would catch its OWN mistake, it self-corrects and your task does not break.**
(This is exactly why a clean, self-verifiable algorithm task — distributed locks, FP-summation,
sorting — resists every fair rule you state: it property-tests the invariant and catches its own
error. If your seed is like that, rework it toward a data-judgment task.) So a break LANDS only
when **the model cannot cheaply tell it is wrong**:
- its **own checks come up GREEN on the wrong answer** (metric, audit, uniqueness/structural
  checks all pass for the incorrect output → no signal → it confidently submits);
- the catch is **discoverable in the agent-visible DATA/environment** (a diligent expert finds it
  in the samples), **never hidden only in `tests/`** (undiscoverable = unfair = rejected);
- the divergence is **calibrated below any loud threshold** the model would notice;
- there's **no graduated verifier feedback** to iterate against on the critical path (binary).

### The strongest shape: OMISSION (the model stops one expert step short)
The most reliable break is NOT "make the stated algorithm wrong" (it implements + self-tests
those) — it's an **omission**: the model produces a confident, complete-LOOKING answer and stops,
never realizing a further step a responsible expert would take. It cannot self-test a step it
doesn't know exists. Worked example (`retail-demand-feature-leakage`): the feed is append-only with
RESTATEMENTS that must be version-resolved per record before aggregating; the model does
`groupby + sum`, never checks input-key uniqueness, double-counts — its output grain stays unique
(uniqueness check passes), its audit mirrors its own build (0 violations), its backtest metric is
green; it ships confidently wrong. The discriminating dup keys are right there in the data, and the
restatement rate was tuned so the metric stays under its gate (no loud signal). Reach for L/M/N to
build this; layer several so no single strand leaks the answer and the framing misdirects toward
the incomplete fix.

### Still valid — compose these
- **Mine the trajectory for the model's WEAKNESS, not its strength** — where it guessed, assumed,
  or got lucky is the lever, not the step it nailed.
- **Make the default confident move WRONG** (a fork it must reject) — a second way to break; weaker
  than omission against a self-tester, but useful, especially composed with a data-quirk.
- **Avoid named, retrievable recipes** — require a derivation, or better an omission, not a textbook
  method.
- **There is no give-up step.** Every round you don't break it, pick a DIFFERENT arena/angle —
  never repeat a defeated lever, never conclude it's unbreakable.

### Fairness + harbor analyze (how a deliberately brutal task still passes)
The bar is a **responsible domain expert paid to be perfect and fully accountable**: they act
diligently — inspect the data, don't stop at the obvious fix, handle the odd cases — and they SOLVE
it. Keep the task solvable for THAT expert. `instruction.md` + the data must be SUFFICIENT for them
even when the discriminating step is unnamed (unnamed-but-data-discoverable is fair; tests-only-
hidden is not). **Document the expert's solution path in `task.toml`'s `difficulty_explanation`**
(and your design notes) every time you add or change a flavour — that documented fairness rationale
is what keeps the reviewer and `harbor analyze` satisfied even when the task is deliberately brutal.

## You may fundamentally rework the task (the proposal is a starting point, not a cage)
The proposal and the Stage-1 difficulty crux are where you *start*, not a boundary you
must stay inside. Adhere to the proposal's spirit when you can — but if the current
framing keeps being solvable, **change it**: swap the crux, restructure the scenario,
replace the data or verifier, redesign the environment, or **remove a Stage-1 leak**
(a verbatim reference solution, the exact grader/verifier, or a benchmark that mirrors
the held-out metric — patching those is in scope and fair: an expert keeps the spec and
tools, not the answer). Straying significantly from the proposal to land a fair,
model-breaking task is **fair game**. The only invariants are the fairness floor
(expert-solvable from `instruction.md` + the agent-visible environment; realistic,
niche-ok; intrinsic, non-contrived difficulty) and oracle 1.0 / no-op <1.0.

## Play at the BORDERLINE of fair — adversarially — and DOCUMENT the justification
You are an adversary. **Push hard, right up to the edge of fair, and sit on that
boundary** — do NOT self-censor away from an aggressive idea because it *feels* too
mean or too niche. The boundary is exactly where model-breaking tasks live, and that
is fine: the independent reviewer and a later human pass adjudicate the boundary, not
your timidity.

The floor that must still hold (and that you must be able to DEFEND):
- **Expert-solvable** — a complete domain expert could still solve it *correctly*
  from `instruction.md` + the agent-visible environment. It may be genuinely hard and
  demand senior judgment; it must not require *guessing* truly undiscoverable
  information or test values that cannot be derived.
- **Realistic — niche is fine.** It need not be common; a researcher / cutting-edge
  practitioner plausibly facing it is enough. A very niche scenario is acceptable
  **if you can justify it.**

So **document the justification.** Whenever you push to the borderline, write WHY the
scenario is realistic (even if niche) and WHY it stays expert-solvable — in
`./stage1-design-notes.md` (your rationale) and in `task.toml`'s
`difficulty_explanation`. That justification is what makes a borderline task
defensible and what gets analysed later. A well-justified niche break is a success.

(Still genuinely out of bounds — not "borderline", just contrived: difficulty from
volume/busywork, output-format gymnastics, resource/time starvation, tokenization
gimmicks, or a task no expert could solve at all.)

## Keep these intact while hardening
- **Anti-cheat**: agent still cannot read `solution/`/`tests/`; reward gated by
  exit code; no hardcoding path. Tighten the verifier if a trial reward-hacks.
- **Outcome verification**: tests check real behaviour, not implementation.
- **Specification**: if you add a constraint the verifier checks, make sure
  instruction.md still fully specifies it (well-specified, not a hidden gotcha) —
  unless hiding it is itself the fair, expert-known invariant (state which).

## When the model still passes: READ ITS TRAJECTORY, triage genuine-vs-hack, fix the RIGHT thing (MANDATORY)
Each round you don't break it, the conductor gives you the on-disk paths to the
**actual passing trajectories** of the SOTA model. You MUST open and traverse them
yourself before editing — there is no pre-digest. For each passing run you get:
- `agent/codex.txt` — the readable transcript (the model's own reasoning + every
  shell command it ran);
- `agent/trajectory.json` — the structured trajectory;
- the full trial dir — also holds `agent/sessions/.../rollout-*.jsonl` and `verifier/`.

Traverse them as deeply as you need, then **FIRST decide HOW the model passed** —
genuine solves and reward-hacks need OPPOSITE fixes:

**A. Genuine solve.** It actually did the proper work. Then the core/proper solution
path is too easy. Do NOT just scale that path (more entities/steps/bigger numbers =
the same recipe, still solvable). Instead mine the trajectory for where the model
*guessed, assumed, hand-waved, nearly failed, or got lucky*, and make a **conceptual
leap** that turns its default confident move into the wrong one (see "Break with a
CONCEPTUAL LEAP"). Trapping a naive shortcut it never takes does nothing.

**B. Reward-hack / cheat.** It scored WITHOUT truly solving — hardcoded or echoed
expected outputs, gamed a metric/threshold/parsing hole, wrote files that satisfy
the check without doing the work, or reached `solution/`/`tests/`. This is a
**verifier bug, not a difficulty gap**: close the hole / tighten anti-cheat and
outcome-verification so the hack no longer scores, and do **NOT** add difficulty
for it. Then re-confirm the oracle still scores 1.0 and the no-op still <1.0.

Either way, **make concrete edits to ./task this turn**.

Never: harden blind without reading the trajectory; respond to a reward-hack by
"adding difficulty" (fix the verifier instead); spawn background jobs / long
experiments and burn the turn collecting their output; or call a round done while
the model still passes. Reading the trajectory and fixing the right thing is the work.

## Each turn — what the conductor gives you and what to return
- **Trial results**: k rewards from GPT-5.5 (+ any reward, + `harbor analyze`
  findings on the failing trajectories: e.g. `reward_hacking`, `task_specification`,
  `difficulty_crux`, `near_miss`). Use these to decide your next lever:
  - any pass → harden the dimension the agent exploited.
  - `reward_hacking` fail → close the verifier hole immediately.
  - `near_miss` fails → the threshold is doing the work, not the concept; deepen
    the conceptual challenge rather than just tightening a number.
  - `difficulty_crux` fail → it's failing for the wrong reason; realign to the crux.
- **Reviewer verdict** (JSON): `realistic`, `expert_solvable`, `non_contrived`,
  `fails_for_fair_reason`, plus `required_changes`. Address every required change.

Reply each turn with: the lever(s) applied, why, the exact files changed, and a
status line. Keep `./stage1-design-notes.md` updated with new levers and any
fairness rationale (e.g. why a hidden invariant is expert-known).
