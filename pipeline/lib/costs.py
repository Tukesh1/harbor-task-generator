"""Per-stage cost & timing accounting.

Important: this module is purely additive. It only *observes* things the
pipeline already has (AgentSession objects and the harbor result dataclasses)
and never changes behavior. If costs.py disappeared tomorrow, every gate
decision would still be exactly the same.

How it is meant to be used: a stage creates one :class:`StageCost` at the top
of run(), feeds it the harbor results each round (``add_harbor``) and the
agent sessions at the end (``add_session``), then calls ``finalize()`` once.
That writes a machine-readable cost.json into the snapshot dir, appends a
markdown block to the stage report, records the same dict in state.json (so
the live monitor can read it), and prints a one-line summary.

We keep two cost sources deliberately separate, because their trust levels
are very different:

  * SDK agents (builder/reviewer) — dollars come straight from the Claude
    Agent SDK's per-turn ResultMessage.total_cost_usd. Trustworthy, full stop.
  * harbor subprocesses (GPT-5.5 break trials via OpenAI; check/analyze via
    Anthropic) — dollars are best-effort, parsed from result.json when the
    field exists (the schema varies across harbor versions). Wall time and
    run counts are always recorded though, so even when dollars are missing
    you can price a run yourself from the raw JSON we dump under runs/.
"""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic, strftime


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _fmt_dur(sec: float | None) -> str:
    if sec is None:
        return "?"
    s = int(round(sec))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_usd(x: float | None) -> str:
    return "$?" if x is None else f"${x:.2f}"


def _tok_total(tokens: dict | None) -> int:
    if not tokens:
        return 0
    return sum(int(v) for v in tokens.values() if isinstance(v, (int, float)))


def _add_tokens(a: dict, b: dict | None) -> dict:
    if not b:
        return a
    for k, v in b.items():
        if isinstance(v, (int, float)):
            a[k] = a.get(k, 0) + int(v)
    return a


