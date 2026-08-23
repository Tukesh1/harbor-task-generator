"""Run-space orchestration — everything around the agent workspaces.

Concretely, this module owns:
  * run init + slugs            (runs/<slug>/ layout, state.json bootstrap)
  * the state ledger            (load/save/set_stage with atomic writes)
  * per-stage provisioning      (fresh vs resume-default workdirs)
  * snapshots                   (state copied OUTSIDE the agent workspace)
  * QA evidence bundling        (each snapshot becomes self-contained)
  * between-stage gates         (stdin prompt or auto mode)
  * small parsing utilities     (pulling JSON verdicts out of replies)

The on-disk contract this module establishes (and that other code relies on):
    runs/<slug>/
      00-proposal.md  state.json
      snapshots/after-<stage>/{task/, report.md, cost.json, qa/}
      logs/<stage>.{builder,reviewer}.log
      harbor/<stage>/<job-dirs + check/analyze JSON>
      <stage>/workdir/{task/, inputs/, .claude/skills/, CLAUDE.md}
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .config import Config


# ---------------------------------------------------------------------------
# Slugs & run init
# ---------------------------------------------------------------------------
def slugify(name: str) -> str:
    """Turn a proposal filename into a filesystem-safe run slug (lowercase,
    non-alphanumerics collapsed to '-'). Used consistently for run dirs AND
    lock files, so every entry point lands on the same names."""
    base = Path(name).stem.lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return base or "task"


def init_run(cfg: Config, proposal_path: Path) -> Path:
    """Create runs/<slug>/, copy the proposal in as 00-proposal.md, and
    bootstrap state.json. Idempotent — re-running keeps what's already there."""
    proposal_path = Path(proposal_path).resolve()
    slug = slugify(proposal_path.name)
    run_dir = cfg.runs_dir / slug
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "snapshots").mkdir(exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    (run_dir / "harbor").mkdir(exist_ok=True)
    dst = run_dir / "00-proposal.md"
    if not dst.exists():
        shutil.copy2(proposal_path, dst)
    st = load_state(run_dir)
    st.setdefault("slug", slug)
    st.setdefault("proposal", str(proposal_path))
    st.setdefault("created", time.strftime("%Y-%m-%d %H:%M:%S"))
    st.setdefault("stages", {})
    save_state(run_dir, st)
    return run_dir


def find_run_dir(cfg: Config, run_dir_or_slug: str) -> Path:
    """Accept either a full path to a run dir or just its slug; resolve and
    return the absolute run dir. Exits loudly if neither exists."""
    p = Path(run_dir_or_slug)
    if p.is_dir():
        return p.resolve()
    cand = cfg.runs_dir / run_dir_or_slug
    if cand.is_dir():
        return cand.resolve()
    raise SystemExit(f"Run dir not found: {run_dir_or_slug} (looked in {cfg.runs_dir})")


# ---------------------------------------------------------------------------
# State ledger
# ---------------------------------------------------------------------------
def _state_path(run_dir: Path) -> Path:
    return Path(run_dir) / "state.json"


def load_state(run_dir: Path) -> dict:
    """Read state.json; returns {} when missing or corrupt rather than raising —
    a half-written state file must never kill a run."""
    p = _state_path(run_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def save_state(run_dir: Path, state: dict) -> None:
    """Write state.json atomically (tmp file + rename), so a reader like the
    monitor never sees a torn write."""
    p = _state_path(run_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(p)


def set_stage(run_dir: Path, stage: str, **fields) -> dict:
    """Merge ``fields`` into state.json['stages'][<stage>] and save. This is
    how stage scripts publish live progress (rounds, rewards, cost, status)
    for the monitor and for humans tailing a run."""
    st = load_state(run_dir)
    st.setdefault("stages", {})
    entry = st["stages"].get(stage, {})
    entry.update(fields)
    entry["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st["stages"][stage] = entry
    save_state(run_dir, st)
    return st


# ---------------------------------------------------------------------------
# Provisioning (fresh per-stage workspace)
# ---------------------------------------------------------------------------
def _apply_subs_in_dir(root: Path, subs: dict[str, str]) -> None:
    """Apply ${PLACEHOLDER} substitution to every .md file under ``root``.
    Used on provisioned guidance files (skills) after copying them in."""
    if not subs:
        return
    for p in root.rglob("*.md"):
        text = p.read_text(errors="replace")
        new = text
        for k, v in subs.items():
            new = new.replace("${" + k + "}", v)
        if new != text:
            p.write_text(new)


def provision_stage(
    cfg: Config,
    run_dir: Path,
    stage_name: str,
    skill_names: list[str],
    seed_task: Path | None = None,
    inputs: dict[str, Path] | None = None,
    claude_md: str | None = None,
    substitutions: dict[str, str] | None = None,
    fresh: bool = False,
) -> Path:
    """Provision runs/<task>/<stage_name>/workdir.

    Default is RESUME: if a workdir with a task/ already exists, keep the
    agent-produced state (task/, inputs/, *-progress.md, scratch files) and
    only REFRESH the pipeline-authored guidance (.claude/skills, CLAUDE.md).
    That way code, prompt and skill changes take effect on a restart WITHOUT
    throwing away the agent's work. Pass fresh=True for the old
    wipe-and-reseed behavior.

    Layout produced (on a fresh build):
        workdir/
          .claude/skills/<skill>/SKILL.md   (copied from pipeline/skills)
          task/                             (seed_task copy, or empty)
          inputs/<name>                     (proposal, design notes, …)
          CLAUDE.md                         (key paths + rules)
    """
    workdir = Path(run_dir) / stage_name / "workdir"
    skills_dst = workdir / ".claude" / "skills"

    def _install_skills():
        if skills_dst.exists():
            shutil.rmtree(skills_dst)
        skills_dst.mkdir(parents=True)
        for skill in skill_names:
            src = cfg.skills_src / skill
            if not src.exists():
                raise FileNotFoundError(f"skill not found: {src}")
            shutil.copytree(src, skills_dst / skill)

    # RESUME: preserve work, refresh only the guidance the pipeline owns.
    if workdir.exists() and not fresh and (workdir / "task").exists():
        _install_skills()
        if claude_md:
            (workdir / "CLAUDE.md").write_text(claude_md)
        _apply_subs_in_dir(workdir / ".claude", substitutions or {})
        return workdir

    # FRESH: wipe + rebuild from canonical assets.
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    _install_skills()

    task_dst = workdir / "task"
    if seed_task is not None and Path(seed_task).exists():
        shutil.copytree(seed_task, task_dst)
    else:
        task_dst.mkdir()

    inputs_dst = workdir / "inputs"
    inputs_dst.mkdir()
    for name, src in (inputs or {}).items():
        src = Path(src)
        if src.exists():
            shutil.copy2(src, inputs_dst / name)

    if claude_md:
        (workdir / "CLAUDE.md").write_text(claude_md)

    _apply_subs_in_dir(workdir / ".claude", substitutions or {})

    # Fresh build → clear any STALE per-run state for this stage so the monitor and the
    # cost summary reflect the NEW run, not the previous one (round/cost/breaking/verdict
    # would otherwise linger until the new run overwrites each field). Only touches this
    # stage's entry; other stages + top-level state (slug, proposal, …) are preserved.
    state = load_state(run_dir)
    if state.get("stages", {}).get(stage_name):
        state["stages"][stage_name] = {"status": "running",
                                       "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
        save_state(run_dir, state)

    return workdir


# ---------------------------------------------------------------------------
# Snapshots (state backed up OUTSIDE the agent workspace)
# ---------------------------------------------------------------------------
def snapshot(
    run_dir: Path,
    label: str,
    task_dir: Path,
    report: str | None = None,
    extra: dict[str, Path] | None = None,
) -> Path:
    """Copy the task state OUTSIDE the agent workspace, into
    runs/<task>/snapshots/after-<label>/ — this is what makes every stage's
    end-state independently inspectable (and re-seedable by the next stage)."""
    dst = Path(run_dir) / "snapshots" / f"after-{label}"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    if Path(task_dir).exists():
        shutil.copytree(task_dir, dst / "task")
    if report is not None:
        (dst / "report.md").write_text(report)
    for name, src in (extra or {}).items():
        src = Path(src)
        if src.is_dir():
            shutil.copytree(src, dst / name)
        elif src.exists():
            shutil.copy2(src, dst / name)
    return dst


def latest_task_snapshot(run_dir: Path, label: str) -> Path:
    """Path to the task/ inside snapshots/after-<label>/ — i.e. what the NEXT
    stage should seed its own workdir from."""
    return Path(run_dir) / "snapshots" / f"after-{label}" / "task"


def collect_qa_evidence(run_dir: Path, stage: str, snap_dir: Path,
                        summary: dict | None = None,
                        also: dict[str, Path] | None = None) -> Path:
    """Bundle a stage's QA/review evidence into ``<snapshot>/qa/`` so each
    after-stageN snapshot is fully self-contained.

    Copies the stage's raw harbor JSON (check / analyze / autoqa / …) from
    runs/<slug>/harbor/<stage>/ into qa/harbor/, copies any ``also`` files
    (e.g. a cross-stage final verdict pulled into the deliverable), and writes
    a qa-summary.json manifest. Best-effort and never raises — evidence
    collection must never be the thing that aborts a run.
    """
    snap_dir = Path(snap_dir)
    qa = snap_dir / "qa"
    try:
        qa.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        hsrc = Path(run_dir) / "harbor" / stage
        if hsrc.is_dir():
            hdst = qa / "harbor"
            hdst.mkdir(exist_ok=True)
            for p in sorted(hsrc.glob("*.json")):
                try:
                    shutil.copy2(p, hdst / p.name)
                    copied.append(f"harbor/{p.name}")
                except Exception:
                    pass
            # Detailed Task QA evidence lives in subdirs (tqa-llm-*/, tqa-sota-final-*/):
            # the per-check envelopes (findings + reasoning) a reviewer wants. Copy ONLY
            # their *.json — not the internal `.tqa-*` cwd artifacts, and NOT the large break
            # job dirs that also live under harbor/<stage>/. qa-summary.json is untouched.
            for d in sorted(hsrc.glob("tqa-*")):
                if not d.is_dir():
                    continue
                ddst = hdst / d.name
                ddst.mkdir(exist_ok=True)
                for j in sorted(d.glob("*.json")):
                    try:
                        shutil.copy2(j, ddst / j.name)
                        copied.append(f"harbor/{d.name}/{j.name}")
                    except Exception:
                        pass
        for name, src in (also or {}).items():
            src = Path(src)
            if src.exists():
                try:
                    shutil.copy2(src, qa / name)
                    copied.append(name)
                except Exception:
                    pass
        manifest = {"stage": stage, "files": copied}
        if summary:
            manifest["summary"] = summary
        (qa / "qa-summary.json").write_text(json.dumps(manifest, indent=2, default=str))
    except Exception:
        pass
    return qa


# ---------------------------------------------------------------------------
# Between-stage gate
# ---------------------------------------------------------------------------
def gate(cfg: Config, run_dir: Path, stage: str, summary: str) -> bool:
    """The between-stage human checkpoint. Returns True to proceed, False to abort.

    mode="stdin": print the stage summary and prompt y/n on the terminal (a
    free-text note after the letter is recorded in state.json). mode="auto":
    always proceed — that's how unattended batch runs go through with no code
    changes anywhere else.
    """
    banner = (
        "\n" + "=" * 72
        + f"\n  GATE — {stage} complete  ({run_dir})\n"
        + "=" * 72 + "\n" + summary.strip() + "\n" + "-" * 72 + "\n"
    )
    print(banner, flush=True)
    set_stage(run_dir, stage, gate_summary=summary)

    if cfg.gate_mode == "auto":
        print("[gate] mode=auto → proceeding automatically.", flush=True)
        return True

    while True:
        try:
            ans = input(f"[gate:{stage}] proceed? [y]es / [n]o(abort)  (notes optional after y/n): ").strip()
        except EOFError:
            print("[gate] no stdin available; aborting. Set [gate].mode=auto to skip.", flush=True)
            return False
        if not ans:
            continue
        head = ans[0].lower()
        note = ans[1:].strip(" :,-")
        if head == "y":
            if note:
                set_stage(run_dir, stage, gate_note=note)
            return True
        if head == "n":
            if note:
                set_stage(run_dir, stage, gate_note=note)
            return False
        print("  please answer y or n.", flush=True)


# ---------------------------------------------------------------------------
# Parsing helpers (reviewer verdict JSON)
# ---------------------------------------------------------------------------
def extract_json_block(text: str) -> dict | None:
    """Pull the last JSON object out of an assistant reply.

    Reviewers are told to end their reply with a strict JSON verdict, but
    models being models, the reply usually has prose around it and often a
    ```json fence. So: collect every fenced block plus every balanced {...}
    run, then walk backwards and return the first one that parses as a dict.
    Returns None when nothing parses — callers decide what fail-closed means
    for them.
    """
    if not text:
        return None
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = list(fences)
    # also try the last balanced {...} run
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(text[start : i + 1])
    for cand in reversed(candidates):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None
