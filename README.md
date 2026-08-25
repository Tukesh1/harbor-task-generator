# harbor-task-generator — Terminal Bench 3 task-generation pipeline

A Python **conductor** that turns a one-page **task proposal** into a finished
[Terminal Bench 3](https://github.com/harbor-framework/terminal-bench-3) (TB3) task that is
**model-breaking** (a SOTA agent fails it), **fair** (a domain expert could solve it from the
instructions + environment alone), and **QA-passing** (`harbor check` + `harbor analyze`). It
drives stage **agents** (a builder + an independent fairness reviewer) via the Claude Agent SDK,
while Python runs the authoritative `harbor` trials and owns every gate decision.

Three Claude Code sessions / stages create a task:

1. **Stage 1 — author.** Generate a working skeleton from a proposal. The builder knows it'll be
   judged on model-breaking + quality, but isn't graded yet. Non-blocking. Gate: oracle (reward
   1.0) + no-op (reward < 1.0) pass.
2. **Stage 2 — break.** Harden until **GPT-5.5 fails k/k** *and* the fairness reviewer approves,
   then restore oracle/no-op.
3. **Stage 3 — QA.** Make it pass all QA checks (`harbor check` + `harbor analyze` + the
   Task QA checks) while keeping the break intact.

---

> **Setting up a new machine? Follow [`SETUP.md`](SETUP.md)** — the canonical setup guide
> (submodules incl. `external/task-qa`, Python 3.11+ (3.12 recommended), API keys, editable
> install). The Quick start below is the short version; where it disagrees with `SETUP.md`,
> `SETUP.md` wins.

## Quick start

```bash
# 1. Clone WITH submodules (TB3 rubrics + the QA engine live under external/ — see "Cloning" below)
git clone --recurse-submodules git@github.com:Tukesh1/harbor-task-generator.git harbor-task-generator
cd harbor-task-generator

# 2. Bootstrap: submodules + venv + Python deps + .env + preflight, in one shot
./scripts/setup.sh

# 3. Put your API keys in .env  (it was seeded from .env.example)
$EDITOR .env

# 4. Verify EVERYTHING is wired (keys, CLIs, docker, submodules, QA engine). This is the
#    single go/no-go gate — fix any ❌ before running.
python pipeline/preflight.py            # add --live to also test the Anthropic key

# 5. Run the pipeline on a shipped example proposal
python pipeline/run_pipeline.py examples/proposals/symplectic-energy-drift.md
```

## Batch runs (seeds CSV)

To generate many tasks unattended, drive the pipeline from a CSV with
[`scripts/run_seeds.py`](scripts/run_seeds.py). Each row's description column is written to a
proposal and run **end-to-end with no human gate** (it passes `run_pipeline.py --gate-mode auto`)
as an isolated subprocess; a top-level semaphore runs **N at a time** (default 2, `-j`/`--max-parallel`).

```bash
# All rows, 2 at a time (CSV columns: Slug, Domain, "description / Seed Form Content"):
python scripts/run_seeds.py --seeds-csv examples/seeds.example.csv

python scripts/run_seeds.py --seeds-csv seeds.csv -j 3 --limit 6      # first 6, 3 concurrent
python scripts/run_seeds.py --seeds-csv seeds.csv --slugs a,b         # specific Slugs
python scripts/run_seeds.py --seeds-csv seeds.csv --domain Systems    # Domain substring
python scripts/run_seeds.py --seeds-csv seeds.csv --dry-run           # preview selection only
```

By default the runner **streams each pipeline's live output to your console** — the same
round-by-round commentary you get from `python pipeline/stageN.py` (oracle/no-op/breaking status,
harbor trials, reviewer verdicts, costs) — tagged with `[slug]` when more than one runs at once so
the streams stay readable. Pass `--quiet` for just START/DONE lines + the summary.

**Stopping the batch** (Ctrl-C, or `kill` the PID) tears down *everything* — every in-flight
`run_pipeline` plus its `claude`/harbor/docker/shell descendants — via the same process-tree
teardown a single stage script uses (recursive `pgrep -P` walk, SIGTERM→3s→SIGKILL, following
PPID so even `setsid`'d children are caught).

Each seed's run lands in `runs/<slug>/` (its proposal under `runs/_seed_proposals/`, its captured
stdout under `runs/_seed_logs/<slug>.log` — both gitignored). To watch from **another** terminal:
**`python watch_run.py --list`** (every run + which are live — the best batch overview),
`python watch_run.py` (menu among the live runs), or `python watch_tui.py <slug>` (scrollable TUI
for one run).

## Cloning (submodules)

Two external repos are vendored as **git submodules** under `external/` (see [`.gitmodules`](.gitmodules)):

| Submodule | Source | Why it's needed |
|---|---|---|
| `external/terminal-bench-3` | `harbor-framework/terminal-bench-3` (public) | Rubrics for `harbor check`/`analyze` + good-task standards the agents read. |
| `external/task-qa` | **your fork** (private) | The QA engine — Stage-3 LLM checks + Stage-2/3 SOTA checks. Editable-installed (see [`SETUP.md`](SETUP.md)). |
| `external/codex-terminal-bench-auto-generation` | optional | AutoQA v3 — **disabled by default**; skip unless you re-enable `[autoqa]`. |

