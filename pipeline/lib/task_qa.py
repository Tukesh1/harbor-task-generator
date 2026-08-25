"""Task QA integration — the conductor-side invocation layer.

Two legs, both subprocess calls to the vendored QA CLI (the
``external/task-qa`` engine, editable-installed; default binary ``task-qa``,
configurable via ``[task_qa].bin``):

1. ``run_llm_checks`` — non-SOTA LLM checks (Stage 3 Phase A). This replaces
   the AutoQA v3 'llm_only' leg. Binary gate: any ``ISSUE_FOUND`` blocks;
   minor concerns inside a passing check are just warnings.

2. ``run_sota_check_matrix`` — SOTA-trajectory checks (Stage 2 Phase C +
   Stage 3 Phase B). The clever bit: instead of paying for a fresh SOTA
   trial, we INJECT each of our own break-trial run dirs via the
   ``TASK_QA_SOTA_RUN_DIR`` env var, so these checks judge exactly the same
   trajectories as the break gate — zero extra trial cost. Per-check
   aggregation uses the same green/yellow/red 3-state rule as harbor analyze.

One gotcha to know: the vendored CLI strips native API key env names and only
reads bridged ``TASK_QA_*`` names. Every invocation therefore goes through
``_qa_subprocess_env``, which does that bridging in one place.
"""
from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .harbor import _run, _dump_proc

# Env var the patched TrajectoryProvider reads to reuse an existing harbor run
# dir (skipping a fresh SOTA trial). The vendored CLI must read THIS name.
TQA_SOTA_RUN_DIR_ENV = "TASK_QA_SOTA_RUN_DIR"


@dataclass
class TqaSuiteResult:
    """Outcome of the non-SOTA LLM checks leg (Stage 3 Phase A)."""
    acceptable: bool
    issues: list[str]
    per_check: dict
    ran: bool
    out_dir: Path
    summary: str
    duration_sec: float | None = None
    cost_usd: float | None = None
    tokens: dict | None = None
    raw: dict | None = None


@dataclass
class SotaCheckMatrixResult:
    """Outcome of SOTA-trajectory checks run across our break trajectories."""
    out_dir: Path
    ran: bool
    colors: dict
    per_traj: dict
    red_checks: list
    yellow_checks: list
    error_checks: list
    n_trajectories: int
    checks: list
    summary: str
    duration_sec: float | None = None
    cost_usd: float | None = None
    tokens: dict | None = None
    raw: dict | None = None


def _judge_openai_key() -> str | None:
    """Resolve the OpenAI key used for QA judge calls. Accepts a couple of
    legacy/alternate env names before falling back to the global OpenAI key,
    so an existing .env keeps working no matter which name you set."""
    return (os.environ.get("QA_JUDGE_OPENAI_API_KEY")
            or os.environ.get("TASK_QA_OPENAI_API_KEY")
            or os.environ.get("AUTOQA_OPENAI_API_KEY")
            or os.environ.get("OPENAI_API_KEY"))


def _qa_subprocess_env(cfg, extra: dict | None = None) -> dict:
    """Subprocess env for the QA CLI: bridge our key names into the
    ``TASK_QA_*`` names the vendored engine expects. All bridging lives here —
    callers should never hand-roll their own env for the QA CLI."""
    from .env import subprocess_env
    oai_key = _judge_openai_key()
    env = subprocess_env(
        TASK_QA_OPENAI_API_KEY=oai_key,
        TASK_QA_ANTHROPIC_API_KEY=os.environ.get("ANTHROPIC_API_KEY"),
        TASK_QA_GEMINI_API_KEY=os.environ.get("GEMINI_API_KEY"),
        TASK_QA_HARBOR_BIN=os.environ.get("TASK_QA_HARBOR_BIN") or getattr(cfg, "harbor_bin", None),
    )
    if extra:
        env.update(extra)
    return env


