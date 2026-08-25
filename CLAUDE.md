# harbor-task-generator — working notes for Claude Code

A Terminal Bench 3 (TB3) task-generation pipeline. A Python **conductor** drives
stage **agents** (builder + independent fairness reviewer) via the Claude Agent SDK;
the conductor runs the authoritative `harbor` trials and owns the gate decisions.

- `pipeline/stage1_author.py` — author a task (oracle 1.0 / no-op <1.0).
- `pipeline/stage2_break.py` — make it genuinely model-breaking *and* fair (the hard stage).
- `pipeline/stage3_qa.py` — QA + re-verify the break.
- `pipeline/lib/` — `sdk.py` (AgentSession), `harbor.py` (trials), `costs.py`, `dataset.py`
  (negative-example capture), `orchestration.py`, `common.py`, `review.py`.
- `watch_run.py` — live monitor. `tests/` — regression tests (`python tests/test_costs.py`).

---

# DOCTRINE: agent guidance MUST survive context compaction

Long agent runs **will** be compacted mid-run (the SDK summarizes earlier context when
it overflows; the summary is lossy). When you change what an agent should DO, you must
ensure the change still steers it **after** a compaction — otherwise late rounds silently
revert to old behavior and performance drops. We do NOT try to keep everything (analyzed
trajectories, transcripts) in context — only the **guidance, the agent's direction, and
its durable state** must survive.

## The four durability layers (where guidance lives, ordered by survival)

| Layer | What it is | Survives compaction? |
|---|---|---|
| **L0 — System prompt** | `system_append` (builder: `BUILDER_SYSTEM` in `stage2_break.py`; reviewer: `pipeline/agents/fairness-reviewer.md`). Passed on **every** API call. | **Always** — never compacted. |
| **L1 — Recovery note** | `COMPACTION_RECOVERY_NOTE` / `review.REVIEWER_COMPACTION_NOTE`, re-injected by `sdk.py` the turn **after** compaction is detected. | **Always** — injected post-compaction. |
| **L2 — On-disk guidance** | `pipeline/skills/<stage>/SKILL.md`, the workdir `CLAUDE.md` (`common.build_claude_md`), exemplars, `${TB3_REPO}` rubrics, `${DATASET_ROOT}` negative examples. | Only if the agent **re-reads** it (L0/L1 must tell it to). |
| **L3 — Agent-maintained state** | `./stage2-progress.md` — the agent's own running log (levers tried, results, oracle/no-op status). | On disk; survives because L0/L1 tell the agent to read it first. |

## The rule (apply to EVERY change to agent behavior/direction)

1. **Load-bearing principle → L0.** If a change affects what the agent should do or its
   standing *direction/mental-model*, the compressed version belongs in the **system
   prompt** (L0). Detail/examples may live in L2, but **never** keep the only copy of a
   load-bearing principle in L2 — it evaporates on compaction.
2. **L1 must restate direction + name the re-reads.** The recovery note has to (a) say
   the agent's one-line direction and (b) name the L2 files to re-read (e.g. the SKILL
   "Know the bar" section). Model it on `REVIEWER_COMPACTION_NOTE`, which already does this.
3. **L3 carries run-specific state.** Design L0/L1 so that *system prompt + recovery note
   + progress.md + the named re-reads* are enough to continue correctly.

## The compaction test (run it after any agent-guidance edit)

> Imagine the conversation is reduced to **L0 + L1 + L3 + whatever the agent re-reads**.
> Would the agent still take the right direction with little performance loss?

If a principle you just added would be lost in that reduction, **promote it to L0** (and
make sure L1 points at its L2 detail). If yes, you're done.

## Gotchas

- **L0 strings are passed RAW — no `${...}` substitution.** `BUILDER_SYSTEM` and the
  recovery notes are Python constants handed straight to the SDK; `${TB3_REPO}` etc. would
  appear literally. Use relative paths the agent resolves (`./...`, `.claude/skills/...`),
  or point to the SKILL (L2) which *is* substituted. Substitutions (`apply_subs`) apply to
  **L2 only** — SKILL.md, the workdir CLAUDE.md, and `pipeline/prompts/*.md`. Available
  placeholders: `${TB3_REPO}`, `${CREATOR_UI_REPO}`, `${AGENTS_SRC}`, `${DATASET_ROOT}`.
- **Edits take effect on the NEXT run / restart**, not live processes. `provision_stage`
  refreshes L2 (skills + workdir CLAUDE.md) each run; L0 is read at process start. A
  currently-running agent keeps the guidance it launched with.
- Keep L0 tight. It's on every call (cost + the model's attention budget) — put the
  *principle* there, the *exposition* in L2.
