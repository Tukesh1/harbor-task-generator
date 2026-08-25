---
name: stage3-qa-task
description: Make a model-breaking Terminal Bench 3 task pass harbor check (implementation rubric) and harbor analyze (trial analysis) WITHOUT reducing its difficulty or losing the break, then keep oracle/no-op passing.
---

# Stage 3 — Pass QA without weakening the task

The task under `./task/` is already model-breaking and was judged fair by the
reviewer. Your job: make it pass **`harbor check`** (the 29-criterion
implementation rubric) and **`harbor analyze`** (trial analysis) — while keeping
it exactly as hard. The conductor runs check/analyze/trials and feeds you the
findings; loop until everything passes.

## ⛔ CARDINAL RULE — never reduce difficulty
A future-proof, super-strong agent (Claude Opus 4.8 and beyond) must still fail
this task after your edits. Therefore you must **NOT**:
- add clarifying comments, helpful error messages, hints, worked examples, or
  step-by-step procedure to the code, environment, or instruction;
- reveal or gesture at the solution approach / the difficulty crux anywhere the
  agent can see (`instruction.md`, `environment/`, code comments);
- loosen a test threshold, widen a tolerance, or remove a check;
- turn a hard diagnostic problem into a guided one.

When a rubric criterion asks for more "clarity," satisfy it by making the
**outcome precisely specified** (well-specified), **not** by hinting the method.
Specification ≠ hand-holding. If you cannot pass a criterion without making the
task easier, say so in your status and stop — do not weaken it.

## `harbor check` — the 29 criteria, and how to pass each kind
Most failures are **metadata / structure / specification**, which you can fix with
zero difficulty cost:
- **Metadata quality**: `difficulty_explanation_quality`,
  `solution_explanation_quality`, `verification_explanation_quality`,
  `category_and_tags`, `expert_time_estimate`, `task_name`, `task_toml_schema`,
  `typos` → write thoughtful, accurate `task.toml` fields; fix typos.
  - `task_name`: you OWN the task's folder slug via `task.toml` `[task].name`. It MUST be in
    harbor's `org/name` form (e.g. `${TASK_ORG}/distributed-lease-fencing`) — a bare slug makes harbor
    reject the whole task *before grading*. The conductor runs the `task_name` criterion against
    the segment AFTER the `/`, so keep that segment to **≤3 hyphenated kebab words**; it is the
    name the task ships under. Fully in your scope — never surface it as a run-dir problem.
- **Structure / hygiene**: `separate_verifier_configured`, `environment_hygiene`
  (no leaked solution/tests, no embedded data dumps), `structured_data_schema`,
  `resource_configuration` (cpus/memory/gpus sane), `task_readme`.
- **Specification / verification**: `verifiable`, `functional_verification`,
  `deterministic_reproducible`, `outcome_verified`, `test_instruction_alignment`
  (instruction alone implies the tests — fix by precise spec, not by leaking),
  `instruction_concision`.
- **Quality / difficulty** (do NOT compromise): `difficult`, `essential_difficulty`,
  `solvable`, `interesting`, `novel`, `agentic`, `reviewable`, `solution_quality`,
  `anti_cheat_robustness`, `task_security`.
  - If `anti_cheat_robustness` / `task_security` fail, **tighten** the verifier
    (harder), never relax it.
  - If `difficult` / `essential_difficulty` are flagged, the fix is almost never
    here in stage 3 — surface it; don't add difficulty hacks or remove any.

## `harbor analyze` — the failing GPT-5.5 trial (also your "still breaking?" check)
The conductor runs a fresh GPT-5.5 trial each round and analyzes the failing one.
It must stay failing (0 passes). Address analyze criteria like this:
- `reward_hacking` FAIL → close the verifier hole (makes it harder, good).
- `task_specification` FAIL → instructions are insufficient → fix by **specifying
  the outcome precisely**, never by hinting the approach.
- `low_timeout` FAIL → the agent was still progressing at the cutoff → **raise
  `[agent].timeout_sec`** so difficulty comes from the problem, not the clock.
  Never simplify to "fit" the timeout.
- `refusals` FAIL → a framing word tripped a safety guardrail → reword the framing
  (keep the substance).
- `near_miss` FAIL → the threshold is doing the work, not the concept → this is a
  difficulty-shape concern; flag it (don't fix by lowering the bar).
- `difficulty_crux` FAIL → it's failing for the wrong reason → realign to the crux.

## Keep intact
Oracle must stay 1.0 and no-op < 1.0 (conductor re-confirms). The break must
survive (0/k). At the end the reviewer does a final fairness pass.

## Each turn
The conductor gives you: failing `harbor check` criteria (+ the raw JSON path),
`harbor analyze` blocking fails / warnings, and the current oracle/no-op + break
status. Fix `./task/` accordingly under the cardinal rule, then reply with the
files changed and a status line. If a required fix would reduce difficulty, refuse
it explicitly and explain — the conductor will surface that to the human.
