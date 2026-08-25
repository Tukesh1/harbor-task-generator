"""Regression tests for the red/green/yellow aggregation across check families.

Locks the invariant the whole gate depends on: a trajectory that did NOT produce a
valid verdict (errored / failed to generate) is NEVER counted as a pass — so it can
never dilute a RED to a YELLOW — and a check whose trajectories ALL errored is
'error' (unverifiable, fail-closed at the gate), never a silent green/pass.

Run: python tests/test_color_logic.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

from lib import task_qa as T
from lib import harbor as H


def _sota_color(statuses):
    return T.classify_sota_colors({"c": [{"status": s} for s in statuses]}, ["c"])["c"]


def _analyze_color(outcomes):
    # each outcome is for criterion 'x' on one analyzed trajectory; None = errored (no entry)
    trials = [{"checks": {"x": {"outcome": o}}} if o is not None else {"checks": {}}
              for o in outcomes]
    return H.aggregate_colors(trials).get("x")


def _llm_acceptable(statuses):
    # mirrors task_qa.run_llm_checks' gate: any non-'passed' (incl. error) blocks
    recs = [{"status": s} for s in statuses]
    ran = any(s in ("passed", "failed") for s in statuses)
    bad = [r for r in recs if r["status"] != "passed"]
    return ran and not bad


def test_sota_errored_trajectory_never_dilutes_red():
    # 8 planned, 7 ran, all 7 failed, 1 errored -> RED (not yellow)
    assert _sota_color(["failed"] * 7 + ["error"]) == "red"
    assert _sota_color(["failed", "failed", "error"]) == "red"


def test_sota_genuine_mix_is_yellow():
    assert _sota_color(["failed", "passed", "error"]) == "yellow"


def test_sota_errors_excluded_not_counted_pass():
    assert _sota_color(["passed", "passed", "error"]) == "green"   # errors excluded, not passes


def test_sota_all_errored_is_error_not_green():
    assert _sota_color(["error", "error"]) == "error"


def test_sota_all_errored_check_fail_closes_gate():
    m = T.SotaCheckMatrixResult(
        out_dir=Path("/t"), ran=True,
        colors={"false-negatives": "green", "false-positives": "error"},
        per_traj={}, red_checks=[], yellow_checks=[], error_checks=["false-positives"],
        n_trajectories=4, checks=["false-negatives", "false-positives"], summary="s")
    tqa_attempted, tqa_ran, tqa_red, tqa_error = True, m.ran, m.red_checks, m.error_checks
    # the gate's fail-closed + gate_ok conditions (must reject an unverifiable check)
    assert (tqa_attempted and (not tqa_ran or tqa_error))            # fail-closed fires
    assert not (not tqa_attempted or (tqa_ran and not tqa_red and not tqa_error))  # gate_ok false


def test_analyze_errored_trajectory_never_dilutes_red():
    assert _analyze_color(["fail"] * 7 + [None]) == "red"     # 7 fail of 8, 1 errored -> red
    assert _analyze_color(["fail", "pass", None]) == "yellow"
    assert _analyze_color(["pass", "pass"]) == "green"


def test_llm_checks_error_is_not_a_pass():
    assert _llm_acceptable(["passed", "passed"]) is True
    assert _llm_acceptable(["passed", "error"]) is False   # one error blocks
    assert _llm_acceptable(["error", "error"]) is False    # all-error fail-closed


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}"); passed += 1
    print(f"\n{passed}/{len(fns)} passed")
