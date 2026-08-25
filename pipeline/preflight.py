#!/usr/bin/env python3
"""Preflight — the ONE go/no-go gate before any run: `python pipeline/preflight.py`

Philosophy: this must be a STRONG signal, not a checkbox dump. If every line
prints ✅ you can genuinely start a run end-to-end; any ❌ is a hard stop with
a concrete fix hint printed right there. We check everything a real run will
actually touch — because the worst failure mode is discovering a broken piece
two hours into a paid run:

  * the project .env and every API key the config uses (Anthropic, OpenAI,
    Task QA judge, Gemini for the analyze matrix) — presence, and with --live,
    actual validity via one cheap authenticated GET per provider;
  * the Claude Agent SDK AND the `claude` CLI + Node it drives;
  * the harbor / docker / uv toolchain, plus agent CLIs the config selects
    (codex for break trials, gemini when the matrix uses gemini-cli);
  * a REAL docker bind-mount under runs_dir (the macOS ~/Documents TCC trap —
    `docker info` passing proves nothing here);
  * vendored submodules (TB3 rubrics/standards, task-qa) are truly checked
    out, not registered-but-empty dirs;
  * Task QA / AutoQA imports in exactly the interpreters the config names.

Run with --live to also make one real SDK call (verifies the key works, not
just that it exists).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import load_config

# The Python module name of the Task QA engine (the vendored external/task-qa
# package). If you vendor a fork with a different import name, change it here.
TASK_QA_MODULE = "task_qa"


def _http_status(url: str, headers: dict, timeout: int = 15) -> tuple[bool, str]:
    """GET ``url`` and report whether auth succeeded. 200 → ok; 401/403 → bad key.
    Network/other errors are reported but NOT treated as a bad key (we don't want
    a flaky network to tell you your valid key is broken)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (200 <= r.status < 300), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"HTTP {e.code} — key rejected"
        return True, f"HTTP {e.code} (reachable; auth not rejected)"
    except Exception as e:
        return False, f"unreachable: {e}"


def _probe_openai(key: str, model: str | None = None) -> tuple[bool, str]:
    """Validate an OpenAI key (list-models GET), and optionally that a specific
    model is accessible with it — used for the QA-judge key, whose model can
    be restricted independently of the key itself."""
    if not key:
        return False, "key not set"
    ok, detail = _http_status("https://api.openai.com/v1/models",
                              {"Authorization": f"Bearer {key}"})
    if ok and model:
        m_ok, m_detail = _http_status(f"https://api.openai.com/v1/models/{model}",
                                      {"Authorization": f"Bearer {key}"})
        if not m_ok:
            return False, f"key valid but model '{model}' not accessible ({m_detail})"
    return ok, detail


def _probe_anthropic(key: str) -> tuple[bool, str]:
    """Validate the Anthropic key against the models endpoint (cheap GET)."""
    if not key:
        return False, "key not set"
    return _http_status("https://api.anthropic.com/v1/models",
                        {"x-api-key": key, "anthropic-version": "2023-06-01"})


def _probe_gemini(key: str) -> tuple[bool, str]:
    """Validate a Gemini key against the generative-language API.

    Cannot reuse ``_http_status``: unlike OpenAI/Anthropic (which reject a bad key with
    401/403), Google's generative-language API returns **HTTP 400 with reason
    ``API_KEY_INVALID``** for a bad/malformed key — so the generic 401/403-only check
    would wave an invalid key through as ✅. The endpoint takes the key as its only auth
    param, so any 4xx client error here means the key: classify 200 → valid;
    400/401/403 → rejected; everything else (429/5xx/network) → reachable, not rejected."""
    if not key:
        return False, "key not set"
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return (200 <= r.status < 300), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        if e.code in (400, 401, 403):
            why = "API_KEY_INVALID" if "API_KEY_INVALID" in body else f"HTTP {e.code}"
            return False, f"{why} — key rejected"
        return True, f"HTTP {e.code} (reachable; auth not rejected)"
    except Exception as e:
        return False, f"unreachable: {e}"


