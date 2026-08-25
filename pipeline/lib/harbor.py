"""Synchronous Harbor wrappers — THE authoritative source of pass/fail.

The core design rule of this pipeline lives here: Python (not the agent) runs
the real harbor trials and parses the rewards, so every gate decision is
deterministic and machine-checkable. The builder agent may run harbor itself
for debugging, but the conductor only ever trusts what this module reports.

Layouts we parse (Harbor's authoritative format):
    <jobs_dir>/<timestamp>/<task-id>/result.json  -> verifier_result.rewards.reward
    <run_dir>/verifier/{reward.txt,test-stdout.txt}
    <run_dir>/agent/trajectory.json

One trap to remember: harbor exits 0 even when a trial FAILS. Never branch on
the return code — always parse result.json.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_TIMEOUT_BUFFER_SEC = 180
_DEFAULT_TIMEOUT_SEC = 1800


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------
@dataclass
class TrialResult:
    """One scored trial inside a k-attempt break run."""
    run_dir: Path
    reward: float | None
    exception: str | None
    trajectory: Path | None

    @property
    def passed(self) -> bool:
        return self.reward is not None and self.reward >= 1.0

    @property
    def errored(self) -> bool:
        """The trial did NOT produce a valid scored model attempt — the agent crashed,
        timed out, hit an auth error, or no reward could be parsed. This is NO SIGNAL,
        NOT a fair failure: a crashed trial must never count as 'the model broke'."""
        return self.exception is not None or self.reward is None


@dataclass
class RunResult:
    """A single oracle/nop run."""
    label: str
    reward: float | None
    exception: str | None
    run_dir: Path | None
    job_dir: Path
    returncode: int
    stdout: str
    stderr: str
    test_stdout: str | None = None
    duration_sec: float | None = None        # wall-clock of the harbor invocation
    cost_usd: float | None = None             # best-effort, parsed from result.json if present
    tokens: dict | None = None                # best-effort token usage if present

    @property
    def passed(self) -> bool:
        return self.reward is not None and self.reward >= 1.0


@dataclass
class TrialsResult:
    """A k-attempt break check (possibly staged probe+confirm, combined)."""
    agent: str
    model: str
    k: int
    trials: list[TrialResult]
    job_dir: Path
    returncode: int
    stdout: str
    stderr: str
    duration_sec: float | None = None        # wall-clock of the whole k-trial harbor run
    cost_usd: float | None = None             # best-effort, summed across trials if present
    tokens: dict | None = None

    @property
    def rewards(self) -> list[float | None]:
        return [t.reward for t in self.trials]

    @property
    def passes(self) -> int:
        # count only VALID (non-errored) trials that passed
        return sum(1 for t in self.trials if not t.errored and t.passed)

    @property
    def n(self) -> int:
        return len(self.trials)

    @property
    def n_valid(self) -> int:
        """Trials that actually ran and were scored (exclude crashed/errored)."""
        return sum(1 for t in self.trials if not t.errored)

    @property
    def n_errored(self) -> int:
        return sum(1 for t in self.trials if t.errored)

    @property
    def all_errored(self) -> bool:
        """Every trial errored — an INFRASTRUCTURE failure (auth/OOM/timeout), NOT a
        break. The conductor must surface this, not treat it as the model failing."""
        return self.n > 0 and self.n_valid == 0

    @property
    def error_summary(self) -> str:
        excs = [str(t.exception) for t in self.trials if t.errored and t.exception]
        if not excs:
            return f"{self.n_errored}/{self.n} trial(s) errored (no parseable reward)"
        return f"{self.n_errored}/{self.n} trial(s) errored: " + "; ".join(e[:90] for e in excs[:3])

    @property
    def is_breaking(self) -> bool:
        """Model-breaking: at least one trial that ACTUALLY RAN was scored, and none of
        the VALID trials passed. Errored/crashed trials are no-signal, never breaks.
        (Normal no-error case: n_valid == n, so this equals 'n > 0 and passes == 0'.)"""
        return self.n_valid > 0 and self.passes == 0


@dataclass
class CheckResult:
    """Outcome of ``harbor check`` (implementation rubric) — parsed criteria,
    which ones failed, and whether parsing worked at all."""
    passed: bool
    failed_criteria: list[str]
    parse_ok: bool
    out_path: Path
    returncode: int
    raw: dict | None
    summary: str
    duration_sec: float | None = None
    cost_usd: float | None = None
    tokens: dict | None = None


@dataclass
class AutoQAResult:
    """Outcome of the legacy AutoQA v3 run (``_overall_result`` rollup plus
    the per-check issues worth feeding back to the builder)."""
    acceptable: bool            # overall result is within the configured gate
    overall: str                # _overall_result ("Pass"/"Minor Issues"/"Major Issues"/"Evaluation Error")
    issues: list[str]           # per-check entries that are not a clean pass
    parse_ok: bool
    out_path: Path
    returncode: int
    raw: dict | None
    summary: str
    duration_sec: float | None = None
    cost_usd: float | None = None
    tokens: dict | None = None


@dataclass
class AnalyzeResult:
    """Outcome of ``harbor analyze`` over one or more models' trajectories.

    The important field is ``colors``: the per-criterion 3-state aggregation
    across ALL analyzed trajectories (official TB3 check_icon rule):
      green  = no trajectory failed this criterion
      red    = EVERY trajectory failed it
      yellow = mixed (some failed, some passed) — models/trajectories disagreed
    """
    out_path: Path
    returncode: int
    raw: dict | None
    blocking_fails: list[str]      # reward_hacking / task_specification / low_timeout / refusals
    warnings: list[str]            # near_miss / difficulty_crux
    summary: str
    duration_sec: float | None = None
    cost_usd: float | None = None
    tokens: dict | None = None
    # 3-state aggregation across all analyzed trajectories (official check_icon rule):
    #   green  = no trajectory failed this criterion
    #   red    = EVERY trajectory failed it
    #   yellow = mixed (some failed, some passed) — models/trajectories disagreed
    colors: dict | None = None     # {criterion: "green"|"yellow"|"red"}
    n_trials: int | None = None    # how many trajectories were aggregated
    models: list | None = None     # models the trajectories came from (Phase C matrix)

    @property
    def ran(self) -> bool:
        """True only if harbor analyze actually produced a parseable result. False
        means it failed to run (tooling/infra/transient) — a hard-gate stop, not a clean verdict."""
        return self.raw is not None

    @property
    def reds(self) -> list[str]:
        return sorted(k for k, v in (self.colors or {}).items() if v == "red")

    @property
    def yellows(self) -> list[str]:
        return sorted(k for k, v in (self.colors or {}).items() if v == "yellow")

    def gate_blocks(self, hard_red, hard_anyfail) -> list[str]:
        """Which criteria HARD-block the gate under the 3-state colors:
          - any criterion in ``hard_red`` whose color is RED (unanimous fail);
          - any criterion in ``hard_anyfail`` whose color is RED *or* YELLOW (any failing
            trajectory) — used for reward_hacking, where a single cheat is disqualifying.
        Everything else (incl. yellows on the adjudicated criteria) goes to the reviewer."""
        c = self.colors or {}
        blocks = [k for k in (hard_red or []) if c.get(k) == "red"]
        blocks += [k for k in (hard_anyfail or []) if c.get(k) in ("red", "yellow")]
        return sorted(set(blocks))

    def hard_blocks(self, adjudicate) -> list[str]:
        """Legacy binary view (kept for callers that pass a flat adjudicate set)."""
        return [c for c in self.blocking_fails if c in adjudicate]


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
def _per_trial_timeout(task_dir: Path) -> int:
    """Per-trial timeout computed from task.toml (build + agent + verifier
    timeouts plus a buffer), falling back to _DEFAULT_TIMEOUT_SEC when the
    toml is missing/unreadable or computes something absurd."""
    toml_path = task_dir / "task.toml"
    if not toml_path.exists():
        return _DEFAULT_TIMEOUT_SEC
    try:
        data = tomllib.loads(toml_path.read_text())
    except Exception:
        return _DEFAULT_TIMEOUT_SEC
    build = data.get("environment", {}).get("build_timeout_sec", 0) or 0
    agent = data.get("agent", {}).get("timeout_sec", 0) or 0
    verifier = data.get("verifier", {}).get("timeout_sec", 0) or 0
    computed = int(build + agent + verifier + _TIMEOUT_BUFFER_SEC)
    return computed if computed > _TIMEOUT_BUFFER_SEC else _DEFAULT_TIMEOUT_SEC


# ---------------------------------------------------------------------------
# Reward parsing
# ---------------------------------------------------------------------------
def _parse_result_json(result_path: Path) -> tuple[float | None, str | None]:
    """Extract (reward, exception) from one per-trial result.json.

    Primary read: verifier_result.rewards.reward. If that's missing we fall
    back to reward.txt / reward.json sitting next to result.json — defensive,
    because harbor's layout has shifted between versions.
    """
    try:
        data = json.loads(result_path.read_text(errors="replace"))
    except Exception:
        return None, None
    exc = data.get("exception_info")
    exc_s = (exc if isinstance(exc, str) else str(exc)) if exc is not None else None
    vr = data.get("verifier_result") or {}
    rewards = vr.get("rewards") if isinstance(vr, dict) else None
    reward = None
    if isinstance(rewards, dict) and rewards:
        val = rewards.get("reward")
        if val is None:
            nums = [v for v in rewards.values() if isinstance(v, (int, float))]
            val = nums[0] if len(nums) == 1 else None
        if isinstance(val, (int, float)):
            reward = float(val)
    if reward is None:
        # fall back to reward.txt next to result.json
        rd = result_path.parent
        for rel in ("verifier/reward.txt", "verifier/reward.json"):
            p = rd / rel
            if p.exists():
                try:
                    raw = p.read_text(errors="replace").strip()
                    reward = float(raw)
                except ValueError:
                    try:
                        obj = json.loads(raw)
                        if isinstance(obj, (int, float)):
                            reward = float(obj)
                        elif isinstance(obj, dict) and isinstance(obj.get("reward"), (int, float)):
                            reward = float(obj["reward"])
                    except Exception:
                        pass
                break
    return reward, exc_s


def _is_job_aggregate(result_path: Path) -> bool:
    """Harbor writes TWO kinds of result.json under a job dir:
      * one job-level aggregate (`<ts>/result.json`) — overall `stats`/
        `n_total_trials`, NO per-trial `verifier_result`;
      * one per trial (`<ts>/task__*/result.json`) — has `verifier_result` and
        `trial_name`, i.e. the actual reward.
    We must parse only the per-trial files; the aggregate has no reward field and
    would (being written last) clobber `trials[-1]` with reward=None. Returns True
    for the aggregate so the collector can skip it. Schema-based so it survives
    harbor version/layout changes."""
    try:
        data = json.loads(result_path.read_text(errors="replace"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return ("verifier_result" not in data and "trial_name" not in data
            and ("n_total_trials" in data or "stats" in data))


def _collect_trials(job_dir: Path) -> list[TrialResult]:
    """Walk a job dir and collect every per-trial result (oldest first), so
    callers can treat trials[-1] as 'the latest trial'."""
    out: list[TrialResult] = []
    for result_path in sorted(job_dir.glob("**/result.json"), key=lambda p: p.stat().st_mtime):
        if _is_job_aggregate(result_path):
            continue
        reward, exc = _parse_result_json(result_path)
        run_dir = result_path.parent
        traj = run_dir / "agent" / "trajectory.json"
        out.append(TrialResult(run_dir, reward, exc, traj if traj.exists() else None))
    return out


def _scan_cost_tokens(data) -> tuple[float | None, dict | None]:
    """Best-effort: recursively pull a dollar cost + token usage out of a harbor
    JSON blob. APPROXIMATE — the schema varies by harbor/agent version, so this
    sums every cost-like and token-like field it finds (which can double-count if
    both per-turn and totals are present). The raw JSON is always dumped under
    runs/<slug>/harbor/, so calibrate these against it on the first real run."""
    costs: list[float] = []
    toks = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    seen_tok = False
    tok_keys = {
        "input": ("input_tokens", "prompt_tokens", "in_tokens", "n_input_tokens"),
        "output": ("output_tokens", "completion_tokens", "out_tokens", "n_output_tokens"),
        "cache_read": ("cache_read_input_tokens", "cache_read_tokens", "n_cache_tokens"),
        "cache_creation": ("cache_creation_input_tokens", "cache_creation_tokens"),
    }

    def walk(o):
        nonlocal seen_tok
        if isinstance(o, dict):
            for k, v in o.items():
                lk = str(k).lower()
                if lk in ("total_cost_usd", "cost_usd") and isinstance(v, (int, float)):
                    costs.append(float(v))
                else:
                    for dst, keys in tok_keys.items():
                        if lk in keys and isinstance(v, (int, float)):
                            toks[dst] += int(v)
                            seen_tok = True
                            break
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(data)
    cost = round(sum(costs), 6) if costs else None
    return cost, (toks if seen_tok else None)


def _merge_tokens(a: dict | None, b: dict | None) -> dict | None:
    """Element-wise sum of two token dicts (None-safe)."""
    if a is None:
        return dict(b) if b else None
    if b is None:
        return dict(a)
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}


def _job_cost_tokens(job_dir: Path) -> tuple[float | None, dict | None]:
    """EXACT cost + tokens for a harbor job, read from the TRUSTED field harbor
    records per trial: ``agent_result.cost_usd`` (and ``agent_result.n_input_tokens`` /
    ``n_output_tokens`` / ``n_cache_tokens``) in each ``<ts>/task__*/result.json``.

    Why this exact read (not the old recursive cost-scan): the job-level rollup
    (``<ts>/result.json``) carries a NULL ``stats.cost_usd``, and recursively summing
    every ``cost_usd`` key risks double-counting. There is exactly ONE per-trial file
    per trial and ``agent_result.cost_usd`` is the agent harness's own reported spend,
    so summing that field across the per-trial files is the authoritative, no-double-
    count total. A deterministic / no-LLM trial (oracle, nop) has no recorded agent
    cost → it contributes 0.0 (it genuinely spent nothing), so it does NOT mark the
    total 'partial'. Returns (None, None) only when the job wrote no per-trial result
    at all (e.g. it errored before any trial produced one)."""
    per_trial = [p for p in job_dir.glob("**/result.json")
                 if p.parent.name.startswith("task__")]
    if not per_trial:
        return None, None
    total_cost = 0.0
    toks = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    for result_path in per_trial:
        try:
            ar = (json.loads(result_path.read_text(errors="replace")) or {}).get("agent_result") or {}
        except Exception:
            ar = {}
        c = ar.get("cost_usd")
        if isinstance(c, (int, float)):
            total_cost += float(c)
        for dst, key in (("input", "n_input_tokens"), ("output", "n_output_tokens"),
                         ("cache_read", "n_cache_tokens")):
            v = ar.get(key)
            if isinstance(v, (int, float)):
                toks[dst] += int(v)
    return round(total_cost, 6), toks


def _read_test_stdout(run_dir: Path | None, max_chars: int = 8000) -> str | None:
    """Grab the verifier's test-stdout.txt for a trial (head+tail truncated to
    max_chars) — this is what we show the builder when the oracle breaks."""
    if run_dir is None:
        return None
    cands = list(run_dir.glob("**/verifier/test-stdout.txt"))
    if not cands:
        return None
    text = sorted(cands, key=lambda p: p.stat().st_mtime)[-1].read_text(errors="replace")
    if len(text) <= max_chars:
        return text
    return text[: max_chars // 2] + "\n...[truncated]...\n" + text[-max_chars // 2 :]


# ---------------------------------------------------------------------------
# Subprocess
# ---------------------------------------------------------------------------
def _run(cmd: list[str], cwd: Path, timeout: int,
         env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False,
        env=env,
    )


def _job_dir(jobs_root: Path, phase: str) -> Path:
    """Create (and return) a fresh timestamped job dir under jobs_root —
    one per harbor invocation, e.g. break-probe-20260603-140301-123/."""
    d = jobs_root / f"{phase}-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time()*1000)%1000:03d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dump_proc(job_dir: Path, cmd: list[str], out: str, err: str, rc: int) -> None:
    """Persist the harbor invocation + its output so empty job dirs aren't a mystery."""
    try:
        (job_dir / "harbor.cmd.txt").write_text(" ".join(cmd) + f"\n# returncode={rc}\n")
        (job_dir / "harbor.stdout.txt").write_text(out or "")
        (job_dir / "harbor.stderr.txt").write_text(err or "")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public: oracle / nop
