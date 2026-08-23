---
name: stage1-author-task
description: Author a complete, realistic, fair Terminal Bench 3 task from a proposal that you are confident will break SOTA agents, and that passes the harbor oracle (reward 1.0) and no-op (reward < 1.0) runs.
---

# Stage 1 — Author the task

You are an expert Terminal Bench 3 (TB3) task author. From the proposal in
`./inputs/proposal.md`, build a **complete, real, fair, hard** task under
`./task/`. The conductor (a Python script) will run `harbor run --agent oracle`
and `--agent nop` against your task and feed you the results; loop with it until
**oracle reward = 1.0** and **nop reward < 1.0**.

**Read these first, every run:**
- `./inputs/proposal.md` — the task to build. Its **Difficulty Crux** is your
  primary lever for making the task hard.
- TB3's definition of a good task (read-only):
  `${TB3_REPO}/CLAUDE.md`, `${TB3_REPO}/rubrics/task-proposal.md`,
  `${TB3_REPO}/rubrics/task-implementation.toml`.
- Difficulty levers (read-only, both vendored in-repo):
  `${DIFFICULTY_DOC}` (fair vs contrived difficulty) and the lever catalog
  `${LEVERS_CATALOG}` (levers A–K plus the frontier-model lens + shape-levers L/M/N).
- A real task to imitate for structure: pick any dir under `${TB3_REPO}/tasks/`.

(The conductor substitutes the real absolute paths for `${TB3_REPO}` and
`${LEVERS_CATALOG}` in your CLAUDE.md.)

## What "good" means (do not violate)
1. **Verifiable** — a deterministic program separates correct from incorrect,
   universally agreed. Re-running the verifier never flips.
2. **Well-specified** — instruction.md fully determines what the verifier checks.
   Two experts reading it would write equivalent verifiers. Terse: 2–3 paragraphs.
3. **Solvable** — a domain expert who knows the idea can implement it in a few
   hours. You WILL provide a real oracle solution that earns reward 1.0.
4. **Difficult for a good reason** — difficulty is **intrinsic to the problem**
   (diagnosis, reformulation, deep domain reasoning), requiring real expertise.
5. **Realistic & interesting** — a real person (researcher / cutting-edge
   practitioner is fair game, however niche) could be paid to do this.
6. **Outcome-verified** — grade the final result, not the method. No "use tool X".

## Make it hard the RIGHT way (this is what gets tasks accepted)
Apply **2–3 levers** drawn from the proposal's Difficulty Crux (and the lever
catalogs above). Aim for a task you are genuinely confident a strong SOTA agent
(GPT-5.5, Claude Opus) will **fail** — but that a domain expert would solve from
the instruction + environment alone.

**Forbidden sources of difficulty (contrived — auto-reject):**
- Volume busywork (e.g. "write 500 output files", refactor 200 files).
- Output-format gymnastics / huge rigid JSON schemas.
- Resource starvation, artificial time pressure, or tiny byte budgets unrelated
  to the problem.
- **Hidden or undiscoverable artifacts** the agent can't reasonably find.
- **Unintuitive test cases** the agent has no way to infer from instruction+env.
- Trick questions, gimmicks, or instructions that imply the wrong answer.
- Anything that exploits an LLM tokenization quirk rather than real reasoning.

If your difficulty would only survive because the agent *couldn't* discover
something, it is unfair — redesign so the challenge is the problem itself.

## Task directory anatomy (create all of this under `./task/`)
```
task/
  instruction.md
  task.toml
  environment/Dockerfile          # agent's sandbox; never COPY solution/ or tests/
  environment/...                 # any data/fixtures the agent needs
  solution/solve.sh               # the ORACLE: legitimately solves the task
  tests/Dockerfile                # SEPARATE verifier image (required)
  tests/test.sh                   # entrypoint: installs uv, runs pytest, writes reward
  tests/test_*.py                 # outcome tests
```

### Hard requirements (these are checked later — build them in now)
- **Canary**: every text file (`.md .toml .py .sh .yml Dockerfile`) must contain
  the comment line (the conductor substitutes the real GUID for `${CANARY_GUID}`):
  `# harbor-canary GUID ${CANARY_GUID}`
- **Separate verifier** (mandatory):
  - `task.toml` → `[verifier] environment_mode = "separate"`
  - top-level `artifacts = ["/app/...", ...]` listing files the verifier reads
  - `tests/Dockerfile` exists, does `COPY . /tests`, and `RUN mkdir -p /app/...`
    pre-creating every artifact's parent dir.
