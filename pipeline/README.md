# TB3 task-generation pipeline

A Claude-Agent-SDK orchestrator that turns a **task proposal** into a finished
**Terminal Bench 3** task that is *objectively model-breaking* (a SOTA agent fails
it), *fair* (a domain expert could solve it from `instruction.md` + the
environment, with non-contrived difficulty), and *QA-passing* (`harbor check` +
`harbor analyze`).

Three independently-runnable stages, each an SDK agent (Claude Opus 4.8) working
in a freshly-provisioned workspace; Python runs the authoritative `harbor`
commands and makes the gate decisions.

| Stage | Script | Loops until | Gate |
|------|--------|-------------|------|
| 1 Author | `stage1_author.py` | a complete, likely-breaking task **passes oracle (1.0) + no-op (<1.0)** | human |
| 2 Break | `stage2_break.py` | **GPT-5.5 fails k/k** AND the **fairness reviewer approves**, then oracle/no-op restored | human |
| 3 QA | `stage3_qa.py` | **`harbor check` + `harbor analyze` pass**, break + oracle/no-op intact, never reducing difficulty | human |

State after every stage is snapshotted **outside the agent workspace** at
`runs/<slug>/snapshots/after-stageN/` for separate inspection.

> **New here? Start with the root [`README.md`](../README.md)** — it covers cloning
> with submodules, the full prerequisite list, and the one-shot bootstrap. This file is
> the deeper operational reference.

## Prerequisites
- `harbor` CLI, `docker` (daemon running), `uv`, Python 3.11+, Node.js + the `claude`
  CLI (the Agent SDK drives it), and the break/analyze agent CLIs (`codex`, `gemini`).
- The TB3 reference repo + the AutoQA repo are vendored as **git submodules** under
  `external/` (`git submodule update --init`). `python pipeline/preflight.py` verifies
  all of the above in one shot.
- **Keys live in a project-root `.env`** (copy `.env.example` → `.env`). `lib/env.py`
  loads it and OVERRIDES the ambient shell, so `.env` is the single source of truth —
  you do NOT `export` keys. Keys:
  - `ANTHROPIC_API_KEY` — Agent SDK (builder/reviewer) + `harbor check`/`analyze`.
  - `OPENAI_API_KEY` — GPT-5.5 break trials (harbor codex agent).
  - `AUTOQA_OPENAI_API_KEY` — AutoQA v3 judge (injected only into the AutoQA
    subprocess; falls back to `OPENAI_API_KEY` if unset).
- `pip install -r pipeline/requirements.txt` (ideally in a venv).

## Run
Run from the repo root. Paths below are repo-relative — no machine-specific paths.
```bash
cp .env.example .env && $EDITOR .env   # fill in keys (loaded automatically, no export)
python pipeline/preflight.py        # checklist; fix any ❌ first

# full pipeline on a proposal (gates between stages, STDIN y/n).
# Example proposals ship in examples/proposals/ so a fresh clone can run immediately:
python pipeline/run_pipeline.py examples/proposals/symplectic-energy-drift.md

# or one stage at a time:
python pipeline/stage1_author.py examples/proposals/symplectic-energy-drift.md
python pipeline/stage2_break.py  symplectic-energy-drift      # slug or run dir
python pipeline/stage3_qa.py     symplectic-energy-drift

# resume the chain from a later stage on an existing run:
python pipeline/run_pipeline.py --from stage2 symplectic-energy-drift
```

At each gate the conductor prints a summary and waits for `y` (proceed) / `n`
(abort); you can append free-text notes after the letter (recorded in state.json).

## Per-task lock & stopping a run (`lib/runguard.py`)
- **Per-task lock:** at most ONE pipeline process may work on a GIVEN task at a time;
  **different tasks run fully in parallel**. The lock is keyed on the task slug — an OS
  `flock` on `runs/.locks/<slug>.lock` — so launching `run_pipeline` (or any stage) for
  the same task twice refuses the second launch and prints who holds it (PID / stage /
  slug), exiting with code 3; launching a *different* task just uses its own lock file
  and proceeds. This prevents two processes from clobbering one task's shared workdir
  while letting you fan out across many tasks. The lock is held for the process lifetime
  and **auto-released the instant the holder dies** (even on `kill -9`/crash) — so there
  are never stale locks. Within one process the builder + reviewer sessions stage 2/3 run
  concurrently, and any subagents, all share that one task lock.
- **Ctrl-C actually works:** each entry point installs SIGINT/SIGTERM handlers
  that force-terminate the whole descendant tree (the SDK `claude` child, harbor,
  shells) via a recursive `pgrep -P` walk (SIGTERM→SIGKILL), then exit. So Ctrl-C
  on the run cleanly kills everything instead of being absorbed as an agent
  "interrupt". If a run is ever wedged, `kill <pid>` (the PID is in the refusal
  message / `runs/.locks/<slug>.lock`) triggers the same teardown.

