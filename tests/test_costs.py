"""Regression tests for cost accounting.

These lock in the three cost bugs found on 2026-06-06 so they can't come back:

  1. SDK `ResultMessage.total_cost_usd` is CUMULATIVE — must be taken latest, not
     summed across turns (summing inflated cost ~Nx).
  2. harbor writes a per-job rollup AND a per-trial file with the SAME cost — the
     reader must count it ONCE, not double it.
  3. harbor token usage uses `n_input_tokens`/`n_output_tokens`/`n_cache_tokens` —
     the scraper must recognise those keys (it silently captured 0 before).

Run with `pytest tests/` or directly: `python tests/test_costs.py`.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "pipeline"))   # lib.*
sys.path.insert(0, str(_ROOT))                 # watch_run

from lib import costs, harbor  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_job(job_dir: Path, rollup_cost: float, trial_cost: float, n_in: int = 1000) -> None:
    """Write a realistic harbor job: <ts>/result.json (rollup, stats.cost_usd) PLUS
    <ts>/task__x/result.json (per-trial, agent_result.cost_usd) with the SAME cost."""
    ts = job_dir / "2026-01-01__00-00-00"
    ts.mkdir(parents=True)
    (ts / "result.json").write_text(json.dumps({
        "n_total_trials": 1,
        "stats": {"cost_usd": rollup_cost, "n_input_tokens": n_in,
                  "n_output_tokens": 10, "n_cache_tokens": 500},
    }))
    trial = ts / "task__abc"
    trial.mkdir()
    (trial / "result.json").write_text(json.dumps({
        "agent_result": {"cost_usd": trial_cost, "n_input_tokens": n_in,
                         "n_output_tokens": 10, "n_cache_tokens": 500},
    }))


def _result_msg(cost: float, input_tokens: int, num_turns: int = 1):
    """A stub of the SDK's ResultMessage (cumulative session totals)."""
    return types.SimpleNamespace(
        content=None, num_turns=num_turns, total_cost_usd=cost,
        duration_api_ms=0, usage={"input_tokens": input_tokens, "output_tokens": 5},
    )


# ---------------------------------------------------------------------------
# 1. SDK cumulative cost — take latest, never sum
# ---------------------------------------------------------------------------
def test_sdk_cost_is_latest_not_sum(tmp_path):
    from lib.sdk import AgentSession
    s = AgentSession(cwd=tmp_path, model="claude-opus-4-8", system_append="x",
                     log_path=tmp_path / "agent.log", label="t")
    # three CUMULATIVE snapshots, as the SDK actually emits them
    for msg in (_result_msg(2.0, 100), _result_msg(6.0, 300), _result_msg(9.0, 450)):
        s._drain_message(msg, [])
    assert s.cost_usd == 9.0, f"expected latest cumulative 9.0, got {s.cost_usd} (summed?)"
    assert s.tokens["input"] == 450, f"expected latest 450 tokens, got {s.tokens['input']} (summed?)"


# ---------------------------------------------------------------------------
# 2 + 3. harbor job reader — count cost once, capture n_* tokens
# ---------------------------------------------------------------------------
def test_job_cost_tokens_no_double_count(tmp_path):
    _make_job(tmp_path, rollup_cost=0.917502, trial_cost=0.917502)
    cost, tokens = harbor._job_cost_tokens(tmp_path)
    assert cost == 0.917502, f"rollup+per-trial double-counted: got {cost}"
    assert tokens and tokens.get("input") == 1000, f"n_input_tokens not captured: {tokens}"
    assert tokens.get("cache_read") == 500, f"n_cache_tokens not captured: {tokens}"


def test_harbor_stage_cost_sums_jobs_without_double(tmp_path):
    _make_job(tmp_path / "break-probe-1", rollup_cost=1.0, trial_cost=1.0)
    _make_job(tmp_path / "break-probe-2", rollup_cost=2.0, trial_cost=2.0)
    total = costs.harbor_stage_cost_usd(tmp_path)
    assert total == 3.0, f"expected 1.0 + 2.0 = 3.0 (each counted once), got {total}"


# ---------------------------------------------------------------------------
# 4. StageCost.live_dict — non-mutating, no double count with finalize
# ---------------------------------------------------------------------------
def test_stagecost_live_dict_is_non_mutating():
    sc = costs.StageCost("stageX")
    sc.add_harbor(types.SimpleNamespace(cost_usd=1.5, tokens=None, duration_sec=10.0))
    sess = types.SimpleNamespace(label="b", cost_usd=10.0, num_turns=5, _turn=5,
                                 api_ms=0, tokens={"input": 100})
    live1 = sc.live_dict(sess)
    live2 = sc.live_dict(sess)   # calling twice must NOT accumulate
    assert live1["totals"]["grand_total_usd"] == 11.5
    assert live2["totals"]["grand_total_usd"] == 11.5, "live_dict mutated state (double-counted)"
    sc.add_session(sess)         # the real finalize path, once
    assert sc.to_dict()["totals"]["sdk_cost_usd"] == 10.0, "finalize double-counted prior live_dict calls"


# ---------------------------------------------------------------------------
# 5. watch_run log scraper — latest cumulative, not sum
# ---------------------------------------------------------------------------
def test_watch_sdk_cost_from_log_latest(tmp_path):
    import watch_run
    log = tmp_path / "stage2.builder.log"
    log.write_text(
        "[00:00:00][t] turn done (cost_usd~2.0, session_total~2.0, turns~3)\n"
        "[00:01:00][t] turn done (cost_usd~6.0, session_total~8.0, turns~5)\n"
        "[00:02:00][t] turn done (cost_usd~9.0, session_total~17.0, turns~7)\n"
    )
    assert watch_run.sdk_cost_from_log(log) == 9.0, "watch_run summed cumulative cost again"


# ---------------------------------------------------------------------------
# plain-script runner (works without pytest)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in fns:
        try:
            if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"PASS {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