def _task_qa_health(cfg) -> list[tuple[str, bool, str]]:
    """Verify the Task QA setup end-to-end-ish: the CLI is on PATH, it resolves
    to the VENDORED submodule (not some other installed copy), the trajectory-
    injection patch is present, and every configured check name is registered."""
    out: list[tuple[str, bool, str]] = []
    bin_ok = shutil.which(cfg.tqa_bin) is not None
    out.append((f"Task QA CLI on PATH ('{cfg.tqa_bin}')", bin_ok,
                "pip install -e external/task-qa"))
    if not bin_ok:
        return out
    try:
        loc = subprocess.run(
            [sys.executable, "-c",
             f"import {TASK_QA_MODULE},os; "
             f"import {TASK_QA_MODULE}.checks.shared.trajectory as t; "
             f"print(os.path.dirname({TASK_QA_MODULE}.__file__)); "
             "print(hasattr(t,'context_from_run_dir'))"],
            capture_output=True, text=True, timeout=30)
        lines = (loc.stdout or "").strip().splitlines()
        pkg = lines[0] if lines else "?"
        patched = len(lines) > 1 and lines[1].strip() == "True"
        in_submodule = "external/task-qa" in pkg
        out.append(("Task QA CLI resolves to the vendored submodule", in_submodule,
                    f"active install is {pkg} — run pip install -e external/task-qa"))
        out.append(("trajectory-injection patch present (context_from_run_dir)", patched,
                    "the submodule is missing the injection patch — re-apply it"))
    except Exception as e:
        out.append(("Task QA import probe", False, str(e)))
    try:
        p = subprocess.run([cfg.tqa_bin, "checks", "list"], capture_output=True, text=True, timeout=60)
        listed = (p.stdout or "") + (p.stderr or "")
        want = set(cfg.tqa_llm_checks) | set(cfg.tqa_stage2_checks) | set(cfg.tqa_stage3_sota_checks)
        missing = sorted(c for c in want if c not in listed)
        out.append(("Task QA checks list runs + all configured checks registered",
                    p.returncode == 0 and not missing,
                    f"rc={p.returncode}; missing checks: {missing}" if missing else f"rc={p.returncode}"))
    except Exception as e:
        out.append(("Task QA checks list runs", False, str(e)))
    return out


def _docker_bind_mount_ok(runs_dir: Path) -> tuple[bool, str]:
    """Reproduce what harbor does: bind-mount a path under runs_dir into a container.

    `docker info` can pass while bind-mounts still fail — e.g. macOS TCC blocks Docker
    Desktop from mounting under ~/Documents/~/Desktop/~/Downloads, or File Sharing was
    reset by an update. That denial only shows up at container start (the failure that
    silently wasted a run). Returns (ok, detail). Side effects: one `--rm` no-op container.
    """
    probe = runs_dir / ".mountprobe"
    try:
        probe.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"cannot create probe dir {probe}: {e}"
    # Prefer a local image (no network pull). The mount is created BEFORE the entrypoint,
    # so any image surfaces a mount denial; we just need one with a shell.
    img = "alpine"
    try:
        imgs = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                              capture_output=True, text=True, timeout=15).stdout.split()
        local = [i for i in imgs if i and i != "<none>:<none>"]
        if local and "alpine:latest" not in local:
            img = local[0]
    except Exception:
        pass
    try:
        p = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "sh", "-v", f"{probe}:/probe", img, "-c", "true"],
            capture_output=True, text=True, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode == 0:
            return True, ""
        if "operation not permitted" in out or "creating mount source path" in out:
            return False, (f"Docker cannot bind-mount under {runs_dir}. On macOS this is a TCC / "
                           "File-Sharing denial (the ~/Documents case): grant Docker Desktop Full "
                           "Disk Access + share /Users and restart Docker, or move runs_root off "
                           "a protected dir. Harbor trials WILL fail at container start until fixed.")
        return False, f"docker bind-mount probe failed (rc={p.returncode}): …{out.strip()[-300:]}"
    except subprocess.TimeoutExpired:
        return False, "docker bind-mount probe timed out (Docker Desktop wedged?)"
    except Exception as e:
        return False, f"probe error: {e}"
    finally:
        try:
            probe.rmdir()
        except Exception:
            pass


def _submodule_populated(path: Path) -> bool:
    """A registered-but-uninitialized submodule is an empty dir. 'Populated' = exists and
    has any tracked content (we look for a non-dotfile entry)."""
    if not path.exists():
        return False
    try:
        return any(c.name != ".git" for c in path.iterdir())
    except Exception:
        return False


