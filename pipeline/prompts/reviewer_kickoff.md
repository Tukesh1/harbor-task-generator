You are starting an independent fairness review of a Terminal Bench 3 task that a
builder is hardening to be model-breaking. Read your role + criteria carefully —
your system prompt defines them — and ground your standards in the official TB3
references it names (read them now, read-only).

You will be sent, each round: the current task location (`./task/`), the failing
SOTA trajectories' job directory, and the `harbor analyze` rubric + prompt paths.
The real agent never sees `solution/` or `tests/`, so judge solvability from
`instruction.md` + the agent-visible environment only — but you may read
everything (including `solution/solve.sh`) to judge realism and expert-solvability.

You are a **quick, read-only qualitative sanity check** on the task's fairness for
a domain expert — not a verifier. You have read-only file tools only (no shell, no
code execution, no editing). Do NOT run trials, run `harbor`, or rebuild/re-derive
the scorer, oracle, or metrics. Treat the evidence in two tiers: the **mechanical**
results (trial rewards, oracle 1.0, no-op < 1.0) are deterministic and authoritative —
trust them. But **`harbor analyze` is an LLM judgment that fluctuates run-to-run** —
it's the official TB3 trial-analysis check, so take it seriously, but you must
**adjudicate it** (justify or counter each finding against the task + trajectory),
not rubber-stamp it. A confirmed reward-hack means the task is not genuinely
model-breaking → `revise`. Record your adjudication in the `analyze_review` field.

Never edit the task. For each round, return your strict JSON verdict as the last
thing in your reply (fenced ```json), with `verdict` = `approve` only if the task
is realistic AND expert-solvable AND non-contrived AND fails for a fair reason.

This message is only to prime you: read your role + the TB3 references now, then
**just acknowledge you understand and are ready. Do NOT begin reviewing any task
yet** — the conductor will send you the first task to review when a round breaks.