# ---------------------------------------------------------------------------
# StageCost
# ---------------------------------------------------------------------------
class StageCost:
    """Timing + cost accumulator for ONE stage run; knows how to render itself
    as a dict / markdown block / one-liner."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self._t0 = monotonic()
        self.started = strftime("%Y-%m-%d %H:%M:%S")
        self.wall_sec: float | None = None
        self.sessions: list[dict] = []
        self.harbor: list[dict] = []

    # -- collection --------------------------------------------------
    def stop(self) -> "StageCost":
        """Freeze the wall-clock timer (first call wins — safe to call twice)."""
        if self.wall_sec is None:
            self.wall_sec = monotonic() - self._t0
        return self

    def add_session(self, session) -> "StageCost":
        """Record an AgentSession's accumulated cost/turns/tokens."""
        self.sessions.append({
            "label": getattr(session, "label", "agent"),
            "cost_usd": round(float(getattr(session, "cost_usd", 0.0) or 0.0), 4),
            "num_turns": int(getattr(session, "num_turns", 0) or 0),
            "send_calls": int(getattr(session, "_turn", 0) or 0),
            "api_ms": int(getattr(session, "api_ms", 0) or 0),
            "tokens": dict(getattr(session, "tokens", {}) or {}),
        })
        return self

    def add_harbor(self, *results) -> "StageCost":
        """Record one or more harbor result objects (None entries are skipped).

        We dispatch on the class name instead of isinstance() here just to
        keep this module import-light — every result type carries the same
        duration_sec/cost_usd/tokens fields anyway.
        """
        for r in results:
            if r is None:
                continue
            cls = type(r).__name__
            if cls == "RunResult":
                label, model, n = getattr(r, "label", "run"), None, 1
            elif cls == "TrialsResult":
                label, model, n = "break", getattr(r, "model", None), getattr(r, "k", 1)
            elif cls == "CheckResult":
                label, model, n = "check", None, 1
            elif cls == "AnalyzeResult":
                label, model, n = "analyze", None, 1
            elif cls == "AutoQAResult":
                label, model, n = "autoqa", None, 1
            elif cls == "TqaSuiteResult":
                label, model, n = "qa-llm", None, 1
            elif cls == "SotaCheckMatrixResult":
                label, model, n = "qa-sota", None, getattr(r, "n_trajectories", 1)
            else:
                label, model, n = cls, getattr(r, "model", None), 1
            self.harbor.append({
                "label": label,
                "model": model,
                "n": n,
                "duration_sec": round(float(getattr(r, "duration_sec", 0.0) or 0.0), 1),
                "cost_usd": getattr(r, "cost_usd", None),
                "tokens": getattr(r, "tokens", None),
            })
        return self

    # -- aggregates --------------------------------------------------
    def _totals(self) -> dict:
        sdk_cost = round(sum(s["cost_usd"] or 0.0 for s in self.sessions), 4)
        sdk_tokens: dict = {}
        for s in self.sessions:
            _add_tokens(sdk_tokens, s["tokens"])
        # Harbor dollars are EXACT now: agent-trial / oracle / nop costs are read from the
        # trusted per-trial `agent_result.cost_usd`. The sonnet `analyze` step is the ONE
        # thing harbor records no cost OR tokens for, anywhere — so an analyze entry with no
        # cost is an EXPECTED, known gap (a few cents), NOT a measurement failure. We keep it
        # OUT of the partial flag (so the otherwise-exact total isn't stamped a vague '+'),
        # and surface its count separately so the display can footnote it honestly.
        metered = [h["cost_usd"] for h in self.harbor if h["cost_usd"] is not None]
        harbor_cost = round(sum(metered), 4) if metered else None
        unmetered = [h for h in self.harbor if h["cost_usd"] is None]
        analyze_unmetered = sum(1 for h in unmetered if h.get("label") == "analyze")
        # 'partial' ONLY if a NON-analyze harbor entry is missing its cost (a genuine gap).
        harbor_cost_partial = bool(metered) and (len(unmetered) - analyze_unmetered) > 0
        harbor_wall = round(sum(h["duration_sec"] or 0.0 for h in self.harbor), 1)
        harbor_tokens: dict = {}
        for h in self.harbor:
            _add_tokens(harbor_tokens, h["tokens"])
        # grand total only meaningful when we actually have harbor dollars
        grand = round(sdk_cost + (harbor_cost or 0.0), 4)
        return {
            "sdk_cost_usd": sdk_cost,
            "sdk_tokens": sdk_tokens,
            "harbor_cost_usd": harbor_cost,
            "harbor_cost_partial": harbor_cost_partial,
            "analyze_unmetered": analyze_unmetered,  # sonnet-analyze calls harbor never prices
            "harbor_wall_sec": harbor_wall,
            "harbor_tokens": harbor_tokens or None,
            "harbor_runs": len(self.harbor),
            "grand_total_usd": grand,
            "grand_total_complete": harbor_cost is not None and not harbor_cost_partial,
        }

    # -- renderers ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "started": self.started,
            "wall_sec": round(self.wall_sec, 1) if self.wall_sec is not None else None,
            "wall_human": _fmt_dur(self.wall_sec),
            "totals": self._totals(),
            "sessions": self.sessions,
            "harbor": self.harbor,
        }

    def summary_line(self) -> str:
        t = self._totals()
        sess = " ".join(
            f"{s['label'].split('-')[-1]}={_fmt_usd(s['cost_usd'])}({s['num_turns']}t)"
            for s in self.sessions
        ) or "none"
        az = t.get("analyze_unmetered", 0)
        if t["harbor_cost_usd"] is None:
            hc = "$? (not in JSON)"
        else:
            hc = (_fmt_usd(t["harbor_cost_usd"]) + ("+" if t["harbor_cost_partial"] else "")
                  + (f" (+{az} analyze unmetered)" if az else ""))
        grand = (_fmt_usd(t["grand_total_usd"]) + ("" if t["grand_total_complete"] else "+harbor"))
        return (f"[{self.stage}] cost/timing — wall {_fmt_dur(self.wall_sec)} | "
                f"SDK {_fmt_usd(t['sdk_cost_usd'])} ({sess}) | "
                f"harbor {hc} over {t['harbor_runs']} runs, {_fmt_dur(t['harbor_wall_sec'])} | "
                f"≈ total {grand}")

    def report_block(self) -> str:
        t = self._totals()
        lines = [
            "## Cost & timing",
            f"- Wall-clock: **{_fmt_dur(self.wall_sec)}**",
            f"- SDK agents: **{_fmt_usd(t['sdk_cost_usd'])}**"
            + (f" ({_tok_total(t['sdk_tokens'])} tokens)" if _tok_total(t['sdk_tokens']) else ""),
        ]
        for s in self.sessions:
            lines.append(f"  - {s['label']}: {_fmt_usd(s['cost_usd'])}, "
                         f"{s['num_turns']} turns, {s['send_calls']} sends")
        if t["harbor_cost_usd"] is None:
            hb = "$? (no cost field in harbor JSON — see raw JSON to price)"
        else:
            hb = _fmt_usd(t["harbor_cost_usd"]) + (" (partial — some runs lack a cost field)"
                                                   if t["harbor_cost_partial"] else "")
        lines.append(f"- Harbor subprocesses: **{hb}**, {t['harbor_runs']} runs, "
                     f"{_fmt_dur(t['harbor_wall_sec'])} wall")
        # group harbor runs by label for a compact breakdown
        by_label: dict[str, dict] = {}
        for h in self.harbor:
            g = by_label.setdefault(h["label"], {"runs": 0, "sec": 0.0, "cost": 0.0, "cost_known": False})
            g["runs"] += 1
            g["sec"] += h["duration_sec"] or 0.0
            if h["cost_usd"] is not None:
                g["cost"] += h["cost_usd"]
                g["cost_known"] = True
        for label, g in by_label.items():
            c = _fmt_usd(g["cost"]) if g["cost_known"] else "$?"
            lines.append(f"  - {label}: {g['runs']} runs, {_fmt_dur(g['sec'])}, {c}")
        grand = (_fmt_usd(t["grand_total_usd"]) + ("" if t["grand_total_complete"] else " + harbor (unknown)"))
        lines.append(f"- **≈ Stage total: {grand}**")
        if not t["grand_total_complete"]:
            lines.append("  > harbor dollars are best-effort; missing where the cost field "
                         "wasn't in the JSON. Wall time + run counts above are exact.")
        return "\n".join(lines)

    def write(self, snapshot_dir: Path) -> Path:
        snapshot_dir = Path(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        out = snapshot_dir / "cost.json"
        out.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return out

    def live_dict(self, *sessions) -> dict:
        """A cost snapshot for LIVE display mid-stage.

        Written into state.json once per round so the monitor reads a
        structured running total instead of scraping logs. It folds in the
        CURRENT cost of the live agent sessions (via add_session, which reads
        session.cost_usd — the SDK's latest cumulative total) WITHOUT
        permanently appending them — otherwise the later finalize() would
        double-count. Shape is the same as to_dict(), plus live=True.
        """
        saved = self.sessions
        self.sessions = []
        try:
            for s in sessions:
                if s is not None:
                    self.add_session(s)
            d = self.to_dict()
        finally:
            self.sessions = saved
        d["wall_sec"] = round(monotonic() - self._t0, 1)
        d["live"] = True
        return d


def finalize(cost: "StageCost", run_dir, stage: str, sessions=(), snapshot_dir=None) -> "StageCost":
    """Stop the timer, fold in the agent sessions, then emit everything at once:
    print the one-line summary, write cost.json + append the report block to the
    snapshot (when there is one), and record the cost dict in state.json.

    Call this EXACTLY ONCE per terminal path of a stage's run() — calling it
    twice on the same path will double-count the sessions. ``sessions`` are the
    AgentSession objects still in scope at that point.
    """
    cost.stop()
    for s in sessions:
        if s is not None:
            cost.add_session(s)
    print(cost.summary_line(), flush=True)
    if snapshot_dir is not None:
        try:
            cost.write(snapshot_dir)
            rep = Path(snapshot_dir) / "report.md"
            if rep.exists():
                rep.write_text(rep.read_text().rstrip() + "\n\n" + cost.report_block() + "\n")
        except Exception:
            pass
    try:
        from .orchestration import set_stage
        set_stage(run_dir, stage, cost=cost.to_dict())
    except Exception:
        pass
    return cost


def record_live(cost: "StageCost", run_dir, stage: str, *sessions) -> None:
    """Persist a running cost snapshot into state.json mid-stage (best-effort).

    Call once per round; the monitor then shows a structured, authoritative
    running total instead of guessing from logs. finalize() overwrites this
    with the final figure when the stage ends.
    """
    try:
        from .orchestration import set_stage
        set_stage(run_dir, stage, cost=cost.live_dict(*sessions))
    except Exception:
        pass


def harbor_stage_cost_usd(stage_dir) -> float | None:
    """Best-effort harbor dollars for a whole stage: walk its job dirs and sum,
    using the ONE authoritative reader (harbor._job_cost_tokens). The monitor
    shares this function so there is exactly one harbor-cost code path — we've
    been bitten before by a divergent second scraper and don't want a repeat.
    Returns None if nothing in the dir recorded a cost.
    """
    from .harbor import _job_cost_tokens
    stage_dir = Path(stage_dir)
    if not stage_dir.exists():
        return None
    total, found = 0.0, False
    for job in stage_dir.iterdir():
        if not job.is_dir():
            continue
        c, _ = _job_cost_tokens(job)
        if c is not None:
            total += c
            found = True
    return round(total, 4) if found else None