def _run_tqa_check(cfg, task_dir: Path, check_name: str, out_path: Path,
                   timeout: int, extra_env: dict | None = None) -> dict:
    """Run ONE QA CLI check invocation and return a record dict with
    ``status`` ∈ {passed, failed, error}. 'error' covers everything that is
    not a real verdict — timeout, crash, unparseable JSON — and callers treat
    it as fail-closed, never as a pass."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent / f".tqa-{out_path.stem}"
    work.mkdir(parents=True, exist_ok=True)
    cmd = [cfg.tqa_bin, check_name, str(Path(task_dir).resolve()),
           "--tqa-format", "json", "--tqa-out", str(out_path),
           "--tqa-artifacts-dir", str(work)]
    env = _qa_subprocess_env(cfg, extra_env)
    rec: dict = {"check": check_name, "status": "error", "summary": "",
                 "cost": None, "out_path": str(out_path)}
    try:
        proc = _run(cmd, cwd=work, timeout=timeout, env=env)
        _dump_proc(out_path.parent, cmd, proc.stdout, proc.stderr, proc.returncode)
    except subprocess.TimeoutExpired:
        rec["summary"] = f"timed out after {timeout}s"
        return rec
    except Exception as exc:                       # pragma: no cover
        rec["summary"] = f"invocation failed: {exc}"
        return rec

    raw = None
    if out_path.exists():
        try:
            raw = json.loads(out_path.read_text(errors="replace"))
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        rec["summary"] = (f"no parseable JSON (rc={proc.returncode}); "
                          f"stderr: {(proc.stderr or '')[-300:]}")
        return rec

    status = str(raw.get("status", "")).strip().lower()
    rec["status"] = status if status in ("passed", "failed", "error") else "error"
    findings = raw.get("findings") or []
    if findings and isinstance(findings, list):
        f0 = findings[0] if isinstance(findings[0], dict) else {}
        rec["summary"] = str(f0.get("summary") or raw.get("summary") or "")[:600]
    else:
        rec["summary"] = str(raw.get("summary") or "")[:600]
    metrics = raw.get("metrics") or {}
    rec["cost"] = metrics.get("llm_cost_usd")
    rec["cost_source"] = metrics.get("llm_cost_source")
    rec["tokens"] = {
        "input": metrics.get("llm_usage_input_tokens"),
        "cached": metrics.get("llm_usage_cached_input_tokens"),
        "output": metrics.get("llm_usage_output_tokens"),
        "requests": metrics.get("llm_usage_requests"),
    }
    return rec


def run_llm_checks(cfg, task_dir: Path, out_dir: Path,
                   checks=None, timeout: int | None = None) -> TqaSuiteResult:
    """Run the non-SOTA LLM checks (in parallel, up to 3 at a time) and gate
    binary-style: ANY check that failed OR errored blocks. Each check gets
    one automatic retry on a transient-looking error before we call it a
    failure."""
    import time
    checks = list(checks if checks is not None else cfg.tqa_llm_checks)
    timeout = timeout or cfg.tqa_timeout
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()

    def one(check_name: str) -> dict:
        rec = _run_tqa_check(cfg, task_dir, check_name,
                             out_dir / f"{check_name}.json", timeout)
        if rec["status"] == "error":
            rec = _run_tqa_check(cfg, task_dir, check_name,
                                 out_dir / f"{check_name}.retry.json", timeout)
        return rec

    with ThreadPoolExecutor(max_workers=3) as pool:
        recs = list(pool.map(one, checks))
    dur = time.monotonic() - t0

    per_check = {r["check"]: r for r in recs}
    ran = any(r["status"] in ("passed", "failed") for r in recs)
    bad = [r for r in recs if r["status"] != "passed"]
    issues = [f"{r['check']}: status={r['status']} {r['summary']}".strip() for r in bad]
    acceptable = ran and not bad
    total_cost = sum(r["cost"] for r in recs if r.get("cost")) or None
    summary = (f"Task QA LLM checks ({len(checks)}): "
               f"{'all passed' if acceptable else f'{len(bad)} not passing'}"
               + (f" — {', '.join(r['check'] for r in bad)}" if bad else ""))
    raw = {"checks": checks, "per_check": per_check, "acceptable": acceptable}
    try:
        (out_dir / "tqa-llm-summary.json").write_text(json.dumps(raw, indent=2, default=str))
    except Exception:
        pass
    return TqaSuiteResult(acceptable=acceptable, issues=issues, per_check=per_check,
                          ran=ran, out_dir=out_dir, summary=summary,
                          duration_sec=dur, cost_usd=total_cost, raw=raw)


def classify_sota_colors(per_traj: dict, checks) -> dict:
    """Per-check 3-state color across trajectories — same rule as harbor analyze.

    The subtle part (and the reason this has dedicated tests): a trajectory
    that ERRORED produced no valid verdict, so it must be EXCLUDED from the
    denominator — never counted as a pass. Otherwise errors would dilute a
    legitimate RED down to YELLOW and weaken the gate.
    """
    colors: dict = {}
    for c in checks:
        verdicts = [r["status"] for r in per_traj.get(c, []) if r["status"] in ("passed", "failed")]
        if not verdicts:
            colors[c] = "error"
            continue
        fails = sum(1 for s in verdicts if s == "failed")
        colors[c] = "green" if fails == 0 else ("red" if fails == len(verdicts) else "yellow")
    return colors


def run_sota_check_matrix(cfg, task_dir: Path, run_dirs, out_dir: Path,
                          checks=None, timeout: int | None = None) -> SotaCheckMatrixResult:
    """Run every SOTA-trajectory check across ALL our break-trial run dirs
    (check × trajectory jobs in parallel, max 3 workers), then aggregate each
    check into green/yellow/red. ``ran`` is True only if at least one
    (check, trajectory) pair produced a real verdict — all-error means the
    gate fail-closes upstream."""
    import time
    checks = list(checks if checks is not None else cfg.tqa_stage3_sota_checks)
    timeout = timeout or cfg.tqa_timeout
    out_dir.mkdir(parents=True, exist_ok=True)
    run_dirs = [Path(d) for d in run_dirs]
    per_traj: dict = {c: [] for c in checks}
    t0 = time.monotonic()

    jobs = [(ti, rd, c) for ti, rd in enumerate(run_dirs) for c in checks]

    def one(job):
        ti, rd, check_name = job
        rec = _run_tqa_check(cfg, task_dir, check_name,
                             out_dir / f"{check_name}__traj{ti}.json", timeout,
                             extra_env={TQA_SOTA_RUN_DIR_ENV: str(rd.resolve())})
        if rec["status"] == "error":
            rec = _run_tqa_check(cfg, task_dir, check_name,
                                 out_dir / f"{check_name}__traj{ti}.retry.json", timeout,
                                 extra_env={TQA_SOTA_RUN_DIR_ENV: str(rd.resolve())})
        rec["run_dir"] = str(rd)
        return (check_name, rec)

    with ThreadPoolExecutor(max_workers=3) as pool:
        for check_name, rec in pool.map(one, jobs):
            per_traj[check_name].append(rec)
    dur = time.monotonic() - t0

    colors = classify_sota_colors(per_traj, checks)
    red = sorted([c for c in checks if colors.get(c) == "red"])
    yellow = sorted([c for c in checks if colors.get(c) == "yellow"])
    error = sorted([c for c in checks if colors.get(c) == "error"])
    any_ran = any(r["status"] in ("passed", "failed")
                  for recs in per_traj.values() for r in recs)
    total_cost = sum(r["cost"] for recs in per_traj.values()
                     for r in recs if r.get("cost")) or None
    summary = (f"Task QA SOTA-check matrix over {len(run_dirs)} trajectory(ies): "
               + ", ".join(f"{c}={colors.get(c, '?')}" for c in checks)
               + (f" | RED (hard-block): {', '.join(red)}" if red else " | no RED")
               + (f" | ERROR (unverifiable, fail-closed): {', '.join(error)}" if error else ""))
    raw = {"colors": colors, "checks": checks, "n_trajectories": len(run_dirs),
           "red_checks": red, "yellow_checks": yellow, "error_checks": error, "per_traj": per_traj}
    try:
        (out_dir / "tqa-sota-matrix.json").write_text(json.dumps(raw, indent=2, default=str))
    except Exception:
        pass
    return SotaCheckMatrixResult(
        out_dir=out_dir, ran=any_ran, colors=colors, per_traj=per_traj,
        red_checks=red, yellow_checks=yellow, error_checks=error, n_trajectories=len(run_dirs),
        checks=checks, summary=summary,
        duration_sec=dur, cost_usd=total_cost, raw=raw)
