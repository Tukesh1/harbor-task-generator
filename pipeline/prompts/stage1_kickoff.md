You are authoring a new Terminal Bench 3 task.

1. Read your playbook in full: `.claude/skills/stage1-author-task/SKILL.md`.
2. Read the proposal: `./inputs/proposal.md`.
3. Skim the references named in the playbook (TB3 standards + lever catalogs) and
   one real task under the TB3 `tasks/` dir for structure.

Then build the complete task under `./task/` — realistic, fair, well-specified,
and hard the RIGHT way (apply 2–3 levers from the proposal's Difficulty Crux so a
strong SOTA agent will plausibly fail, but a domain expert would solve it from the
instruction + environment alone). Build in every hard requirement from the
playbook (canary lines, separate verifier, absolute paths, instruction suffix,
full task.toml metadata, pinned pip, anti-cheat, a genuine oracle `solve.sh`).

Also write `./stage1-design-notes.md` with: your break hypothesis (exactly how you
expect SOTA to fail), the levers you used, and a hidden-knowledge audit (each
thing you know that the agent must be able to infer — confirm it is fairly
inferable).

Do NOT run `harbor check`, `harbor analyze`, or AutoQA. The conductor will run the
oracle and no-op for you and report back. End with a one-line status.

**Author first; do not over-validate.** See the playbook's "Validation budget"
section. Validation is the conductor's job (oracle / no-op now, GPT-5.5 break trial
later) — you do NOT need to empirically prove the task breaks SOTA or is non-flaky
before building. Spend this turn authoring the full `./task/`, not exploring in
`scratch/`. At most ONE tiny, fast sanity experiment (≤~30 lines, seconds, reduced
scale) — no parameter sweeps, precision ladders, large traces, or extra toolchains.
A long turn of compiles/sweeps/large reads will overflow context and auto-compact,
making you lose state and re-do work. Pick defensible numbers from domain knowledge
and refine them later using the conductor's real oracle/no-op results.