# ---------------------------------------------------------------------------
def _run_single_agent(cfg, task_dir: Path, jobs_root: Path, agent: str, label: str) -> RunResult:
    """Run ONE trial of a single agent (oracle or nop) and wrap whatever came
    back into a RunResult. Never raises on harbor failure — timeouts and
    non-zero exits become part of the result for the gate to judge."""
    task_dir = task_dir.resolve()
    job_dir = _job_dir(jobs_root, label)
    timeout = _per_trial_timeout(task_dir) + _TIMEOUT_BUFFER_SEC
    cmd = [
        cfg.harbor_bin, "run", "-y",
        "-a", agent,
        "-p", str(task_dir),
        "--jobs-dir", str(job_dir),
        "-e", cfg.harbor_env,
        "-k", "1", "-n", "1",
    ]
    t0 = time.monotonic()
    try:
        proc = _run(cmd, cwd=task_dir.parent, timeout=timeout)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, out, err = -1, (exc.stdout or ""), f"TimeoutExpired after {timeout}s\n{exc.stderr or ''}"
    dur = time.monotonic() - t0
    _dump_proc(job_dir, cmd, out, err, rc)
    trials = _collect_trials(job_dir)
    t = trials[-1] if trials else None
    cost, tokens = _job_cost_tokens(job_dir)
    return RunResult(
        label=label,
        reward=(t.reward if t else None),
        exception=(t.exception if t else None),
        run_dir=(t.run_dir if t else None),
        job_dir=job_dir,
        returncode=rc,
        stdout=out,
        stderr=err,
        test_stdout=_read_test_stdout(t.run_dir if t else None),
        duration_sec=dur,
        cost_usd=cost,
        tokens=tokens,
    )


