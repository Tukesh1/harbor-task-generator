"""Regression tests for break-trajectory reuse (harbor.task_fingerprint + BreakCache).

Locks the safety + savings contract:
  - the fingerprint is STABLE for byte-identical task/ trees and changes on ANY edit
    (modify / add / delete), so we never reuse stale trajectories — but ignores volatile
    cruft (__pycache__/.pyc/.DS_Store);
  - the cache reuses (no re-run) only when the fingerprint matches AND the job dir still
    exists; it re-runs after any task edit, keys per model, and never reuses when disabled.

Run: python tests/test_trajectory_cache.py
"""
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

from lib import harbor as H

CFG = types.SimpleNamespace(break_agent="codex", break_model="openai/gpt-5.5",
                            break_agent_kwargs=["reasoning_effort=xhigh"], break_k=3)


def _task(files: dict) -> Path:
    root = Path(tempfile.mkdtemp()) / "task"
    root.mkdir(parents=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


BASE = {"instruction.md": "solve it", "task.toml": "[task]\n", "tests/test_a.py": "assert True",
        "solution/solve.sh": "echo hi", "environment/Dockerfile": "FROM x"}


# ---- fingerprint -----------------------------------------------------------
def test_fingerprint_stable_across_identical_trees():
    a, b = _task(BASE), _task(BASE)
    assert H.task_fingerprint(a) == H.task_fingerprint(b)


def test_fingerprint_changes_on_edit_add_delete():
    a = _task(BASE)
    fp0 = H.task_fingerprint(a)
    (a / "instruction.md").write_text("solve it DIFFERENTLY")
    assert H.task_fingerprint(a) != fp0                      # modify
    a2 = _task(BASE); (a2 / "tests" / "test_b.py").write_text("x")
    assert H.task_fingerprint(a2) != fp0                     # add
    a3 = _task(BASE); (a3 / "solution" / "solve.sh").unlink()
    assert H.task_fingerprint(a3) != fp0                     # delete


def test_fingerprint_ignores_volatile_cruft():
    a = _task(BASE)
    fp0 = H.task_fingerprint(a)
    (a / "__pycache__").mkdir(); (a / "__pycache__" / "x.cpython-311.pyc").write_text("junk")
    (a / "tests" / "t.pyc").write_text("junk")
    (a / ".DS_Store").write_text("junk")
    assert H.task_fingerprint(a) == fp0


# ---- cache (monkeypatch the underlying harbor runners) ---------------------
_calls = {"staged": 0, "single": 0}


def _fake_staged(cfg, task_dir, jobs_root, label="break"):
    _calls["staged"] += 1
    jd = Path(jobs_root) / f"probe-{_calls['staged']}"; jd.mkdir(parents=True, exist_ok=True)
    probe = types.SimpleNamespace(job_dir=jd, rewards=[0.0])
    return types.SimpleNamespace(probe=probe, confirm=None, runs=[probe],
                                 result=types.SimpleNamespace(is_breaking=True, job_dir=jd))


def _fake_single(cfg, task_dir, jobs_root, k=None, label="break", agent=None, model=None,
                 agent_kwargs=None):
    _calls["single"] += 1
    jd = Path(jobs_root) / f"single-{(model or '').replace('/', '_')}-{_calls['single']}"
    jd.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(job_dir=jd, model=model)


H.run_break_trials_staged = _fake_staged          # cache calls the module globals → patched here
H.run_break_trials = _fake_single


def _jobs():
    d = Path(tempfile.mkdtemp()) / "jobs"; d.mkdir(parents=True); return d


def test_cache_reuses_when_task_unchanged():
    _calls["staged"] = 0
    task, jobs = _task(BASE), _jobs()
    c = H.BreakCache(enabled=True)
    s1, r1 = c.run_staged(CFG, task, jobs)
    s2, r2 = c.run_staged(CFG, task, jobs)
    assert r1 is False and r2 is True            # 2nd is a reuse
    assert _calls["staged"] == 1                 # runner ran only once
    assert s2 is s1 and c.reuses == 1


def test_cache_reruns_after_edit():
    _calls["staged"] = 0
    task, jobs = _task(BASE), _jobs()
    c = H.BreakCache(enabled=True)
    _, r1 = c.run_staged(CFG, task, jobs)
    (task / "instruction.md").write_text("hardened")
    _, r2 = c.run_staged(CFG, task, jobs)
    assert r1 is False and r2 is False and _calls["staged"] == 2


def test_cache_keys_per_model():
    _calls["single"] = 0
    task, jobs = _task(BASE), _jobs()
    c = H.BreakCache(enabled=True)
    _, ra = c.run_single(CFG, task, jobs, 3, "m", "codex", "anthropic/opus", [])
    _, rb = c.run_single(CFG, task, jobs, 3, "m", "codex", "gemini/pro", [])   # diff model → miss
    _, ra2 = c.run_single(CFG, task, jobs, 3, "m", "codex", "anthropic/opus", [])  # same → hit
    assert (ra, rb, ra2) == (False, False, True) and _calls["single"] == 2


def test_cache_reruns_if_jobdir_gone():
    _calls["staged"] = 0
    task, jobs = _task(BASE), _jobs()
    c = H.BreakCache(enabled=True)
    s1, _ = c.run_staged(CFG, task, jobs)
    s1.probe.job_dir.rmdir()                     # the cached trajectories vanished → must re-run
    _, r2 = c.run_staged(CFG, task, jobs)
    assert r2 is False and _calls["staged"] == 2


def test_disabled_never_reuses():
    _calls["staged"] = 0
    task, jobs = _task(BASE), _jobs()
    c = H.BreakCache(enabled=False)
    _, r1 = c.run_staged(CFG, task, jobs)
    _, r2 = c.run_staged(CFG, task, jobs)
    assert r1 is False and r2 is False and _calls["staged"] == 2 and c.reuses == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}"); passed += 1
    print(f"\n{passed}/{len(fns)} passed")
