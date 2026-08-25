# Terminal-Bench Hardening Guidelines

Distilled from "What Makes a Good Terminal Bench Task" (ibercovich, TB issue #224).
Read this before proposing or applying any hardening edit. Every change should
push toward these principles, not away from them.

> **NOTE — Stage-2 working copy.** This is the pipeline's own copy of the upstream
> creator-UI catalog. The frontier-model **lens** below and three extra shape-levers (**L, M, N**)
> are added for Stage 2; the upstream levers A–K and their fairness bars are unchanged.

## Why most levers don't break a frontier model — and what actually does (read this LENS first)
This is **understanding to design with, not a rule to satisfy** (there is no check for it — it is
just *how breaking a strong model works*). A SOTA agent (e.g. GPT-5.5) is at least as strong as a
generalist senior engineer, and it does two things that defeat most hardening:
1. **It implements anything you can STATE precisely.** If the difficulty is a rule, convention,
   invariant, or algorithm you spell out — even subtly — the model just builds it correctly. A
   break that rests on a *stateable, retrievable* idea will be solved.
2. **It self-tests aggressively.** It writes its own property tests, backtests, sanity/uniqueness
   checks, and re-derivations, and iterates until they pass. **If the model can construct an oracle
   that would flag its OWN mistake, it self-corrects before submitting** — and the task does not
   break. (This is exactly why a clean, self-verifiable algorithm task — locks, FP-summation,
   sorting — resists every fair rule you state: the model property-tests the invariant and catches
   its own error.)

So a break only lands when **the model cannot cheaply tell it is wrong.** The breaks that have
actually worked here share these traits — read every lever A–K (and L–N) through this lens:
- **The model's own checks come up GREEN on the wrong answer** — its metric/backtest, its audit,
  its uniqueness/structural checks all pass for the incorrect output, so it has no signal and
  confidently submits. (If its natural check *would* catch the error, expect it to self-fix.)
- **The catch is discoverable in the AGENT-VISIBLE DATA / environment**, found by a diligent expert
  inspecting the samples — NOT a rule that lives only in hidden `tests/` (undiscoverable = unfair =
  rejected). The data is fair game as the hiding place.
- **The divergence is calibrated BELOW any loud threshold** the model would notice (a metric that
  stays green; no error/explosion), so nothing prompts a second look.
- **No graduated verifier feedback** to iterate against on the critical path (prefer binary, cf. C).

The strongest expression of all this is an **OMISSION**: the model produces a complete-LOOKING
answer and confidently stops, never realizing there was a further step a responsible expert would
take (lever **L**). *"Make the default move wrong"* is one way to break; *"make the model stop one
expert step short, with its own checks green"* is usually the stronger one. Reach for **L/M/N** to
embody this lens, and apply the lens when judging whether any A–K edit will really land.

## The three properties: Adversarial, Difficult, Legible

A benchmark is **adversarial**, not a prompt. A prompt is engineered to help
the agent succeed. A benchmark states an unambiguous objective, verifies
confidently, and is hard. Stop adding hints, examples, and emphasis "to help
the agent." That's anti-benchmark.

**Difficult** means *conceptually* difficult — the agent has to think before
acting, has to investigate, has to reason about the problem. Difficulty does
NOT come from:
- bigger datasets / more rows / longer files
- resource constraints (CPU, memory, time)
- convoluted output formats
- verbose, multi-page instructions
- hidden permissions tricks

> A task that takes 30 minutes because the agent is wrestling with the problem
> is more interesting than one that takes 30 minutes because `make -j4` is
> running.

**Legible** means short, self-contained, no README needed. The ideal task is
two paragraphs. Brevity is a KPI: every extra token is a chance to add
ambiguity or a spec hole the tests don't catch.

## When making the task harder

**Add conceptual difficulty, not clerical difficulty.** A new bug, a subtler
edge case in the data, a non-obvious invariant the agent has to discover —
yes. Stricter JSON formatting, more required deliverables, more required
libraries — no.

**Difficulty rule of thumb:** harder = more *thinking* before action, not more
*typing* during action. If the harder version still has a one-line fix once
you see it, but it takes longer to *find* — that's good hardening. If the
harder version has the same bug but now there are 10 of them — that's bigger,
not harder.

**Anchor on the TB3 bar:** "any task that could be solved by an average
undergraduate student in under a few days is too easy." If hardening doesn't
push past this bar, it's wasted effort.

## Anti-patterns to avoid

### AI-generated instructions
Verbose, over-structured, written-to-help-the-agent tone. If the prose feels
like an LLM trying to maximize success probability, rewrite it. Direct,
specific, sufficient — never redundant or attention-grabbing.

### Over-prescriptive instructions
Don't tell the agent *how* to solve. State the end state. Don't enumerate
failure modes. Don't tell the agent how it will be tested. Trust that an
experienced engineer reading the spec will know what to do — and assume the
agent has that level.

The instruction must be specified well enough that meeting it implies passing
the tests. But not a step further.

### Same-family bugs

A new bug that's *structurally identical* to an existing bug doesn't make
the task harder — once the agent spots the pattern, it fixes both with one
edit. Examples of the trap:

- Existing bug: `floor(revenue_amount)` truncates cents in `stg_touchpoints`.
- Bad new "harden": `cast(ad_spend as integer)` in `stg_ad_spend`. Same
  bug family (numeric truncation in a staging model). The agent reads two
  staging models, spots the parallel pattern, removes both casts in
  seconds. Net effect: no added difficulty.

When proposing lever **A** (compounding bug), the new bug should be a
*different conceptual axis* from existing bugs. If existing bugs are all
type-coercion in staging, propose a window-function frame bug, an
incorrect CTE alias chain, or a JOIN cardinality issue — not another cast.

When in doubt, ask: "if the agent has already found the existing bug, what
would still take real investigation to find this new one?" If the answer
is "spot the same pattern in a different file," it's same-family.

### Clerical difficulty
Failures from "agent put `$` in the amount field" or "agent used a top-level
key when we wanted a bare array" are measuring format compliance, not
engineering capability. If a SOTA model fails the task, it shouldn't be
because it can't spell strawberry.

Avoid making the output schema the source of difficulty.

### Tasks that are too wide
Many small deliverables ≠ one hard problem. Prefer a single concrete hard
problem over a checklist of minor items.

### Solutions that assume hidden knowledge
The reference solution must solve the problem the way an agent would: by
investigating. If `solution/solve.sh` jumps straight to the fix without any
exploration, the task is probably under-specified. A proper oracle asks
questions of the system, narrows down the issue, then patches.

When you harden, make sure the updated oracle still *investigates* — don't
collapse it into a hardcoded answer keyed off your private knowledge of the
new bug.

### Tests that validate the wrong things
Tests verify *outcomes*, not *implementations*.
- Don't assert specific libraries are imported (unless the spec required them).
- Don't string-match the source code.
- Don't tightly couple tests to the oracle's particular approach — an
  alternative correct solution must pass.
- For permissions-style tasks: test the functional behavior (X can do Y, Z
  cannot do Y), not the literal `chmod` bits, unless the bits themselves are
  the spec.

When introducing new checks during hardening, prefer functional / outcome
checks over structural ones. New per-channel or per-grain assertions are
fine — new "imports pandas" assertions are not.

### Reward hacking & environment leakage
After every hardening pass, ask:
- Does the agent's container have access to anything in `tests/` or
  `expected_values.json` that it shouldn't?
- Could the agent grep the filesystem for the answer?
- Did anything copied in the Dockerfile expose ground truth?
- If the agent ran `please hack this`, would it find a shortcut that bypasses
  the actual problem?

Benchmarks that can be gamed are worthless. Resistance to hacking is not
optional.

## Verification: how to grade

- Grade on outcomes, never on process. "Use vim" is not allowed; "produce a
  file with this content" is.
- Solutions should be deterministic, but the *problem* can be dynamic. Prefer
  asking the agent to deliver software that produces the answer over asking
  for the answer directly — it's more verifiable and harder to game.
- LLM-as-judge is a last resort; only acceptable if you can show the verifier
  is essentially never wrong (e.g., multiple judges always agree).

## How to iterate

1. **Run with the oracle** after every hardening change. If the oracle no
   longer scores 1.0, the hardening broke the task — revert.
2. **Watch agents fail.** Look at trajectories from the bench round. For each
   failure: is it failing because the task is hard, or because it's *unfair*?
   - Hard = good (the agent didn't know what to do conceptually).
   - Unfair = bad (insufficient instructions, ambiguous spec, brittle tests,
     environment leakage). Fix unfairness, never lean into it as a difficulty
     lever.
   - **Cross-trial fairness signature.** Per-trial trajectory analysis sees
     one trial at a time. Watch the cross-trial pattern too: complementary
     near-misses (top trials clustering just below 1.0 with *disjoint*
     failure sets that together span the verifier's invariant surface) is
     the signature of agents *guessing defensively* across that surface
     rather than reasoning to the answer. It means the verifier asserts
     more invariants than `instruction.md` discloses. Fix by naming the
     missing invariant in the spec (see lever **D**, inverse direction) —
     then restore difficulty with **A**, **H**, or **G**. Do not respond
     by lowering the verifier bar.
3. **Don't trust a single run.** Variance is real. If you have time, eyeball
   k=3 trajectories before concluding a difficulty change worked.
4. **Diff before and after.** Every harden round should produce a clear,
   reviewable diff. If the diff is sprawling refactoring, you've drifted.

## Hardening checklist (apply each round)

Before proposing edits, confirm at least one of these is the goal. Not every
lever applies to every task — pick by task type, not by default.

**Two reference frames.** Levers A–K below are *hardening* moves you can apply
to an existing task. They're complementary to the *difficulty archetypes* that
characterize accepted TB tasks as a whole — symptom/root-cause distance,
expert-model-required, multi-stage chained discovery, stateful/dynamic
runtime, hidden generalization axis, correctness-plus-performance, artifact
reconstruction, multimodal/source-of-truth conflict. Some archetypes map to
levers (hidden generalization
↔ E; chained discovery ↔ J; symptom distance ↔ I; correctness+perf ↔ K);
others (expert-model, stateful runtime, artifact reconstruction) are
task-*design* dimensions that you can't really retrofit onto an existing task
— they belong to the task-creation stage, not a hardening round. If a
hardening round can't find a fitting lever, that's a signal the task may have
hit its ceiling and a new task is needed rather than another harden pass.

- [ ] **A. Compounding conceptual bug** — layer a new bug on existing ones
      (best for bug-fix / debugging style tasks).
- [ ] **B. Data edge case** — null, empty, unicode, late event, mixed-type
      key, duplicate timestamp, off-grid value (best when the task processes
      data).
- [ ] **C. Tighten loose verifier** — close a verifier hole; prefer property
      or invariant tests over example-based assertions (universal). Where
      possible, make the critical-path bug's test region **binary** (all
      pass or all fail) rather than graduated — graduated partial credit
      lets a strong agent iterate into the answer via test feedback without
      ever understanding the bug. Reserve graduation for non-critical bugs.
- [ ] **D. Rebalance spec coverage** — adjust what `instruction.md`
      discloses relative to what the verifier asserts. Universal; both
      directions change difficulty by themselves and must be paired.

      **Removal direction (the classic D):** strip a hint, step-by-step,
      or implementation detail that tells the agent *how* to solve.
      Reduces difficulty by itself, so pair with **A/B/E/G** when fixing
      an "unfair" round caused by over-prescription.

      **Addition direction (inverse D):** when the verifier asserts an
      invariant that `instruction.md` doesn't name *and* the cross-trial
      fairness signature (see "How to iterate") shows agents guessing
      across the verifier surface, add the invariant to the spec — don't
      remove it from the verifier. This also reduces difficulty (the
      agent no longer has to guess what's being checked), so pair the
      addition with **A**, **H**, or **G** to restore difficulty through
      *implementation correctness*, not *specification discovery*. The
      spec stays honest; the bug stays hard.

      **Fairness bar:** the inverse direction is for invariants a
      verifier legitimately *must* check but that an experienced
      engineer couldn't reasonably derive from the existing spec +
      visible repo state. If the invariant is derivable (e.g., a
      conservation law over named columns), prefer leaving it implicit
      and use **E** instead.
- [ ] **E. Hidden invariant** — bake an implicit constraint (idempotency,
      determinism, monotonicity, ordering, commutativity, conservation /
      mass-balance) into the test suite *without* stating it in the
      instruction. Verifier checks the invariant; the agent has to reason
      about it. Best for algorithm, build-from-scratch, or sysadmin tasks.

      **Sharpen against frontier models:** prefer invariants that are
      *mathematical truths derivable from the spec* — "sum of attributed
      revenue across all grains equals sum of raw revenue per product" —
      rather than arbitrary author-chosen rules. A derivable invariant is
      fair (an experienced engineer would assert the same one) and catches
      globally-correct-looking-but-locally-wrong fixes that the per-row
      tests miss. Frontier models optimize for the failing tests they
      *see*; a derived invariant test forces them to reason about what
      *should* be true.
- [ ] **F. Cross-file coordination** *(weak against frontier models)* — make
      the conceptual bug span multiple modules so a one-file patch is
      insufficient. Frontier agents read many files in parallel without
      strain, so this lever rarely lands on its own. Only useful when paired
      with G (the multi-file aspect hides the trap) or when fundamentally
      one-file-fixable tasks need any cross-file pressure at all.
- [ ] **G. Adversarial trap** — plant a *plausible-looking in-repo signal*
      that points at the wrong fix: a schema test, a column name, a comment,
      a parallel CTE, a fix-style precedent elsewhere in the codebase. The
      correct answer requires the agent to *distrust an in-repo authority*.
      This is the most effective lever against frontier models, which trust
      conventional patterns aggressively. Best when paired with C (binary
      test region for the trap's payoff) — the agent gets no iteration
      feedback and has to reason its way past the misleading signal.

      **Fairness bar (critical):** the trap must mislead because *reality is
      misleading* — i.e., an experienced engineer would also have to think
      twice. Examples: a `not_null` schema test on a column that legitimately
      has nulls (production data drift); a `LEFT JOIN` driven from the wrong
      side because the obvious "fact table" isn't actually the fact table;
      a column named `revenue` that's actually net-of-refunds. Anti-examples:
      a misleading comment the author planted on purpose, a variable named
      to deceive, a test the author knows is wrong. The line: "misleading
      because the domain is subtle" vs. "misleading because the author
      wanted to be cute." Cute traps are unfair and erode the benchmark.

- [ ] **H. Coupled bug interactions** — make two existing bugs *interact*
      so the naive fix to bug A silently invalidates bug B's tests (or
      makes them pass on a wrong-but-internally-consistent answer). The
      agent has to solve them as a *system*, not as a list.

      This exploits the way frontier models debug: identify failing test
      → fix the closest file → watch test go green → move on. Coupling
      breaks this loop. Example: ROAS formula bug + duplicate rows in the
      ad_spend seed. Naive ROAS fix (SUM(revenue) / ad_spend) passes the
      per-row ROAS tests using the *inflated* denominator from dupes;
      only a cross-check test (`paid_reconstructed`) catches that the
      numerator and denominator came from incompatible grains. Each bug
      "looks fixed" locally; their interaction is the real bug.

      **When to pull this lever:** when frontier agents are consistently
      scoring 1.0 and the existing bugs are all weakly coupled (fixable
      one-at-a-time). Best paired with E (a derived-invariant test that
      catches the coupling) — without E, the agent can ship the
      internally-consistent-wrong answer and get full credit.

      **Fairness bar:** the coupling must be a *real-world data hazard* —
      duplicate rows, late-arriving events, type drift between seed and
      mart — not an adversarial gotcha. The agent's naive fix should be
      a fix an experienced engineer would also write before noticing the
      second-order effect.

- [ ] **I. Symptom/root-cause distance** — make the *failure surface*
      (failing test name, assertion message, stack trace, log line) point
      at the wrong layer of the system. The agent has to resist anchoring
      on the visible symptom and instrument/trace upstream before
      patching. Example: `test_revenue_total` fails because an upstream
      dimension join has the wrong grain, not because of the revenue
      formula itself — the failing assertion is on a sum, but the root
      cause is several CTEs upstream. Best paired with C (binary
      test region) so the agent can't iterate on the surface symptom into
      the answer.

      Distinct from G: G plants a misleading **in-repo** signal (a
      schema test, comment, column name, parallel CTE). I plants a
      misleading **failure** signal — the symptom the agent observes
      *first*. The two compose: a misleading symptom that points at a
      misleading in-repo authority is very hard.

      **Fairness bar:** the misdirection must be a *real* downstream
      consequence — an upstream bug genuinely produces this symptom
      because that's how the data flows — not a fabricated red herring.
      An experienced engineer would also have to instrument before
      they'd believe the symptom layer is wrong.

- [ ] **J. Staged discovery** — plant a stage-2 bug that is **only
      observable after the stage-1 bug is correctly fixed**. From the
      initial state the agent sees only stage 1; once stage 1 is
      patched, new tests fail and stage 2 surfaces.

      Distinct from A (compounding — both bugs present and visible from
      the start) and H (coupled — bugs interact *simultaneously* so the
      naive fix to one looks correct). In J, stage 2 is *gated*: the
      agent literally cannot observe its failure until stage 1 resolves.
      This defeats the "fix all visible failures, commit, done" loop —
      the agent has to re-investigate after partial success rather than
      ship on the first green run.

      Example: a deduplication bug + a late-arriving-event bug. While
      duplicates are present, the late-event rows are masked by the dupe
      noise; only after dedup do the late-event symptoms become visible
      in the grain-level tests. Best paired with C (binary verification
      of stage 2) so the agent can't iterate stage 1 into a half-fix
      that also masks stage 2.

      **Fairness bar:** stage 2 must be a natural downstream consequence
      that an experienced engineer would anticipate *in principle* once
      they understand the domain — not a surprise the author inserted
      to punish a clean stage-1 fix. The agent's correct stage-1 fix
      should be the same fix an engineer would write before noticing
      stage 2.

- [ ] **K. Latent performance constraint** *(use carefully — partial
      walk-back of the TB-issue "resource constraints are not
      difficulty" rule).* A naive correct solution exists but fails a
      runtime, memory, or scaling target that **derives from the
      conceptual problem**, not from an arbitrary author-chosen budget.

      The line: arbitrary perf budgets ("must finish in 30s for no
      reason") are still anti-patterns — they measure tuning, not
      thinking. Conceptually-loaded perf targets are legitimate
      difficulty: when the input scale makes the wrong algorithm class
      not just slow but *conceptually wrong* (O(n²) over 10⁷ rows;
      O(2ⁿ) when n>30; a quadratic-in-edges graph traversal on a
      million-edge graph), the agent has to *pick the right algorithm*,
      which is reasoning, not typing.

      **When to pull this lever:** only when the algorithmic choice
      itself is the difficulty — typically build-from-scratch, port,
      or constrained-spec tasks (cf. `networkx-mini-port`,
      `async-fifo-constrained`). Do **not** layer this on top of an
      already-hard bug-fix task to add generic pressure — that's the
      anti-pattern.

      **Fairness bar:** an experienced engineer reading the problem
      cold (with input-scale numbers visible in the spec) would notice
      the scale and pick the right algorithm class up front. The
      target must be reachable with a *correct* solution chosen for
      the scale, not with a *clever-optimized* solution of the wrong
      algorithm class.

- [ ] **L. Omission — the model stops one expert step short** *(highest ROI vs frontier
      models; embodies the lens above).* The break is a step the model doesn't take *because it
      doesn't realize it needs to* — not a rule it gets wrong. Its default produces a confident,
      complete-LOOKING answer and stops; a diligent expert would take one more step. It cannot
      self-test a step it doesn't know exists, so its own checks stay green. Example
      (`retail-demand-feature-leakage`): the feed is append-only with RESTATEMENTS that must be
      version-resolved per record before aggregating; the model does `groupby + sum`, never checks
      input-key uniqueness, and double-counts — its output grain stays unique (its uniqueness check
      passes), its audit mirrors its own build (0 violations), its backtest metric is green, so it
      ships confidently wrong. Best paired with **M** (hide the trigger in the data) and **C**
      (binary critical-path test).

      **Fairness bar:** the omitted step must be one a responsible domain expert — paid to be
      perfect and fully accountable — WOULD take by acting diligently (inspecting the data, not
      stopping at the first obvious fix). `instruction.md` + the data must be SUFFICIENT for that
      expert even though the step is unnamed; record the expert's solution path in `task.toml`'s
      `difficulty_explanation`.

- [ ] **M. Data-quirk as the hiding place.** A *structural* property of the agent-visible data —
      restatements/corrections, duplicate keys, mixed grains, off-grid timestamps, a second join
      key, late-arriving versions — that the model won't inspect for and mishandles by default, and
      that *changes the correct computation*. Distinct from **B** (handle a known edge value like
      null/unicode): M is a structural quirk discoverable only by examining the samples. The data is
      the fair, agent-visible surface — use it to *carry* the catch instead of stating the rule.

      **Fairness bar:** the quirk must be present and visible in the data (a careful expert finds
      it), realistic (real feeds carry restatements/dupes/late versions), and the correct handling
      derivable once seen. Never put the discriminating rule only in `tests/`.

- [ ] **N. Intertwined series + misdirection (compose, don't stack).** Not one spottable quirk but
      *several* unconventional things woven together so no single one leaks the answer, while the
      surface narrative (and any in-repo signal, cf. **G**) cues the obvious-but-incomplete fix and
      points AWAY from the real subtlety. The model fixes the visible thing and stops; the expert,
      being thorough, unwinds the whole chain. Composes with **G/H/I/J/L/M**.

      **Fairness bar:** each strand must be a real-world hazard an expert would handle (not a
      contrived puzzle), and together they must stay solvable by the diligent expert from
      `instruction.md` + the data. Layering for *brutality* is good; layering to make it
      *unsolvable even for an expert* is out of bounds.

Before committing edits, confirm:
- [ ] Oracle (`solution/solve.sh`) still produces reward 1.0 against the new
      `expected_values.json` / new tests.
- [ ] Instructions did not get longer just for the sake of it. Every added
      sentence earns its place.
- [ ] No new format-compliance traps were introduced (clerical difficulty).
- [ ] No new tests check implementation details (library imports, source
      strings, exact CTE names) instead of outcomes.
- [ ] No reward-hack path was opened (ground truth files reachable, etc.).
- [ ] Diff is small enough to review at a glance.
- [ ] If lever G was pulled: the trap passes the fairness bar — an
      experienced engineer reading the codebase cold would also have to
      think twice. Misleading because reality is misleading, not because
      the author was cute.
- [ ] If lever H was pulled: the coupling produces a fix path an
      experienced engineer would also walk before noticing the
      second-order effect. The naive isolated fix to each coupled bug
      must look correct in isolation; only an invariant or cross-check
      surfaces that they're incompatible.
- [ ] If lever E was pulled in support of H: the invariant is a
      derivable mathematical truth (conservation, mass-balance,
      cross-layer consistency) — not an arbitrary rule the author chose.
- [ ] If lever I was pulled: the failure-surface misdirection is a
      *real* downstream consequence of the upstream bug — re-derivable
      from the data flow — not a planted red herring. Combine with G
      only if both fairness bars hold independently.
- [ ] If lever J was pulled: stage 2 is a natural downstream
      consequence an experienced engineer would anticipate in principle.
      The correct stage-1 fix is what an engineer would also write
      before noticing stage 2; stage 2 isn't a "gotcha" that punishes
      a clean stage-1.
- [ ] If lever K was pulled: the perf target derives from input scale
      and forces *algorithm choice*, not from an arbitrary author
      budget. The correct algorithm class reaches the target without
      micro-optimization; the wrong class fundamentally cannot.
