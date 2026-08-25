# SETUP — running this pipeline on a fresh machine

Follow this guide top to bottom on a clean machine. When anything here disagrees with
an older note elsewhere, **this file wins**.

`python pipeline/preflight.py --live` is the **single go/no-go gate** — every step below
exists to make that command print **"All good — ready to run."**

---

## 0. What you're installing

| Piece | What it is | How it's obtained |
|---|---|---|
| The conductor | the Python pipeline (`pipeline/`) | this repo |
| `external/terminal-bench-3` | TB3 rubrics + good-task standards (**public**) | git submodule |
| `external/task-qa` | the **QA check engine** (LLM + SOTA checks) | git submodule + editable pip install |
| `external/codex-terminal-bench-auto-generation` | AutoQA v3 — **disabled by default** | optional git submodule |
| Host CLIs | `harbor`, `docker`, `uv`, `node`+`claude`, `codex` | installed on the host |
| API keys | Anthropic / OpenAI / QA judge / Gemini | gitignored project-root `.env` |

Before your first run, edit `[task]` in `pipeline/config/pipeline.toml` with your
organization's default `org`, `author_name`, and `author_email` for generated tasks.

---

## 1. Prerequisites

| Tool | Used for |
|---|---|
| **Python ≥ 3.11** (3.12 recommended) | the venv for the conductor and the Task QA package |
| **Node.js ≥ 18** + **`claude`** CLI | the Claude Agent SDK |
| **`docker`** (daemon running) | harbor trials |
| **`uv`** | verifier images |
| **`harbor`** (0.13.x) | trials, `check`, `analyze` |
| **`codex`** | GPT-5.5 break trials |

---

## 2. Clone with submodules

```bash
git clone --recurse-submodules git@github.com:Tukesh1/harbor-task-generator.git harbor-task-generator
cd harbor-task-generator
git config submodule.recurse true
```

If you cloned without submodules:

```bash
git submodule update --init external/terminal-bench-3 external/task-qa
```

Point `.gitmodules` at **your** QA-engine fork before initializing `external/task-qa`.

---

## 3. Python venv + install

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -r pipeline/requirements.txt
uv pip install -e "external/task-qa[llm,ci-checks]"
```

Use an **editable** install (not `uv tool install`) so preflight can verify the vendored
source. The QA CLI binary name defaults to `task-qa` in `[task_qa].bin` — change it in
`pipeline/config/pipeline.toml` if your fork ships a different entry point.

---

## 4. API keys → `.env`

```bash
cp .env.example .env && $EDITOR .env
```

| Key | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | Agent SDK + harbor check/analyze |
| `OPENAI_API_KEY` | GPT-5.5 break trials |
| `QA_JUDGE_OPENAI_API_KEY` | Task QA judge (falls back to `TASK_QA_OPENAI_API_KEY` / `OPENAI_API_KEY`) |
| `GEMINI_API_KEY` | analyze-matrix gemini leg |

---

## 5. Preflight

```bash
python pipeline/preflight.py --live
```

---

## 6. Run

```bash
python pipeline/run_pipeline.py examples/proposals/symplectic-energy-drift.md
```

Operational details: [`pipeline/README.md`](pipeline/README.md). All knobs live in
[`pipeline/config/pipeline.toml`](pipeline/config/pipeline.toml).