def _autoqa_import_ok(cfg) -> tuple[bool, str]:
    """Run `<autoqa_python> -c 'import task_evaluation.autoqa_v3'` from autoqa_cwd. This is
    the honest check that AutoQA is usable: it catches a missing submodule AND missing deps
    (AutoQA has its own requirements.txt that must be installed into [autoqa].python)."""
    py = cfg.autoqa_python
    if shutil.which(py) is None and not Path(py).exists():
        return False, f"[autoqa].python='{py}' not found — set it to an interpreter that has AutoQA's deps"
    try:
        p = subprocess.run([py, "-c", "import task_evaluation.autoqa_v3"],
                           cwd=str(cfg.autoqa_cwd), capture_output=True, text=True, timeout=90)
        if p.returncode == 0:
            return True, ""
        tail = (p.stderr or p.stdout or "").strip()[-300:]
        return False, (f"`{py} -m task_evaluation.autoqa_v3` is not importable from {cfg.autoqa_cwd}. "
                       f"Install AutoQA deps into that interpreter: "
                       f"`{py} -m pip install -r {cfg.autoqa_cwd / 'requirements.txt'}`. ({tail})")
    except subprocess.TimeoutExpired:
        return False, "AutoQA import timed out (>90s)"
    except Exception as e:
        return False, f"AutoQA import error: {e}"


