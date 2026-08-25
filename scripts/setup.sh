#!/usr/bin/env bash
# One-shot bootstrap for the TB3 task-generation pipeline.
# Idempotent — safe to re-run. Run from anywhere; it cd's to the repo root.
#
#   ./scripts/setup.sh
#
# It does NOT install the external CLIs (harbor, docker, uv, node+claude, codex, gemini)
# or start Docker — those are host prerequisites (see README.md). The final preflight
# step tells you exactly what is still missing.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> [1/6] Initializing git submodules (TB3 rubrics + Task QA + optional AutoQA) …"
echo "    (private submodules need auth — point .gitmodules at your forks; see SETUP.md)"
git submodule update --init --recursive || {
  echo "    ⚠ Some submodules failed — init the ones you need manually (see SETUP.md)."
}

echo "==> [2/6] Creating venv (.venv) if missing …"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null

echo "==> [3/6] Installing pipeline Python deps …"
pip install -r pipeline/requirements.txt

echo "==> [4/6] Installing Task QA + optional AutoQA deps …"
if [ -f external/task-qa/pyproject.toml ]; then
  pip install -e "external/task-qa[llm,ci-checks]" || {
    echo "    ⚠ Task QA editable install failed — see SETUP.md."
  }
else
  echo "    ⚠ external/task-qa not checked out — skip until submodule is initialized."
fi
if [ -f external/codex-terminal-bench-auto-generation/requirements.txt ]; then
  pip install -r external/codex-terminal-bench-auto-generation/requirements.txt || {
    echo "    ⚠ AutoQA deps failed (optional unless [autoqa] is enabled)."
  }
fi

echo "==> [5/6] Seeding .env if missing …"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    created .env from .env.example — EDIT IT to add your API keys."
fi

echo "==> [6/6] Preflight …"
if python pipeline/preflight.py; then
  echo
  echo "Bootstrap complete. Activate the venv:  source .venv/bin/activate"
else
  echo
  echo "Preflight reported ❌ items above. Typical remaining steps:"
  echo "  - put your API keys in .env (Anthropic / OpenAI / QA judge / Gemini)"
  echo "  - install the host CLIs: harbor, docker (running), uv, node + claude, codex"
  echo "  - initialize external/task-qa and pip install -e external/task-qa[llm,ci-checks]"
  exit 1
fi
