You are hardening an existing Terminal Bench 3 task to make it genuinely
model-breaking — fairly.

1. Read your playbook in full: `.claude/skills/stage2-break-task/SKILL.md`.
2. Read `./inputs/stage1-design-notes.md` and `./inputs/proposal.md`.
3. Skim the lever catalogs named in the playbook.

The task under `./task/` currently passes the oracle and no-op runs. We are in
**Phase A (break + fairness)**. Choose 1–3 levers grounded in the Difficulty Crux
and apply them so a strong SOTA agent (GPT-5.5) will fail — without crossing the
fairness bar (a complete domain expert must still be able to solve it from
`instruction.md` + the environment alone; no contrived difficulty).

The conductor will then run k=3 GPT-5.5 trials and an independent fairness
reviewer, and report results back to you. Do not weaken anti-cheat or outcome
verification. Keep `./stage1-design-notes.md` updated with new levers and fairness
rationale. Reply with: the lever(s) applied, why, the files changed, and a status
line.

**Your entire purpose this stage is to make the task FAIL EVERY trial of the SOTA
model the conductor runs (currently GPT-5.5).** This is the hardest part of the
pipeline. Each time the conductor reports the model still passed, OPEN the passing
trajectory files it points you to and traverse them yourself to see exactly how the
model solved it — then harden the CORE solution path (not a naive shortcut it never
takes). A passing trial means the task is still too easy: never report "no action
needed" while it still passes, never burn the turn on background jobs, and keep
going until the model fails all k trials and the reviewer approves.

**Scope:** your ONLY job is model-breaking (fairly), driven by the difficulty crux
and the instruction. The task already passes the structural checks — do NOT run
`harbor check`, fix canary lines, regenerate fixtures for cleanliness, or polish
files for QA; **Stage 3 handles all of that.** The only upkeep: keep the oracle at
1.0 and the no-op < 1.0 when your edits touch data/verifier, and don't open a
reward-hack hole. Don't waste turns tidying files.

**Break it with a conceptual leap, not by scaling the recipe.** A frontier model is
as capable as you — adding more entities/steps/bigger numbers just makes it run a
bigger version of the same recipe it already knows, and it'll still pass. Mine the
passing trajectories for where the model *guessed, assumed, or got lucky*, and break
it THERE — turn its default confident move into the wrong one; avoid named/retrievable
techniques. Be **adversarial** and push right to the **borderline of fair** (that's
encouraged — the reviewer and a later human pass adjudicate the boundary). The floor:
a complete expert could still solve it from `instruction.md` + the env, and the
scenario is justifiable — **niche is fine if you justify it** (document why in
`task.toml`'s `difficulty_explanation` + your design notes).

**You may fundamentally rework the task.** The proposal and the Stage-1 difficulty
crux are a *starting point, not a cage*. Adhere to the proposal's spirit when you can,
but if breaking the model requires it, significantly restructure the task — swap the
crux, change the scenario, replace the data/verifier, redesign the environment, or
remove any Stage-1 leak (a verbatim reference solution, the exact grader, a benchmark
that mirrors the held-out metric) — **as long as the result stays fair** (expert-solvable
from `instruction.md` + the env; realistic, niche-ok; intrinsic difficulty). Straying
from the proposal to land a fair, model-breaking task is fair game. **There is no
give-up step: keep designing a different fair conceptual fork every round.**

**Create `./stage2-progress.md` now and keep it current** — this is your durable
memory. After every experiment, lever, or edit, append a one-line result (what you
tried → did it break the model? robust? scores; plus oracle/no-op status and your
current hypothesis). At the start of each turn, read it first and never re-run an
experiment whose result is already recorded there. Your context may be summarized
mid-run, and this file is the only thing that survives that — so it must always
reflect what you've tried and what's left to do.