def main() -> int:
    """Run every check section in order and exit 0 only if ALL passed. The
    `check()` helper below flips the module-level `ok` flag on any failure —
    that's the whole control flow."""
    ap = argparse.ArgumentParser(description="Preflight checks for the pipeline.")
    ap.add_argument("--live", action="store_true",
                    help="also make one real SDK call to verify auth actually works")
    args = ap.parse_args()
    cfg = load_config()
    ok = True

    def check(label, cond, hint=""):
        nonlocal ok
        mark = "✅" if cond else "❌"
        print(f"  {mark} {label}" + ("" if cond else f"   → {hint}"))
        if not cond:
            ok = False

    def section(title):
        print(f"\n{title}")

    print("Preflight for the TB3 task-generation pipeline")

    # ── .env + API keys ──────────────────────────────────────────────────────────────
    section("Keys (.env — sourced here, not the ambient shell)")
    from lib.env import loaded_from
    src = loaded_from()
    check(".env loaded", src is not None, "create a .env in the project root (see .env.example)")
    if src is not None:
        print(f"     (loaded {src})")
    check("ANTHROPIC_API_KEY set (Agent SDK builder/reviewer + harbor check/analyze)",
          bool(os.environ.get("ANTHROPIC_API_KEY")), "set ANTHROPIC_API_KEY in .env")
    check("OPENAI_API_KEY set (GPT-5.5 break trials via the codex agent)",
          bool(os.environ.get("OPENAI_API_KEY")), "set OPENAI_API_KEY in .env")
    check("AUTOQA_OPENAI_API_KEY set (AutoQA v3 judge — legacy)",
          bool(os.environ.get("AUTOQA_OPENAI_API_KEY")),
          "set AUTOQA_OPENAI_API_KEY in .env (falls back to OPENAI_API_KEY if absent)")
    if cfg.tqa_enabled:
        check("QA_JUDGE_OPENAI_API_KEY set (Task QA LLM/SOTA judge)",
              bool(os.environ.get("QA_JUDGE_OPENAI_API_KEY")
                   or os.environ.get("TASK_QA_OPENAI_API_KEY")
                   or os.environ.get("AUTOQA_OPENAI_API_KEY")
                   or os.environ.get("OPENAI_API_KEY")),
              "set QA_JUDGE_OPENAI_API_KEY in .env (falls back to TASK_QA_OPENAI_API_KEY, "
              "AUTOQA_OPENAI_API_KEY, then OPENAI_API_KEY)")
    # Gemini is required iff the analyze matrix actually uses a gemini agent (config-aware).
    # A gemini MODEL needs the key (terminus-2 or gemini-cli both call Google); only the
    # gemini-cli AGENT additionally needs the `gemini` CLI binary on PATH.
    needs_gemini = any("gemini" in m.get("model", "") for m in cfg.analyze_matrix)
    needs_gemini_cli = any(m.get("agent") == "gemini-cli" for m in cfg.analyze_matrix)
    if needs_gemini:
        check("GEMINI_API_KEY set (analyze-matrix gemini model leg)",
              bool(os.environ.get("GEMINI_API_KEY")),
              "set GEMINI_API_KEY in .env — the default analyze matrix runs gemini; a missing key "
              "silently drops a model from the accept gate")

    # ── Python / Agent SDK ───────────────────────────────────────────────────────────
    section("Python / Claude Agent SDK")
    try:
        import claude_agent_sdk  # noqa: F401
        check("claude-agent-sdk importable", True)
    except Exception as e:
        check("claude-agent-sdk importable", False, f"pip install -r pipeline/requirements.txt ({e})")
    for mod in ("rich", "textual"):
        try:
            __import__(mod)
            check(f"{mod} importable (watch_run/watch_tui)", True)
        except Exception:
            check(f"{mod} importable (watch_run/watch_tui)", False,
                  "pip install -r pipeline/requirements.txt")

    # ── CLI toolchain ────────────────────────────────────────────────────────────────
    section("CLI toolchain (must be on PATH)")
    # The Agent SDK drives the Claude Code CLI, which needs Node.
    check("node on PATH (Claude Code runtime)", shutil.which("node") is not None,
          "install Node.js ≥18 (the claude CLI the Agent SDK drives needs it)")
    check("claude CLI on PATH (the Agent SDK drives it)", shutil.which("claude") is not None,
          "install Claude Code: npm i -g @anthropic-ai/claude-code")
    check(f"harbor on PATH ('{cfg.harbor_bin}')", shutil.which(cfg.harbor_bin) is not None,
          "install the harbor CLI")
    check("uv on PATH (verifier images install via uv; harbor uses it)",
          shutil.which("uv") is not None, "install uv (https://docs.astral.sh/uv/)")
    # Break agent CLI (config-aware): codex by default.
    if cfg.break_agent == "codex":
        check("codex on PATH (break_agent='codex' — GPT-5.5 break trials)",
              shutil.which("codex") is not None, "install the codex CLI (npm i -g @openai/codex)")
    # Analyze-matrix agent CLIs (config-aware). terminus-2 routes gemini via litellm (no CLI);
    # only the gemini-cli AGENT needs the binary.
    if needs_gemini_cli:
        check("gemini on PATH (analyze-matrix gemini-cli leg)", shutil.which("gemini") is not None,
              "install the gemini CLI (npm i -g @google/gemini-cli)")

    # ── Docker ───────────────────────────────────────────────────────────────────────
    section("Docker (harbor runs every trial in a container)")
    docker_present = shutil.which("docker") is not None
    check("docker on PATH", docker_present, "install Docker Desktop / docker engine")
    docker_ok = False
    if docker_present:
        try:
            docker_ok = subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
        except Exception:
            docker_ok = False
        check("docker daemon reachable", docker_ok, "start Docker Desktop / dockerd")
        if docker_ok:
            # `docker info` passing is NOT enough — verify a real bind-mount under runs_dir,
            # which is what every harbor trial does (and where the ~/Documents TCC denial hits).
            mnt_ok, mnt_detail = _docker_bind_mount_ok(cfg.runs_dir)
            check(f"docker can bind-mount runs_dir ({cfg.runs_dir})", mnt_ok, mnt_detail)

    # ── Submodules / reference content ───────────────────────────────────────────────
    section("Reference content (git submodules + vendored docs)")
    tb3_ok = _submodule_populated(cfg.tb3_repo)
    check(f"TB3 submodule checked out ({cfg.tb3_repo})", tb3_ok,
          "run: git submodule update --init --recursive   (or set $TB3_REPO to a checkout)")
    if tb3_ok:
        check(f"  implementation rubric ({cfg.impl_rubric.name})", cfg.impl_rubric.exists(),
              "TB3 submodule is on an unexpected commit — see .gitmodules (pinned branch)")
        check(f"  trial-analysis rubric ({cfg.trial_rubric.name})", cfg.trial_rubric.exists())
        check(f"  trial-analysis job prompt ({cfg.trial_job_prompt.name})", cfg.trial_job_prompt.exists())
        check("  TB3 CLAUDE.md (good-task definition)", (cfg.tb3_repo / "CLAUDE.md").exists())
    # Vendored, in-repo (no external repo): lever catalog + difficulty reference.
    levers = cfg.pipeline_root / "levers" / "hardening_guidelines.md"
    difficulty = cfg.pipeline_root / "levers" / "difficulty_vetting_and_levers.md"
    check("vendored lever catalog (pipeline/levers/hardening_guidelines.md)", levers.exists())
    check("vendored difficulty reference (pipeline/levers/difficulty_vetting_and_levers.md)",
          difficulty.exists())

    # ── AutoQA (now disabled by default — replaced by Task QA) ───────────────────────
    section("AutoQA v3")
    if not cfg.autoqa_enabled:
        print("  (disabled in config — [autoqa].enabled = false; use [task_qa] below)")
    else:
        aq_ok = _submodule_populated(cfg.autoqa_cwd)
        check(f"AutoQA submodule checked out ({cfg.autoqa_cwd})", aq_ok,
              "run: git submodule update --init --recursive   (or set $AUTOQA_CWD)")
        if aq_ok:
            imp_ok, imp_detail = _autoqa_import_ok(cfg)
            check(f"AutoQA importable via [autoqa].python='{cfg.autoqa_python}'", imp_ok, imp_detail)

    # ── Task QA (the QA check engine: Stage-3 LLM checks + Stage-2/3 SOTA checks) ─────
    section("Task QA")
    if not cfg.tqa_enabled:
        print("  (disabled in config — [task_qa].enabled = false)")
    else:
        for label, cond, hint in _task_qa_health(cfg):
            check(label, cond, hint)
        print(f"     Stage-3 LLM checks: {cfg.tqa_llm_checks}")
        print(f"     Stage-2 SOTA checks: {cfg.tqa_stage2_checks}")
        print(f"     Stage-3 SOTA checks: {cfg.tqa_stage3_sota_checks}")

    # ── Live auth (opt-in) ─────────────────────────────────────────────────────────────
    if args.live:
        from lib.sdk import run_live_auth_check
        print("\n  … running a live SDK auth ping (one cheap call) …")
        live_ok, detail = run_live_auth_check(cfg.model)
        check(f"live SDK auth works (model={cfg.model})", live_ok,
              f"the key is set but the SDK rejected it: {detail}. Use a valid Anthropic API "
              f"key (sk-ant-…); subscription/OAuth is not accepted by the SDK.")

        # Probe every provider key the run actually uses (one cheap GET each, no tokens spent).
        section("Live key probes (one auth GET per provider — no tokens spent)")
        ah = os.environ.get("OPENAI_API_KEY")
        ok, d = _probe_openai(ah)
        check(f"OpenAI break-trial key valid (OPENAI_API_KEY, …{(ah or '')[-4:]})", ok, d)
        if cfg.tqa_enabled:
            tq = (os.environ.get("QA_JUDGE_OPENAI_API_KEY")
                  or os.environ.get("TASK_QA_OPENAI_API_KEY")
                  or os.environ.get("AUTOQA_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"))
            judge_model = os.environ.get("TASK_QA_LLM_MODEL", "gpt-5.4-mini").split(":", 1)[-1]
            ok, d = _probe_openai(tq, model=judge_model)
            check(f"OpenAI Task QA-judge key valid + '{judge_model}' accessible "
                  f"(…{(tq or '')[-4:]})", ok, d)
        ant = os.environ.get("ANTHROPIC_API_KEY")
        ok, d = _probe_anthropic(ant)
        check(f"Anthropic key valid (ANTHROPIC_API_KEY, …{(ant or '')[-4:]})", ok, d)
        if needs_gemini:
            gem = os.environ.get("GEMINI_API_KEY")
            ok, d = _probe_gemini(gem)
            check(f"Gemini key valid (GEMINI_API_KEY, …{(gem or '')[-4:]})", ok, d)
    else:
        print("\n  (run with --live to actually verify auth + probe every provider key — "
              "'set' above only checks the var exists)")

    print(f"\nConfig: model={cfg.model}  break={cfg.break_agent}/{cfg.break_model} k={cfg.break_k}  "
          f"env={cfg.harbor_env}  gate={cfg.gate_mode}  "
          f"autoqa={cfg.autoqa_enabled}  task_qa={cfg.tqa_enabled}")
    print("\n" + ("All good — ready to run." if ok else "Fix the ❌ items above before running."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