If you cloned without `--recurse-submodules`, run
`git submodule update --init external/terminal-bench-3 external/task-qa`.

**Private submodule auth.** Update `.gitmodules` with your repository URLs, then
`git submodule sync && git submodule update --init external/task-qa`. Use SSH keys,
HTTPS + PAT, or a `url.*.insteadOf` rewrite — whatever your host supports.

## Prerequisites

`./scripts/setup.sh` installs the Python deps and AutoQA's deps; the **host CLIs below are not
auto-installed**. `python pipeline/preflight.py` checks every item here and tells you what's
missing.

| Tool | Used for | Install |
|---|---|---|
| **Python ≥ 3.11** (3.12 recommended) | the conductor + the Task QA editable install | — |
| **Node.js ≥ 18** + **`claude`** CLI | the Claude Agent SDK drives the Claude Code CLI | `npm i -g @anthropic-ai/claude-code` |
| **`harbor`** | authoritative trials, check, analyze | the harbor CLI |
| **`docker`** (daemon running) | every harbor trial runs in a container | Docker Desktop / engine |
| **`uv`** | verifier images install via uv | https://docs.astral.sh/uv/ |
| **`codex`** | GPT-5.5 break trials (`break_agent`) | `npm i -g @openai/codex` |
| **`gemini`** | the analyze-matrix cross-model leg | `npm i -g @google/gemini-cli` |

**API keys** live in a project-root `.env` (copy from `.env.example`). `pipeline/lib/env.py`
loads it and **overrides the ambient shell**, so `.env` is the single source of truth — you do
*not* `export` keys.

| Key | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | Agent SDK (builder + reviewer) and `harbor check`/`analyze`. |
| `OPENAI_API_KEY` | GPT-5.5 break trials (codex agent). |
| `QA_JUDGE_OPENAI_API_KEY` | the Task QA judge (LLM + SOTA checks); falls back to `TASK_QA_OPENAI_API_KEY` → `AUTOQA_OPENAI_API_KEY` → `OPENAI_API_KEY`. |
| `GEMINI_API_KEY` | the analyze-matrix gemini leg (`gemini/gemini-3.1-pro-preview` via terminus-2). |
| `AUTOQA_OPENAI_API_KEY` | AutoQA v3 judge — only if you re-enable `[autoqa]` (falls back to `OPENAI_API_KEY`). |

> **AutoQA has its own (heavy) Python deps.** It runs in the interpreter named by
> `[autoqa].python` in `pipeline/config/pipeline.toml` (default: `python`). `setup.sh` installs
> them into the venv; if they conflict with the pipeline deps, point `[autoqa].python` at a
> dedicated interpreter and install
> `external/codex-terminal-bench-auto-generation/requirements.txt` there.

## Manual setup (without the bootstrap script)

```bash
git submodule update --init --recursive
python3 -m venv .venv && source .venv/bin/activate
pip install -r pipeline/requirements.txt           # or: pip install .
pip install -r external/codex-terminal-bench-auto-generation/requirements.txt   # AutoQA
cp .env.example .env && $EDITOR .env
python pipeline/preflight.py
```

## Layout

```
README.md                     this file (start here)
pyproject.toml                Python deps / packaging metadata
scripts/setup.sh              one-shot bootstrap
scripts/run_seeds.py          batch driver: run a seeds CSV end-to-end, N tasks at a time
.env.example                  template for the project-root .env (gitignored)
examples/proposals/           shipped input proposals (run any of these)
examples/seeds.example.csv    example seeds CSV for scripts/run_seeds.py
external/                     git submodules (TB3 + AutoQA) — see .gitmodules
pipeline/
  README.md                   deeper operational reference
  config/pipeline.toml        all knobs (model, break agent/k, caps, QA models, gate mode)
  lib/                        config, env, harbor wrappers, SDK session, orchestration, costs
  levers/                     VENDORED reference: lever catalog + difficulty doc (pipeline-owned)
  skills/  agents/  prompts/  per-stage agent guidance (L2) + reviewer system prompt
  stage1_author.py / stage2_break.py / stage3_qa.py / run_pipeline.py / preflight.py
watch_run.py / watch_tui.py   live monitors for a running pipeline
runs/<slug>/                  per-task workspace + snapshots + logs + state (gitignored)
```

Where the paths to the external repos come from, and how to override them, is documented in
`pipeline/config/pipeline.toml` (`tb3_repo` / `$TB3_REPO`, `autoqa_cwd` / `$AUTOQA_CWD`).

## Operating a run

See [`pipeline/README.md`](pipeline/README.md) for: the per-stage gate semantics, the per-task
lock, the stalled-stream watchdog, cost/timing accounting, and how to watch a run from another
terminal. The agent-guidance durability doctrine (how changes survive SDK context compaction)
is in [`CLAUDE.md`](CLAUDE.md).

## Security

`.env` is gitignored and must never be committed. If keys are ever exposed, rotate them.