def run_oracle(cfg, task_dir: Path, jobs_root: Path) -> RunResult:
    """Run the reference solution once through harbor — must score reward 1.0."""
    return _run_single_agent(cfg, task_dir, jobs_root, "oracle", "oracle")


def run_nop(cfg, task_dir: Path, jobs_root: Path) -> RunResult:
    """Run the no-op agent once through harbor — must score BELOW 1.0 (the
    empty submission must not solve the task)."""
    return _run_single_agent(cfg, task_dir, jobs_root, "nop", "nop")


# ---------------------------------------------------------------------------
# Public: break trials (k attempts of the SOTA model)
# ---------------------------------------------------------------------------
def run_break_trials(cfg, task_dir: Path, jobs_root: Path, k: int | None = None,
                     label: str = "break", agent: str | None = None, model: str | None = None,
                     agent_kwargs: list | None = None) -> TrialsResult:
    """Run k trials of an agent/model against the task. Defaults to the SOTA break
    agent/model (cfg.break_*); pass ``agent``/``model``/``agent_kwargs`` to run a DIFFERENT
    model (the Phase-C multi-model analyze matrix, e.g. opus-4.8 or gemini)."""
    task_dir = task_dir.resolve()
    kk = k if k is not None else cfg.break_k
    use_agent = agent or cfg.break_agent
    use_model = model or cfg.break_model
    use_kwargs = cfg.break_agent_kwargs if agent_kwargs is None else agent_kwargs
    conc = max(1, min(cfg.break_concurrency, kk))
    job_dir = _job_dir(jobs_root, label)
    waves = math.ceil(kk / conc)
    timeout = _per_trial_timeout(task_dir) * waves + _TIMEOUT_BUFFER_SEC
    cmd = [
        cfg.harbor_bin, "run", "-y",
        "-a", use_agent,
        "-m", use_model,
        "-p", str(task_dir),
        "--jobs-dir", str(job_dir),
        "-e", cfg.harbor_env,
        "-k", str(kk), "-n", str(conc),
    ]
    for kw in (use_kwargs or []):
        cmd += ["--ak", kw]
    t0 = time.monotonic()
    try:
        proc = _run(cmd, cwd=task_dir.parent, timeout=timeout)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, out, err = -1, (exc.stdout or ""), f"TimeoutExpired after {timeout}s\n{exc.stderr or ''}"
    dur = time.monotonic() - t0
    _dump_proc(job_dir, cmd, out, err, rc)
    trials = _collect_trials(job_dir)
    cost, tokens = _job_cost_tokens(job_dir)
    return TrialsResult(
        agent=use_agent, model=use_model, k=kk,
        trials=trials, job_dir=job_dir, returncode=rc, stdout=out, stderr=err,
        duration_sec=dur, cost_usd=cost, tokens=tokens,
    )