## Stalled-stream watchdog (`lib/sdk.py`)
Headless SDK runs have no human to hit Ctrl-C when the model's streaming response
hangs (an open HTTPS connection with no bytes flowing), so a stall would otherwise
block the pipeline forever. Every `AgentSession.send()` now runs an **inactivity
watchdog**: if no streamed activity arrives for `[agent].stall_timeout_sec` (default
240s), the dead turn is **interrupted and re-issued in the same session** (all prior
context preserved) up to `[agent].stall_retries` times (default 1). If it still
can't get a response, it raises a **`StallError`** that aborts the stage cleanly
(distinct from the auth error, so you know it's a network hang). The builder log
shows `Ns since last activity; stall at <timeout>s` heartbeats and any
`STALL … interrupting` / `stall retry` lines.

## Watching a run (separate session)
Everything is on disk under `runs/<slug>/`:
- `logs/stageN.builder.log`, `logs/stageN.reviewer.log` — live tool/say stream
  (each SDK turn logs `turn done (cost_usd~…, session_total~…)`).
- `state.json` — stage status, rounds, rewards, gate notes, and a `cost` block per stage.
- `snapshots/after-stageN/` — the task state, a `report.md` (with a **Cost & timing**
  section appended), and a machine-readable `cost.json`.
- `harbor/stageN/...` — raw harbor job dirs + check/analyze JSON.

Tail the logs from another terminal/CC session, e.g. `tail -f runs/<slug>/logs/*.log`.

## Cost & timing (`lib/costs.py`)
Each stage prints a one-line summary when it ends and writes the detail to disk:
```
[stage2] cost/timing — wall 41m13s | SDK $3.92 (builder=$3.18(62t) reviewer=$0.74(9t)) | harbor $2.36+ over 8 runs, 36m03s | ≈ total $6.28+harbor
```
Two cost sources are tracked separately:
- **SDK agents** (builder/reviewer) — dollars/turns/tokens come straight from the
  Claude Agent SDK's per-turn `ResultMessage.total_cost_usd`, summed across the
  stage. **Reliable.**
- **Harbor subprocesses** (GPT-5.5 break trials, `check`/`analyze`) — wall time and
  run counts are exact; **dollars are best-effort** (parsed from `result.json` only
  if a cost field is present, which varies by harbor/agent version). When missing,
  the summary shows `$?`/`+` and the report says so — price it from the token counts
  / raw JSON. This is a known calibration point: the raw JSON is always dumped under
  `harbor/stageN/`, so `lib/harbor.py:_scan_cost_tokens` can be tuned after the first
  real run.

Full breakdown (per-session and per-harbor-run, with tokens) is in
`snapshots/after-stageN/cost.json` and `state.json` → `stages.stageN.cost`.

## Layout
```
pipeline/
  config/pipeline.toml      knobs: model, break agent/model/k, caps, QA models, gate mode
  lib/                      config, harbor wrappers, sdk session, orchestration, common
  skills/                   per-stage SKILL.md (copied into each workdir's .claude/skills)
  agents/fairness-reviewer.md   reviewer system prompt (realism + expert-solvability + non-contrived)
  prompts/                  kickoff prompts (dynamic round prompts are built in code)
  stage1_author.py / stage2_break.py / stage3_qa.py / run_pipeline.py / preflight.py
runs/<slug>/                per-task workspace + snapshots + logs + state (git-ignored)
```

## Config highlights (`config/pipeline.toml`)
- `[agent].model = "claude-opus-4-8"` for all stage agents.
- `[harbor]` break = `codex` / `openai/gpt-5.5`, `break_k = 3` (0/3 = breaking),
  `env = "docker"`, QA `check_model`/`analyze_model` (need ANTHROPIC_API_KEY).
- `[caps]` per-stage round limits (escalate to human on exhaustion).
- `[gate].mode = "stdin"` now; `"auto"` later for hands-off productionization.
- `[autoqa].enabled = false` — AutoQA v3 is intentionally **off** for v1 (harbor
  check/analyze only); the hook is reserved for later.

## Calibration notes
- The `harbor check` / `harbor analyze` JSON schemas vary by harbor version. The
  parsers in `lib/harbor.py` (`_iter_criteria`, `_criterion_failed`) are tolerant
  by design; when a harbor update shifts a schema, every run dumps its raw JSON
  under `runs/<slug>/harbor/stageN/`, making recalibration quick.
- The Claude Agent SDK option names (`ClaudeAgentOptions` fields, `setting_sources`,
  `skills`) can shift between SDK releases; `lib/sdk.py` is the single place to
  adjust if a field is rejected.
