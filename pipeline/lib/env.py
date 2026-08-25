"""API keys: the project-root ``.env`` is the single source of truth.

Why this module exists — the pipeline must never depend on whatever keys
happen to be exported in your shell. Half-day debugging sessions have been
lost to "it worked in my terminal but not in the run" because the shell had
a stale key. So :func:`load_env` reads ``<repo>/.env`` and writes the values
into ``os.environ`` with **override semantics** — the .env always wins over
any ambient export. Subprocesses (harbor / codex / AutoQA) then simply
inherit those values.

One more thing: when a component needs a *different* key from the global one
(AutoQA uses its own OpenAI key, for example), do NOT mutate the global env.
Build a per-subprocess environment with :func:`subprocess_env` instead.
"""

from __future__ import annotations

import os
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent       # .../harbor-task-generator/pipeline
REPO_ROOT = PIPELINE_ROOT.parent                             # .../harbor-task-generator

_loaded_from: Path | None = None


def _parse_env_file(path: Path) -> dict[str, str]:
    """A tiny .env parser. Handles comments, an optional ``export `` prefix,
    and values wrapped in single/double quotes. Deliberately simple — we
    don't need dotenv's full feature set here."""
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (len(val) >= 2) and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def load_env(root: Path | None = None, *, override: bool = True) -> dict[str, str]:
    """Load ``<root>/.env`` into ``os.environ`` (the .env wins by default).

    Safe to call from every entrypoint — calling it again just re-applies the
    same values. Returns the parsed map; empty dict if no .env exists (missing
    keys are surfaced later, by preflight or at first use).
    """
    global _loaded_from
    root = Path(root) if root else REPO_ROOT
    path = root / ".env"
    if not path.exists():
        return {}
    values = _parse_env_file(path)
    for k, v in values.items():
        if override or k not in os.environ:
            os.environ[k] = v
    _loaded_from = path
    return values


def loaded_from() -> Path | None:
    """Path of the .env file that was actually loaded (for diagnostics), or
    None if nothing was loaded. Preflight uses this to show which file won."""
    return _loaded_from


def subprocess_env(**overrides: str | None) -> dict[str, str]:
    """A copy of ``os.environ`` with per-call overrides applied.

    Pass ``KEY=None`` to leave a variable untouched — handy for optional keys:
    ``subprocess_env(OPENAI_API_KEY=os.environ.get("AUTOQA_OPENAI_API_KEY"))``
    only overrides when the source key is actually set. Everything else in the
    child's environment stays exactly as it is here.
    """
    env = dict(os.environ)
    for k, v in overrides.items():
        if v is not None:
            env[k] = v
    return env