@dataclass
class StagedBreak:
    """Result of a staged break check: a cheap probe first, escalated to more
    trials only when the probe actually broke."""
    probe: TrialsResult                 # the first break_probe_k trial(s)
    confirm: TrialsResult | None        # the break_extra_k MORE trials (only if probe broke)
    result: TrialsResult                # authoritative combined verdict (probe [+ confirm])

    @property
    def escalated(self) -> bool:
        """True when the probe broke and we paid for the confirm trials."""
        return self.confirm is not None

    @property
    def runs(self) -> list[TrialsResult]:
        """The actual harbor runs performed (for cost accounting)."""
        return [self.probe] + ([self.confirm] if self.confirm is not None else [])


def run_break_trials_staged(cfg, task_dir: Path, jobs_root: Path, label: str = "break") -> StagedBreak:
    """Cost-saving staged break check.

    Run ``break_probe_k`` trial(s) FIRST (usually 1). If the probe still PASSES,
    the task is too easy — return immediately so the builder keeps hardening, having
    spent only one trial. Only if the probe BREAKS (fails) do we run ``break_extra_k``
    MORE trials (in parallel) and judge breaking over the full set. Trajectory
    insights are common across trials, so one probe is enough to drive hardening
    while the task is still solvable.
    """
    probe = run_break_trials(cfg, task_dir, jobs_root, k=cfg.break_probe_k, label=f"{label}-probe")
    if not probe.is_breaking:
        return StagedBreak(probe, None, probe)
    confirm = run_break_trials(cfg, task_dir, jobs_root, k=cfg.break_extra_k, label=f"{label}-confirm")
    combined = TrialsResult(
        agent=probe.agent, model=probe.model,
        k=probe.k + confirm.k,
        trials=probe.trials + confirm.trials,
        job_dir=confirm.job_dir,        # failing trials for analyze --failing
        returncode=confirm.returncode, stdout="", stderr="",
        duration_sec=(probe.duration_sec or 0) + (confirm.duration_sec or 0),
    )
    return StagedBreak(probe, confirm, combined)


# ---------------------------------------------------------------------------
# Break-trial reuse (fingerprint-keyed) — skip re-running break trials on a task
# that has NOT changed since we last ran them.
# ---------------------------------------------------------------------------
_FINGERPRINT_SKIP = ("__pycache__", ".git", ".pytest_cache")


