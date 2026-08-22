"""Load and resolve pipeline configuration from config/pipeline.toml.

This is the one place that knows how a knob in the TOML maps to something
the code uses. Every entrypoint calls :func:`load_config` first thing, which
also has a useful side effect: it loads the project .env (see lib/env.py),
so keys are always sourced from there and never from the ambient shell.

Path handling, in short:
  * relative paths in the TOML resolve against the repo root;
  * ``$TB3_REPO`` / ``$AUTOQA_CWD`` env vars override the TOML value if set —
    handy when a clone wants to point at a different checkout without editing
    config;
  * rubric files prefer a repo-local copy (pipeline/rubrics/) and fall back
    to the TB3 submodule copy.
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent     # .../harbor-task-generator/pipeline
REPO_ROOT = PIPELINE_ROOT.parent                           # .../harbor-task-generator


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(str(p)))


def _resolve(base: Path, p: str) -> Path:
    pp = _expand(p)
    return pp if pp.is_absolute() else (base / pp)


def _resolve_rubric(tb3: Path, p: str) -> Path:
    """Rubric paths: absolute → as-is; else prefer a repo-local file (vendored
    rubrics, e.g. pipeline/rubrics/trial-analysis.toml with the non_clericalness
    criterion) and fall back to the TB3 submodule (the pinned official copies)."""
    pp = _expand(p)
    if pp.is_absolute():
        return pp
    local = REPO_ROOT / pp
    return local if local.exists() else (tb3 / pp)


@dataclass
class Config:
    """The fully-resolved pipeline config, as a flat bag of typed fields.

    Kept deliberately flat — stage scripts read ``cfg.<thing>`` directly and
    nobody should have to dig through nested groups to find a knob. ``raw``
    keeps the original TOML dict around for anything that needs a value we
    didn't map explicitly.
    """

    raw: dict
    config_path: Path

    # general
    tb3_repo: Path
    runs_root: Path

    # task metadata defaults (Stage 1 author)
    task_org: str
    task_author_name: str
    task_author_email: str

    # agent
    model: str
    permission_mode: str
    max_turns: int
    stall_timeout_sec: float       # inactivity timeout per turn before treating the stream as stalled
    stall_retries: int             # auto-retries (interrupt + re-issue) on a stall before aborting
    stall_settle_sec: float        # pause after an interrupt before re-issuing the turn
    api_max_retries: int           # transient API-error (429/5xx/529/overloaded) retries per turn
    api_backoff_base_sec: float    # exponential backoff base (full jitter)
    api_backoff_max_sec: float     # cap on a single backoff wait

    # harbor
    harbor_bin: str
    harbor_env: str
    break_agent: str
    break_model: str
    break_agent_kwargs: list[str]
    break_k: int
    break_probe_k: int
    break_extra_k: int
    break_concurrency: int
    reuse_break_trajectories: bool  # reuse break trials when the task hasn't changed (fingerprint)
    check_model: str
    analyze_model: str
    analyze_max_retries: int        # retry transient harbor-analyze failures before giving up
    check_max_retries: int          # retry transient harbor-check failures (no-output) before giving up
    impl_rubric: Path
    trial_rubric: Path
    trial_job_prompt: Path
    analyze_adjudicate: list[str]   # analyze findings that hard-fail the gate (+ reviewer adjudicates)
    analyze_hard_red: list[str]     # criteria that HARD-block when RED (unanimous fail)
    analyze_hard_anyfail: list[str] # criteria that HARD-block on ANY failing trajectory (red or yellow)
    analyze_matrix: list[dict]      # Phase-C extra models: [{tag, agent, model, kwargs, k}]
    analyze_matrix_failing: bool    # analyze only failing trials (False = all, so passing models count)

    # caps
    stage1_max_rounds: int
    stage2_break_max_rounds: int
    stage2_oracle_max_rounds: int
    stage2_final_max_rounds: int
    stage3_max_rounds: int
    stage3_verify_max_rounds: int

    # autoqa
    autoqa_enabled: bool
    autoqa_cwd: Path
    autoqa_mode: str
    autoqa_python: str

    # task QA engine
    tqa_enabled: bool
    tqa_bin: str                    # QA CLI (PATH name or abs path)
    tqa_llm_checks: list[str]       # non-SOTA checks (Stage 3 Phase A; binary gate)
    tqa_stage2_checks: list[str]    # SOTA-trajectory checks at Stage 2 Phase C (3-state)
    tqa_stage3_sota_checks: list[str]  # SOTA-trajectory checks at Stage 3 Phase B (3-state)
    tqa_timeout: int                # per-check subprocess timeout (sec)

    # gate
    gate_mode: str

    # ---- convenience accessors -------------------------------------
    @property
    def pipeline_root(self) -> Path:
        return PIPELINE_ROOT

    @property
    def repo_root(self) -> Path:
        return REPO_ROOT

    @property
    def skills_src(self) -> Path:
        return PIPELINE_ROOT / "skills"

    @property
    def agents_src(self) -> Path:
        return PIPELINE_ROOT / "agents"

    @property
    def prompts_src(self) -> Path:
        return PIPELINE_ROOT / "prompts"

    @property
    def runs_dir(self) -> Path:
        return self.runs_root if self.runs_root.is_absolute() else (REPO_ROOT / self.runs_root)

    def trial_count_for_concurrency(self, k: int | None = None) -> int:
        """Number of serial waves of trials given the concurrency setting."""
        kk = k if k is not None else self.break_k
        conc = max(1, self.break_concurrency)
        return max(1, math.ceil(kk / conc))


def load_config(path: str | Path | None = None) -> Config:
    """Read the TOML (default: pipeline/config/pipeline.toml), apply env-var
    overrides for external-repo paths, and return a resolved :class:`Config`.
    Also pulls in the project .env first — every entrypoint goes through here,
    so this guarantees keys come from .env and not the ambient shell."""
    # Keys come from the project-root .env (authoritative over ambient env).
    # Every entrypoint loads config, so this guarantees keys are sourced from .env.
    from .env import load_env
    load_env(REPO_ROOT)

    config_path = Path(path) if path else (PIPELINE_ROOT / "config" / "pipeline.toml")
    data = tomllib.loads(config_path.read_text())

    g = data.get("general", {})
    a = data.get("agent", {})
    h = data.get("harbor", {})
    c = data.get("caps", {})
    q = data.get("autoqa", {})
    t = data.get("task", {})
    tq = data.get("task_qa", {})
    gate = data.get("gate", {})

    # Paths resolve repo-relative unless absolute; an env var (if set) wins over the TOML so a
    # clone can point at a different checkout without editing config. Default = vendored submodule.
    tb3 = _resolve(REPO_ROOT, os.environ.get("TB3_REPO") or g.get("tb3_repo", "external/terminal-bench-3"))

    return Config(
        raw=data,
        config_path=config_path,
        tb3_repo=tb3,
        runs_root=_expand(g.get("runs_root", "runs")),
        task_org=str(t.get("org", "tasks")),
        task_author_name=str(t.get("author_name", "Pipeline Author")),
        task_author_email=str(t.get("author_email", "tasks@example.com")),
        model=a.get("model", "claude-opus-4-8"),
        permission_mode=a.get("permission_mode", "bypassPermissions"),
        max_turns=int(a.get("max_turns", 250)),
        stall_timeout_sec=float(a.get("stall_timeout_sec", 600)),
        stall_retries=int(a.get("stall_retries", 2)),
        stall_settle_sec=float(a.get("stall_settle_sec", 5)),
        api_max_retries=int(a.get("api_max_retries", 5)),
        api_backoff_base_sec=float(a.get("api_backoff_base_sec", 2.0)),
        api_backoff_max_sec=float(a.get("api_backoff_max_sec", 60.0)),
        harbor_bin=h.get("bin", "harbor"),
        harbor_env=h.get("env", "docker"),
        break_agent=h.get("break_agent", "codex"),
        break_model=h.get("break_model", "openai/gpt-5.5"),
        break_agent_kwargs=list(h.get("break_agent_kwargs", [])),
        break_k=int(h.get("break_k", 3)),
        break_probe_k=int(h.get("break_probe_k", 1)),
        break_extra_k=int(h.get("break_extra_k", 2)),
        break_concurrency=int(h.get("break_concurrency", 3)),
        reuse_break_trajectories=bool(h.get("reuse_break_trajectories", True)),
        check_model=h.get("check_model", "anthropic/claude-opus-4-7"),
        analyze_model=h.get("analyze_model", "sonnet"),
        analyze_max_retries=int(h.get("analyze_max_retries", 3)),
        check_max_retries=int(h.get("check_max_retries", 3)),
        impl_rubric=_resolve_rubric(tb3, h.get("impl_rubric", "rubrics/task-implementation.toml")),
        trial_rubric=_resolve_rubric(tb3, h.get("trial_rubric", "rubrics/trial-analysis.toml")),
        trial_job_prompt=_resolve_rubric(tb3, h.get("trial_job_prompt", "rubrics/trial-analysis-job.txt")),
        analyze_adjudicate=list(h.get("analyze_adjudicate", ["task_specification", "reward_hacking"])),
        analyze_hard_red=list(h.get("analyze_hard_red", ["task_specification"])),
        analyze_hard_anyfail=list(h.get("analyze_hard_anyfail", ["reward_hacking"])),
        analyze_matrix=list(h.get("analyze_matrix", [])),
        analyze_matrix_failing=bool(h.get("analyze_matrix_failing", False)),
        stage1_max_rounds=int(c.get("stage1_max_rounds", 6)),
        stage2_break_max_rounds=int(c.get("stage2_break_max_rounds", 6)),
        stage2_oracle_max_rounds=int(c.get("stage2_oracle_max_rounds", 4)),
        stage2_final_max_rounds=int(c.get("stage2_final_max_rounds", 2)),
        stage3_max_rounds=int(c.get("stage3_max_rounds", 5)),
        stage3_verify_max_rounds=int(c.get("stage3_verify_max_rounds", 2)),
        autoqa_enabled=bool(q.get("enabled", False)),
        autoqa_cwd=_resolve(REPO_ROOT, os.environ.get("AUTOQA_CWD") or q.get("autoqa_cwd", "external/codex-terminal-bench-auto-generation")),
        autoqa_mode=q.get("mode", "llm_only"),
        autoqa_python=q.get("python", "python"),
        tqa_enabled=bool(tq.get("enabled", False)),
        tqa_bin=tq.get("bin", "task-qa"),
        tqa_llm_checks=list(tq.get("llm_checks", [])),
        tqa_stage2_checks=list(tq.get("stage2_checks", [])),
        tqa_stage3_sota_checks=list(tq.get("stage3_sota_checks", [])),
        tqa_timeout=int(tq.get("timeout", 1800)),
        gate_mode=gate.get("mode", "stdin"),
    )
