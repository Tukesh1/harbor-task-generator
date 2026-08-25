"""A central, cross-run dataset of model-breaking FAILURES — our negative-example
corpus, and honestly one of the most valuable things this pipeline produces.

The idea is simple: when Stage 2 ends WITHOUT a fair, accepted break, that
rejected task is not waste — it's a lesson. So the conductor captures the
rejected task, the harbor-analyze and fairness-reviewer verdicts (with their
explanations), and a failing SOTA trajectory into one dataset record. Future
runs can feed these back in as anti-patterns: "here are breaks that LOOKED
good but were rejected, and exactly why".

Model tagging is mandatory here. A verdict or trajectory only means something
relative to the model that produced it, and frontier abilities move fast — an
old GPT-5.5-era verdict must never be mistaken for a current one. Every record
therefore stores which model we tried to break (``broken_model``), which model
authored the task (``builder_model``), and which model judged it
(``analyze_model``).

Records land under  <repo>/datasets/break_failures/<slug>__<stage>__<ts>/ .
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path


def dataset_root(cfg) -> Path:
    """Where the break-failure corpus lives: <runs_parent>/datasets/break_failures."""
    return cfg.runs_dir.parent / "datasets" / "break_failures"


def capture_break_failure(cfg, run_dir, stage: str, *, status: str, summary: str,
                          verdicts=None, break_report=None,
                          task_dir=None, snapshot_dir=None) -> Path | None:
    """Append one model-tagged negative example to the corpus.

    Best-effort by design: this function NEVER raises. A capture problem must
    not take down the pipeline's terminal path — losing one dataset record is
    recoverable, crashing a stage finish is not.
    """
    try:
        run_dir = Path(run_dir)
        slug = run_dir.name
        rec_dir = dataset_root(cfg) / f"{slug}__{stage}__{time.strftime('%Y%m%d-%H%M%S')}"
        rec_dir.mkdir(parents=True, exist_ok=True)

        verdicts = list(verdicts or [])
        break_report = dict(break_report or {})

        # Rounds where the model actually broke (0/k) but the task was still rejected —
        # the richest negative signal ("this break shape is unfair, here's why").
        breaking_rounds = []
        for phase in ("phaseA", "phaseB"):
            for r in (break_report.get(phase) or []):
                if r.get("breaking"):
                    breaking_rounds.append({"phase": phase, **r})
        if (break_report.get("final") or {}).get("breaking"):
            breaking_rounds.append({"phase": "final", **break_report["final"]})

        record = {
            "schema": "break_failure/v1",
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "slug": slug,
            "stage": stage,
            # --- MODEL TAGGING (a verdict is only valid for the model that produced it) ---
            "broken_model": getattr(cfg, "break_model", None),    # the SOTA model we tried to break
            "builder_model": getattr(cfg, "model", None),         # the agent that authored the task
            "analyze_model": getattr(cfg, "analyze_model", None), # the analyze judge (if configured)
            # --- outcome ---
            "terminal_status": status,
            "summary": summary,
            "n_breaking_rounds": len(breaking_rounds),
            "breaking_rounds": breaking_rounds,
            "verdicts": verdicts,            # full reviewer verdicts incl. analyze_review + reasons
            "break_report": break_report,
            "source_run_dir": str(run_dir),
            "source_snapshot": str(snapshot_dir) if snapshot_dir else None,
        }
        (rec_dir / "record.json").write_text(json.dumps(record, indent=2, default=str))

        # The rejected task itself (what was actually tried).
        if task_dir:
            for name in ("instruction.md", "task.toml"):
                src = Path(task_dir) / name
                if src.exists():
                    try:
                        shutil.copy2(src, rec_dir / name)
                    except Exception:
                        pass

        # The analyze verdicts — WHY it was judged unfair (task_specification, etc.).
        analyze_src = run_dir / "harbor" / stage
        if analyze_src.exists():
            adir = rec_dir / "analyze"
            for j in sorted(analyze_src.glob("analyze*.json")):
                adir.mkdir(exist_ok=True)
                try:
                    shutil.copy2(j, adir / j.name)
                except Exception:
                    pass

        # The failing SOTA trajectory (readable transcript) for each breaking round.
        for br in breaking_rounds:
            jd = br.get("job_dir")
            if not jd:
                continue
            for codex in sorted(Path(jd).glob("**/agent/codex.txt")):
                tdir = rec_dir / "trajectories"
                tdir.mkdir(exist_ok=True)
                dest = tdir / f"{br['phase']}-r{br.get('round', '?')}.codex.txt"
                try:
                    shutil.copy2(codex, dest)
                except Exception:
                    pass
                break   # one representative trajectory per breaking round is enough

        return rec_dir
    except Exception:
        return None