def task_fingerprint(task_dir: Path) -> str:
    """A stable content hash of EVERYTHING under ``task_dir`` (relative path +
    bytes of every file, in sorted order).

    Deliberately OVER-inclusive: it hashes the whole ``task/`` tree, not just
    the files a break trial happens to read. Reuse must be fail-safe — a false
    "unchanged" would feed STALE trajectories to the accept gate and could pass
    a task that actually changed, which is far worse than paying to re-run. So
    we err toward "changed": any edit anywhere under task/ (even solution/,
    which a break trial ignores) misses the cache and re-runs. Only obvious
    volatile cruft (__pycache__, .pyc, .DS_Store) is skipped.
    """
    task_dir = Path(task_dir)
    if not task_dir.exists():
        return "no-task"
    h = hashlib.sha256()
    paths = []
    for p in sorted(task_dir.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(task_dir).as_posix()
        if any(part in _FINGERPRINT_SKIP for part in p.relative_to(task_dir).parts):
            continue
        if rel.endswith((".pyc", ".pyo")) or p.name == ".DS_Store":
            continue
        paths.append((rel, p))
    for rel, p in paths:
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except Exception:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


@dataclass
class _BreakCacheEntry:
    result: object               # StagedBreak | TrialsResult
    job_dirs: list[Path]         # all job dirs the result references (must still exist to reuse)


class BreakCache:
    """Reuse break trials WITHIN a stage run when the task hasn't changed.

    Re-running the (expensive, xhigh) gpt-5.5 break trials on a byte-identical task
    is the pipeline's biggest redundancy: in the happy path the SAME break runs in
    Phase A (last breaking round), again in Phase B (re-confirm), and again in
    Phase C (accept gate) before analyze — with no task edit in between. This caches
    a ``(model, task-fingerprint) -> result`` so a hit returns the SAME job-dirs /
    run-dirs, and analyze + the Task QA SOTA checks judge the identical trajectories
    at zero extra trial cost.

    Correctness: keyed on :func:`task_fingerprint` (the whole ``task/`` tree), so ANY
    change misses and re-runs. A hit is only honored if every referenced job-dir still
    exists on disk. In-memory per stage run (a resume starts cold and re-runs once,
    then caches within the run) — never reuses across processes. Disable with
    ``BreakCache(enabled=False)`` (config ``[harbor].reuse_break_trajectories``)."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._store: dict[tuple, _BreakCacheEntry] = {}
        self.reuses = 0

    @staticmethod
    def _entry_reusable(entry: _BreakCacheEntry | None) -> bool:
        """A cached entry is only reusable if every job dir it references still
        exists — someone may have wiped runs/ between calls."""
        return entry is not None and all(Path(d).exists() for d in entry.job_dirs)

    def run_staged(self, cfg, task_dir: Path, jobs_root: Path, label: str = "break"):
        """Cached :func:`run_break_trials_staged`. Returns ``(StagedBreak, reused)``;
        the caller must skip cost accounting when ``reused`` is True."""
        if not self.enabled:
            return run_break_trials_staged(cfg, task_dir, jobs_root, label), False
        fp = task_fingerprint(task_dir)
        key = ("staged", cfg.break_agent, cfg.break_model,
               tuple(cfg.break_agent_kwargs or []), None, fp)
        entry = self._store.get(key)
        if self._entry_reusable(entry):
            self.reuses += 1
            print(f"[break-cache] task unchanged (fp={fp[:12]}) — reusing {cfg.break_model} "
                  f"staged break, skipping re-run ({[str(d) for d in entry.job_dirs]})", flush=True)
            return entry.result, True
        staged = run_break_trials_staged(cfg, task_dir, jobs_root, label)
        job_dirs = [staged.probe.job_dir] + ([staged.confirm.job_dir] if staged.confirm else [])
        self._store[key] = _BreakCacheEntry(staged, job_dirs)
        return staged, False

    def run_single(self, cfg, task_dir: Path, jobs_root: Path, k: int | None = None,
                   label: str = "break", agent: str | None = None, model: str | None = None,
                   agent_kwargs: list | None = None):
        """Cached :func:`run_break_trials` (e.g. an analyze-matrix model). Returns
        ``(TrialsResult, reused)``; the caller must skip cost accounting when reused.
        Keyed per model+k+kwargs, so different matrix legs don't collide."""
        if not self.enabled:
            return (run_break_trials(cfg, task_dir, jobs_root, k, label, agent, model, agent_kwargs),
                    False)
        fp = task_fingerprint(task_dir)
        key = ("single", agent or cfg.break_agent, model or cfg.break_model,
               tuple(agent_kwargs if agent_kwargs is not None else cfg.break_agent_kwargs or []),
               k if k is not None else cfg.break_k, fp)
        entry = self._store.get(key)
        if self._entry_reusable(entry):
            self.reuses += 1
            print(f"[break-cache] task unchanged (fp={fp[:12]}) — reusing {model or cfg.break_model} "
                  f"k={k} trials, skipping re-run ({[str(d) for d in entry.job_dirs]})", flush=True)
            return entry.result, True
        tr = run_break_trials(cfg, task_dir, jobs_root, k, label, agent, model, agent_kwargs)
        self._store[key] = _BreakCacheEntry(tr, [tr.job_dir])
        return tr, False


# ---------------------------------------------------------------------------
# Public: harbor check (implementation rubric)
# ---------------------------------------------------------------------------
_VERDICT_KEYS = {"passed", "pass", "result", "verdict", "score", "decision", "outcome"}


def _iter_criteria(obj, name=None):
    """Yield dicts that look like a single rubric criterion verdict, each carrying a
    'name'. Handles BOTH shapes harbor/our rubrics emit:
      - explicit list/object items: {"name": "...", "passed"|"verdict"|...: ...}
      - harbor `check`'s mapping:   {"checks": {"<name>": {"outcome": "pass", ...}}}
        (criterion keyed BY name, verdict under "outcome").
    """
    if isinstance(obj, dict):
        keys = set(obj.keys())
        # explicit named criterion
        if "name" in keys and (keys & _VERDICT_KEYS):
            yield obj
            return
        # value-style criterion reached via its parent key (e.g. checks.verifiable)
        if name is not None and "name" not in keys and (keys & _VERDICT_KEYS):
            c = dict(obj)
            c.setdefault("name", name)
            yield c
            return
        for k, v in obj.items():
            yield from _iter_criteria(v, name=k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_criteria(v, name=name)


# Outcome strings that DO NOT count as a failing criterion.
_OK_VERDICTS = {"pass", "passed", "yes", "true", "ok",
                "not_applicable", "n/a", "na", "none", "skip", "skipped",
                "warning", "warn", "info"}
_FAIL_VERDICTS = {"fail", "failed", "no", "false"}


def _criterion_failed(crit: dict) -> bool:
    for key in ("passed", "pass"):
        if key in crit and isinstance(crit[key], bool):
            return not crit[key]
    for key in ("result", "verdict", "decision", "outcome"):
        if key in crit and isinstance(crit[key], str):
            v = crit[key].strip().lower()
            if v in _FAIL_VERDICTS:
                return True
            if v in _OK_VERDICTS:
                return False
            return False  # unknown verdict string → don't hard-fail on it
    if "score" in crit and isinstance(crit["score"], (int, float)):
        return crit["score"] <= 0
    return False


def _declared_task_slug(task_dir: Path) -> str | None:
    """The task's intended TB3 folder name = the last path component of task.toml's
    ``[task].name`` (or top-level ``name``). harbor check's ``task_name`` criterion judges
    the task FOLDER NAME, so this is the name the task means to ship under. None if undeclared."""
    try:
        data = tomllib.loads((task_dir / "task.toml").read_text())
    except Exception:
        return None
    name = (data.get("task", {}) or {}).get("name") or data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip().rstrip("/").split("/")[-1] or None


# harbor's task.name rule, replicated from harbor.constants.ORG_NAME_PATTERN. harbor is installed
# as a standalone uv tool and is NOT importable from this venv, so we copy the pattern; keep it in
# sync if harbor changes it. A name is valid iff it matches AND contains no "..".
_ORG_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _diagnose_invalid_task_dir(task_dir: Path, harbor_output: str) -> str | None:
    """`harbor check` aborts BEFORE grading when ``Task.is_valid_dir`` is False, printing the
    GENERIC message "is not a valid task directory (missing instruction.md, task.toml, or tests/)".
    That same message fires for a genuinely missing instruction.md AND for a task.toml that fails
    harbor's own schema (e.g. ``[task].name`` not in ``org/name`` form) — harbor swallows the real
    reason. Return an APPEND-ONLY, fact-based breakdown (never a rewrite of harbor's message) so the
    agent gets the truth, or None when this isn't that failure / we found nothing concrete (so we
    never touch unrelated no-JSON failures like API timeouts, and never mislabel a real missing file
    as a name problem). Diagnoses ``task_dir`` (the real, persistent dir) — identical to the staged
    copy harbor checked."""
    if "not a valid task directory" not in (harbor_output or "").lower():
        return None
    lines = ["", "PIPELINE DIAGNOSIS (harbor's message above is generic — here is what is actually wrong):"]
    # Report which required artifacts are genuinely missing — verified facts, never a guess.
    missing = [rel for rel in ("instruction.md", "task.toml") if not (task_dir / rel).exists()]
    if not (task_dir / "tests").is_dir():
        missing.append("tests/")
    if not (task_dir / "environment").is_dir():
        missing.append("environment/")
    if missing:
        lines.append(f"  - MISSING required path(s): {', '.join(missing)} — add them (the literal cause).")
    # Attribute to the name ONLY when the name is provably malformed, so a real missing-file
    # failure is never mislabeled as a name problem.
    toml = task_dir / "task.toml"
    if toml.exists():
        try:
            data = tomllib.loads(toml.read_text())
            name = (data.get("task", {}) or {}).get("name") or data.get("name")
        except Exception:
            name = None
            lines.append("  - task.toml does NOT parse as TOML — fix the syntax.")
        if isinstance(name, str) and name.strip():
            n = name.strip()
            if not _ORG_NAME_RE.match(n) or ".." in n:
                lines.append(
                    f"  - task.toml [task].name = {n!r} is NOT in harbor's required 'org/name' form, "
                    "so harbor rejects the task before grading (this is the real cause even when every "
                    "file is present). Set it to e.g. 'your-org/<slug>': the segment AFTER the '/' is the "
                    "shipped folder name the `task_name` rubric judges, so keep that segment to <=3 "
                    "hyphenated kebab words.")
    if len(lines) <= 2:
        # Files present and name well-formed, yet harbor rejected the dir → task.toml fails harbor's
        # schema on some OTHER field. Say so honestly without guessing which (harbor hid the detail).
        if not missing and toml.exists():
            lines.append("  - All required paths exist and [task].name is well-formed, yet harbor still "
                         "rejected the directory: task.toml fails harbor's config schema on another field "
                         "(e.g. [environment]/[verifier]). Validate task.toml against the implementation "
                         "rubric's task_toml_schema.")
        else:
            return None
    return "\n".join(lines)


def run_check(cfg, task_dir: Path, out_path: Path) -> CheckResult:
    """Run ``harbor check`` (the implementation rubric) against the task.

    Two subtleties worth knowing:
      * We check a transient COPY of the task placed in a dir named after the
        DECLARED slug (task.toml [task].name), because the rubric's
        ``task_name`` criterion judges the folder name and our run dirs have
        long descriptive slugs.
      * Transient failures (no parseable JSON — API blip, timeout, laptop
        sleep) are retried with backoff+jitter; a deterministic "not a valid
        task directory" failure is NOT retried.
    """
    task_dir = task_dir.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Clear any stale output first: on a RESUME the round counter restarts at 1 and an old
    # check-1.json (with since-fixed failures) may be on disk; if harbor then fails to write,
    # we must not read that stale result. harbor overwrites it on success.
    out_path.unlink(missing_ok=True)
    # harbor check's `task_name` criterion judges the task FOLDER NAME. Our workdir keeps the
    # task in 'task/' under a long descriptive run-slug dir, so the rubric LLM picks up the
    # multi-word run slug and fails an otherwise-fine task. Run the check on a transient copy in a
    # dir named with the task's DECLARED slug (task.toml [task].name) so the criterion sees the
    # real intended ≤3-word name. Falls back to the live dir if no name is declared / copy fails.
    slug = _declared_task_slug(task_dir)
    staging = None
    check_target = task_dir
    if slug and slug != task_dir.name:
        try:
            staging = Path(tempfile.mkdtemp(prefix="hbcheck-"))
            check_target = staging / slug
            shutil.copytree(task_dir, check_target)
        except Exception:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
            staging, check_target = None, task_dir
    cmd = [
        cfg.harbor_bin, "check", str(check_target),
        "-r", str(cfg.impl_rubric),
        "-m", cfg.check_model,
        "-o", str(out_path),
    ]
    # Retry transient failures (no parseable JSON: API blip / timeout / a laptop sleep mid-check)
    # with exponential backoff + jitter — same pattern as run_analyze — so a blip doesn't
    # spuriously fail a QA round. The task is unchanged across attempts, so a retry is safe.
    attempts = max(1, cfg.check_max_retries + 1)
    timeout = 1800
    t0 = time.monotonic()
    rc, last_out, last_err, raw = -1, "", "", None
    for attempt in range(attempts):
        try:
            proc = _run(cmd, cwd=check_target.parent, timeout=timeout)
            rc, last_out, last_err = proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired:
            rc, last_out, last_err = -1, "", f"harbor check timed out after {timeout}s"
        raw = None
        if out_path.exists():
            try:
                raw = json.loads(out_path.read_text(errors="replace"))
            except Exception:
                raw = None
        if isinstance(raw, dict):
            break  # success
        if "not a valid task directory" in f"{last_out}\n{last_err}".lower():
            break  # deterministic precondition failure (not transient) — retrying won't help
        if attempt < attempts - 1:
            time.sleep(random.uniform(0, min(cfg.api_backoff_max_sec,
                                             cfg.api_backoff_base_sec * 2 ** attempt)))
    dur = time.monotonic() - t0
    diag = _diagnose_invalid_task_dir(task_dir, f"{last_out}\n{last_err}") if raw is None else None
    # Persist the invocation + output so a check failure is never silent (mirror run_analyze).
    try:
        out_path.with_suffix(".harbor.txt").write_text(
            " ".join(cmd) + f"\n# returncode={rc}  attempts={attempts}\n\n"
            f"== STDOUT ==\n{last_out}\n\n== STDERR ==\n{last_err}\n"
            + (f"\n=={diag}\n" if diag else ""))
    except Exception:
        pass
    if staging:
        shutil.rmtree(staging, ignore_errors=True)
    if raw is None:
        msg = (f"harbor check produced no parseable JSON after {attempts} attempt(s) — "
               f"inspect {out_path.with_suffix('.harbor.txt')}.")
        return CheckResult(False, [], False, out_path, rc, None,
                           msg + (diag or ""), duration_sec=dur)
    cost, tokens = _scan_cost_tokens(raw)
    crits = list(_iter_criteria(raw))
    failed = [c.get("name", "?") for c in crits if _criterion_failed(c)]
    parse_ok = len(crits) > 0
    passed = parse_ok and not failed
    summary = (f"{len(crits)} criteria parsed; {len(failed)} failing"
               + (f": {', '.join(failed)}" if failed else ""))
    return CheckResult(passed, failed, parse_ok, out_path, rc, raw, summary,
                       duration_sec=dur, cost_usd=cost, tokens=tokens)


# ---------------------------------------------------------------------------
# Public: AutoQA v3 (external module — `python -m task_evaluation.autoqa_v3`)
# ---------------------------------------------------------------------------
# Overall-result rollup strings (compute_overall_verdict in autoqa_v3/verdict.py).
AUTOQA_ACCEPTABLE = {"pass", "minor issues"}     # within the Stage 3 gate
AUTOQA_BLOCK = {"major issues", "evaluation error"}


def _autoqa_issues(raw: dict) -> list[str]:
    """Per-check rubric entries that are NOT a clean pass (for builder feedback)."""
    issues = []
    for k, v in raw.items():
        if k.startswith("_") or k == "task_name" or not isinstance(v, dict):
            continue
        result = str(v.get("result", "")).lower()
        sev = str(v.get("severity", v.get("worst_severity", ""))).lower()
        if result in ("fail", "error") or sev in ("high", "major", "medium", "minor"):
            msg = v.get("message") or v.get("justification") or v.get("summary") or ""
            issues.append(f"{k}: result={result or '?'} severity={sev or '-'} "
                          f"{str(msg)[:200]}".strip())
    return issues


def run_autoqa(cfg, task_dir: Path, out_path: Path) -> AutoQAResult:
    """Run AutoQA v3 (configured mode; Stage 3 uses 'llm_only' = no oracle, no SOTA).

    Invokes `python -m task_evaluation.autoqa_v3 <task> --out <json> --mode <mode>`
    from cfg.autoqa_cwd and parses the resulting eval JSON's `_overall_result`.
    """
    task_dir = task_dir.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [cfg.autoqa_python, "-m", "task_evaluation.autoqa_v3", str(task_dir),
           "--out", str(out_path), "--mode", cfg.autoqa_mode]
    timeout = 2400
    # AutoQA judges via OpenAI (pydantic_ai). Give it its OWN OpenAI key
    # (AUTOQA_OPENAI_API_KEY from .env) so its spend is separate from the break
    # trials' OPENAI_API_KEY; fall back to the global key if it isn't set.
    from .env import subprocess_env
    aq_env = subprocess_env(OPENAI_API_KEY=os.environ.get("AUTOQA_OPENAI_API_KEY"))
    t0 = time.monotonic()
    rc, err = 0, ""
    try:
        proc = _run(cmd, cwd=cfg.autoqa_cwd, timeout=timeout, env=aq_env)
        rc = proc.returncode
        err = (getattr(proc, "stderr", "") or "")[-600:]
    except subprocess.TimeoutExpired:
        rc = -1
        err = f"AutoQA timed out after {timeout}s"
    dur = time.monotonic() - t0

    raw = None
    if out_path.exists():
        try:
            raw = json.loads(out_path.read_text(errors="replace"))
        except Exception:
            raw = None
    if raw is None:
        return AutoQAResult(
            False, "(no output)", [], False, out_path, rc, None,
            f"AutoQA produced no parseable JSON at {out_path} (rc={rc}). "
            f"Check cfg.autoqa_python / autoqa_cwd / deps. stderr: {err}",
            duration_sec=dur)

    cost, tokens = _scan_cost_tokens(raw)
    overall = str(raw.get("_overall_result", "")).strip()
    parse_ok = overall != ""
    acceptable = parse_ok and overall.lower() in AUTOQA_ACCEPTABLE
    issues = _autoqa_issues(raw)
    summary = (f"AutoQA ({cfg.autoqa_mode}) overall='{overall or '?'}' "
               f"acceptable={acceptable}; {len(issues)} non-pass check(s)"
               + (f": {', '.join(i.split(':',1)[0] for i in issues)}" if issues else ""))
    return AutoQAResult(acceptable, overall, issues, parse_ok, out_path, rc, raw, summary,
                        duration_sec=dur, cost_usd=cost, tokens=tokens)


# ---------------------------------------------------------------------------
# Public: harbor analyze (trial-analysis rubric, failing trials)
# ---------------------------------------------------------------------------
_BLOCKING = {"reward_hacking", "task_specification", "low_timeout", "refusals",
             "non_clericalness"}  # vendored rubric criterion (pipeline/rubrics/)
_WARNING = {"near_miss", "difficulty_crux"}


# harbor analyze structural errors that retrying will NOT fix (don't burn retries on them).
_NON_RETRYABLE_ANALYZE = ("not a trial directory", "not a job directory",
                          "no trial.log", "no job.log")


def _analyze_target(job_dir: Path) -> Path:
    """`harbor analyze` requires the dir that actually holds ``job.log`` (a job dir) or
    ``trial.log`` (a trial dir). Our ``TrialsResult.job_dir`` is the OUTER wrapper
    (e.g. ``break-confirm-<ts>/``); its ``job.log`` lives one level down in the
    timestamped run dir. Descend to it, else analyze fails with "not a job directory"."""
    job_dir = Path(job_dir)
    if (job_dir / "job.log").exists() or (job_dir / "trial.log").exists():
        return job_dir
    cands = sorted(job_dir.glob("*/job.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0].parent if cands else job_dir


def _trials_from_raw(raw) -> list[dict]:
    """Per-trial entries from a harbor analyze JSON ({"trials": [{"checks": {...}}]})."""
    if not isinstance(raw, dict):
        return []
    t = raw.get("trials")
    return [x for x in t if isinstance(x, dict)] if isinstance(t, list) else []


def aggregate_colors(trials: list[dict]) -> dict[str, str]:
    """Per-criterion 3-state across trajectories, using the OFFICIAL TB3 check_icon rule
    (run-trials.yml): green = no trajectory failed it, red = EVERY trajectory failed it,
    yellow = mixed (some failed, some passed). pass / not_applicable both count as 'not a
    fail'. A criterion present in ANY analyzed trial gets a color."""
    from collections import defaultdict
    fails: dict = defaultdict(int)
    totals: dict = defaultdict(int)
    for t in trials:
        checks = (t or {}).get("checks") or {}
        for cname, cval in checks.items():
            outcome = (cval or {}).get("outcome") if isinstance(cval, dict) else cval
            if outcome is None:
                continue
            totals[cname] += 1
            if isinstance(outcome, str) and outcome.strip().lower() in _FAIL_VERDICTS:
                fails[cname] += 1
    colors: dict = {}
    for cname, total in totals.items():
        f = fails[cname]
        colors[cname] = "green" if f == 0 else ("red" if f == total else "yellow")
    return colors


def run_analyze(cfg, job_dir: Path, out_path: Path, failing: bool = True) -> AnalyzeResult:
    """Run `harbor analyze` on a job's trajectories, with the same agent the official TB3
    repo uses (model = cfg.analyze_model = sonnet; rubric + job-prompt). Retries transient
    failures with exponential backoff + jitter; persists stdout/stderr next to the output so
    a failure is never silent. ``AnalyzeResult.ran`` is False if it never produced a parseable
    result. ``failing=True`` analyzes only failing trajectories (Phase-A/legacy); set False to
    analyze ALL completed trials (Phase C — so passing trajectories of other models count
    toward the 3-state colors, the way the official multi-agent run produces yellows)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve to ABSOLUTE so the path is correct regardless of cwd (we set cwd below).
    target = _analyze_target(Path(job_dir)).resolve()
    cmd = [
        cfg.harbor_bin, "analyze", str(target),
        "-r", str(cfg.trial_rubric),
        "--job-prompt", str(cfg.trial_job_prompt),
        "-m", cfg.analyze_model,
        "-o", str(out_path),
    ]
    if failing:
        cmd.append("--failing")
    attempts = max(1, cfg.analyze_max_retries + 1)
    t0 = time.monotonic()
    rc, last_out, last_err, raw = -1, "", "", None
    for attempt in range(attempts):
        try:
            proc = _run(cmd, cwd=target.parent, timeout=1800)
            rc, last_out, last_err = proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired:
            rc, last_out, last_err = -1, "", "harbor analyze timed out after 1800s"
        raw = None
        if out_path.exists():
            try:
                raw = json.loads(out_path.read_text(errors="replace"))
            except Exception:
                raw = None
        if isinstance(raw, dict):
            break  # success
        if any(s in (last_out + last_err).lower() for s in _NON_RETRYABLE_ANALYZE):
            break  # structural error — retrying won't help
        if attempt < attempts - 1:
            time.sleep(random.uniform(0, min(cfg.api_backoff_max_sec,
                                             cfg.api_backoff_base_sec * 2 ** attempt)))
    dur = time.monotonic() - t0
    # Persist the invocation + output so an analyze failure is never silent again.
    try:
        out_path.with_suffix(".harbor.txt").write_text(
            " ".join(cmd) + f"\n# returncode={rc}  attempts={attempts}\n\n"
            f"== STDOUT ==\n{last_out}\n\n== STDERR ==\n{last_err}\n")
    except Exception:
        pass
    blocking, warnings = [], []
    summary = ""
    cost = tokens = None
    colors = None
    n_trials = None
    if isinstance(raw, dict):
        summary = str(raw.get("summary") or raw.get("job_summary") or "")[:4000]
        cost, tokens = _scan_cost_tokens(raw)
        for crit in _iter_criteria(raw):
            name = crit.get("name", "")
            if _criterion_failed(crit):
                if name in _BLOCKING:
                    blocking.append(name)
                elif name in _WARNING:
                    warnings.append(name)
        trials = _trials_from_raw(raw)
        colors = aggregate_colors(trials)
        n_trials = len(trials)
    else:
        tail = (last_err or last_out or "").strip()[-300:]
        summary = (f"harbor analyze FAILED to run (rc={rc}) after {attempts} attempt(s) — "
                   f"inspect {out_path.with_suffix('.harbor.txt')}. tail: {tail}")
    blocking = sorted(set(blocking))
    warnings = sorted(set(warnings))
    return AnalyzeResult(out_path, rc, raw, blocking, warnings, summary,
                         duration_sec=dur, cost_usd=cost, tokens=tokens,
                         colors=colors, n_trials=n_trials)


def run_analyze_matrix(cfg, model_jobs, out_path: Path, failing: bool = False) -> AnalyzeResult:
    """Phase-C multi-model analyze. ``model_jobs`` is a list of ``(model_tag, job_dir)``.
    Runs ``harbor analyze`` on EACH model's job dir (``failing=False`` so passing
    trajectories count too), MERGES the per-trial outcomes across all models, and computes
    ONE 3-state color map over the combined set — mirroring the official multi-agent analyze
    where yellows come from cross-model disagreement. Per-model JSONs are written beside
    out_path; the merged result + colors are written to out_path. ``ran`` is True iff at
    least one model produced a parseable analyze result (otherwise a hard-stop)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    per: list = []
    merged_trials: list = []
    models_present: list = []
    total_cost = 0.0
    any_ran = False
    for i, (tag, jd) in enumerate(model_jobs):
        safe = tag.replace("/", "_").replace(":", "_")
        # Index the per-model file so legs that share a tag (e.g. the break model's probe
        # + confirm job dirs, both 'openai/gpt-5.5') don't overwrite each other — every
        # trajectory's analyze JSON is preserved in qa/ (no dropped gpt-5.5 trajectory).
        sub = out_path.with_name(f"{out_path.stem}__{i}_{safe}.json")
        a = run_analyze(cfg, jd, sub, failing=failing)
        per.append((tag, a))
        if a.cost_usd:
            total_cost += a.cost_usd
        if a.ran:
            any_ran = True
            ts = _trials_from_raw(a.raw)
            for t in ts:
                tt = dict(t)
                tt["_model"] = tag
                merged_trials.append(tt)
            if ts:
                models_present.append(tag)
    colors = aggregate_colors(merged_trials)
    blocking = sorted([c for c in colors if colors[c] != "green" and c in _BLOCKING])
    warnings = sorted([c for c in colors if colors[c] != "green" and c in _WARNING])
    summary = " | ".join(
        f"[{tag}] {(a.summary or '').strip()[:600]}" if a.ran else f"[{tag}] (analyze did not run)"
        for tag, a in per)[:4000]
    merged_raw = None
    if any_ran:
        merged_raw = {"trials": merged_trials, "colors": colors, "models": models_present,
                      "per_model_summaries": {tag: a.summary for tag, a in per}}
    try:
        out_path.write_text(json.dumps(
            merged_raw if merged_raw is not None
            else {"error": "no model analyze produced a parseable result", "models": [t for t, _ in model_jobs]},
            indent=2, default=str))
    except Exception:
        pass
    return AnalyzeResult(out_path, 0 if any_ran else -1, merged_raw, blocking, warnings, summary,
                         cost_usd=(total_cost or None),
                         colors=(colors if any_ran else None),
                         n_trials=len(merged_trials), models=models_present)