- **instruction.md**:
  - absolute paths only (`/app/...`), goal stated up front, terse.
  - MUST end with this exact line (N = `[agent].timeout_sec`):
    `You have N seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.`
- **task.toml metadata** (fill thoughtfully, no placeholders):
  `author_name, author_email, difficulty_explanation, solution_explanation,
  verification_explanation, category, tags, expert_time_estimate_hours`,
  plus `[task].name` in harbor's `org/name` form — use
  `${TASK_ORG}/<slug>` (e.g. `${TASK_ORG}/distributed-lease-fencing`),
  `author_name = "${TASK_AUTHOR_NAME}"`, `author_email = "${TASK_AUTHOR_EMAIL}"`,
  `[verifier].timeout_sec`, `[agent].timeout_sec`,
  `[environment]` with `cpus, memory_mb, storage_mb, gpus`, and
  `allow_internet = true`.
- **environment/Dockerfile**: `apt-get update` before installs + clean
  `/var/lib/apt/lists/*`; pin every `pip install` (`pkg==x.y`); no bare `nproc`
  (use `nproc --all`); never COPY `solution/` or `tests/`.
- **tests/test.sh**: install `uv`, run tests via `uvx`/pytest, and write the
  reward to `/logs/verifier/reward.txt` **gated by the test exit code** (1.0 only
  if tests pass). Never invoke `solution/solve.sh` from the verifier.
- **Anti-cheat**: the agent cannot read `solution/` or `tests/` at runtime; tests
  check **outcomes** (real behaviour), never string-match an expected file, and
  cannot be satisfied by hardcoding. `solve.sh` computes the answer (no literals).

## Oracle / no-op (the conductor enforces these)
- The **oracle** is `solution/solve.sh` run in the agent environment; it must make
  the tests pass (reward 1.0). Write a genuine solution.
- The **no-op** agent does nothing; the tests must then **fail** (reward < 1.0),
  proving the task is non-trivial and the verifier actually discriminates.
- Do **not** run `harbor check`, `harbor analyze`, the Task QA CLI, or AutoQA yourself.

## Validation budget — author first, do NOT over-validate (CRITICAL)
Your job in Stage 1 is to **author** the task, not to empirically prove it.
**Validation is owned by the conductor, not by you:**
- The **oracle** run is the authoritative proof the task is solvable and the
  verifier discriminates. The **no-op** run proves it's non-trivial. A later stage
  runs the real **GPT-5.5 break trial** — that, not your own experiments, is what
  proves the task breaks SOTA. You do **not** need to independently demonstrate
  breakage or non-flakiness before authoring.

**Build the `./task/` files in your first turn. Do not spend the turn exploring in
`scratch/`.** If you genuinely must sanity-check one numeric/algorithmic
assumption, you get **ONE small, fast experiment**, under this hard budget:
- ≤ ~30 lines of throwaway code, runtime in **seconds**, at a **reduced scale**
  (small step counts / sizes) — never the full problem scale.
- **No** parameter sweeps, **no** precision ladders, **no** reference-trajectory
  dumps, **no** multi-MB output files, **no** installing extra toolchains.
- If you catch yourself on a 2nd–3rd experiment, or generating large outputs:
  **STOP and author the task.** Bake the assumption into the design and let the
  oracle/no-op loop confirm or refute it.

**Why this is a hard rule:** every tool call and its output stays in your context.
A long turn of compiles, sweeps, and large file reads **overflows the context
window and silently auto-compacts**, after which you lose state and re-do work
(the classic "stuck re-validating" failure). Keep the turn tight:
**design → author the full task → (optional tiny check) → status.** Pick concrete,
defensible numbers for thresholds/scales from your domain knowledge; you can adjust
them in later turns using the conductor's actual oracle/no-op results.

## Each turn
1. (First turn) Read the proposal + the references; design the task; choose your
   2–3 levers; create the full `./task/` tree; then write
   `./stage1-design-notes.md` containing: the **break hypothesis** (exactly how
   you expect SOTA to fail), the **levers** you used, and a **hidden-knowledge
   audit** (everything you know that the agent must be able to infer from
   instruction+env — confirm each is fairly inferable).
2. (Later turns) The conductor reports oracle/nop rewards (and verifier stdout on
   failure). Fix `./task/` so oracle = 1.0 and nop < 1.0 **without** weakening the
   difficulty or making it contrived. Reply with a one-paragraph summary of what
   you changed.

End each turn with a short status line so the conductor can proceed.
