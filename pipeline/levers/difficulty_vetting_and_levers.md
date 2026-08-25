# Where Does Difficulty Actually Come From?

## Two simultaneous truths that the blog/seed-form and the corpus reveal

**(A) The blog says:** difficulty must come from the *problem*, not from clerical scaffolding, resource starvation, or expansiveness. Undergrad-in-a-few-days = too easy. The instructions should be terse, the difficulty intrinsic.

**(B) The corpus reveals:** only a minority of TB3 tasks (`prove-takens-embedding-lean`, `jtptl-structural-fault-locator`, `mp-checkpoint-consolidation`) are hard purely because the *underlying problem* is hard. Most achieve their difficulty through **construction** — layering, scattering, hiding, composing. `ssti-secret-key` stacks five obstacles. `phantom-wallet-forensics` is hard because it's a serial pipeline with no intermediate signal. `binary-sprite-extraction` plants a transpose-symmetric trap that fakes partial success. These aren't "intrinsically hard problems" — they're well-architected presentations.

**This is the wedge for your pipeline.** The "intrinsic problem" half needs a domain expert and can't be auto-generated. The "architecture of presentation" half is largely proceduralizable. Your seed → pipeline split should mirror this split.

## Where difficulty lives, by automatability

| Source | Example tasks | Pipeline can create? |
|---|---|---|
| **Deep domain expertise** (the *what*) | takens-Lean, varswap-volconv, mp-checkpoint, ECDSA-nonce-attack, jtptl-physics | **No.** Must come from a human who lived in that domain. |
| **Construction patterns** (the *how*) | ssti (5-layer obstacles), phantom-wallet (no-signal pipeline), binary-sprite (trap), service-mesh (misleading comment) | **Yes — recipe-able.** |
| **Environmental adversaries** | apache-cxf-ssrf (WAF), cve-2020-17526 (IDS), ssti (WAF+honeypot) | **Yes — generatable & tunable.** |
| **Distributed-state correctness** | service-mesh-cert, kv-live-surgery, crash-safe-agent-loop, ros2-xray | **Partially.** Skeleton automatable; the *specific* invariant violation needs an authentic systems thinker. |

The construction-pattern row is your sweet spot — and notably, it's where humans most often fail. The blog's "first several proposals were rejected" comment maps cleanly onto authors who had a real kernel but constructed it badly.

## Why proposals fail (reading between the blog's lines)

1. Hard kernel wrapped in clerical scaffolding (output-format gymnastics, step-by-step prescription).
2. Genuinely a college course assignment — hard for an undergrad in 2 hrs, easy for SOTA in 5 min.
3. Hard but unverifiable, or hard only via withheld information that wasn't fairly inferable.
4. Real engineering but not interestingly hard for LLMs (e.g., "refactor 200 files" — laborious, not difficult).

A vetting pipeline must catch all four. The current PDF form catches (1) and (3) decently; it doesn't catch (2) or (4) at all.

# A proposed framework

## Step 1 — The seed form is the right starting point, but extend it

The PDF's four fields (Instruction Crux / Solution Crux / Difficulty Crux / Verification note) are excellent — they pre-empt the most common antipatterns by forcing the human to articulate the crux before authoring. I'd add three:

5. **Failure-mode hypothesis** — "When a SOTA model fails this, *what specifically* will it do wrong?" Forces a falsifiable prediction. If the human can't name a specific failure mode, the difficulty is probably illusory. Catches antipattern (4).
6. **Hidden-knowledge audit** — "What do you (the author) know that the agent won't? Is each item fairly inferable from the environment?" Forces the "solutions that assume hidden knowledge" antipattern out into the open.
7. **Adversarial sketch** — "How might an agent cheat or shortcut this?" Forces anti-cheat thinking before tests are written. Cheaper than the blog's "please hack" trial, complementary to it.

## Step 2 — Vet the human via the seed, not credentials

The blog's vision is that good tasks come from "a problem someone actually had to solve." Resume-vetting is a worse signal than seed-vetting. Concrete protocol:

- **Stage A — seed lint** (LLM-graded, cheap): does each field pass the antipattern checks? Concrete or vague? Does the difficulty crux describe the *problem*, or sneak in environmental friction / output formatting?
- **Stage B — undergrad heuristic** (LLM-graded): "Could a strong undergrad solve this in <3 days given the seed?" If yes, flag.
- **Stage C — failure-prediction grounding** (run a SOTA model on the seed-generated draft task; see if its actual failure mode matches the human's predicted one). This is *the* most powerful vetting signal — it ground-truths the human's expertise. A human who can predict how models fail at their domain has the right intuition.
- **Stage D — solution-as-oracle test**: ask the human for a one-paragraph solve sketch. If it jumps to the answer instead of describing investigation, flag for hidden-knowledge risk.

A human who clears C and D is almost certainly a good task author. Most credentialed humans will fail C.

## Step 3 — Difficulty levers (the heart of automation)

Levers = the proceduralizable construction patterns the corpus survey surfaced. For each lever: what the human declares, what the pipeline implements, which LLM failure mode it targets.

### Tier 1 — Pure pipeline implementation (no LLM creativity needed beyond seed)

| Lever | Human declares | Pipeline implements | Targets |
|---|---|---|---|
| **Layered-obstacle composition** | domain + path (e.g. "web exploitation, SSTI route") | Stacks decoys / honeypots / WAF / disabled-gadgets in env | Long-horizon planning, false-progress |
| **Serial pipeline w/ no intermediate signal** | stage list `[parse, decrypt, recover-key, decode]` | Builds env so each stage's correctness is invisible until final | Lack of verification discipline |
| **Convention-trap injection** | domain conventions `[day-count, spot-vs-fwd, log-vs-pct]` | Plants k independent convention bugs, each individually plausible | Default-pattern hallucination |
| **Red-herring scaffolding** | decoy paths/endpoints/files | Wires plausible-but-wrong solution attractors | Premature commitment |
| **Distributed-state scattering** | core invariant + module count | Distributes the bug/requirement across N modules | Local-only reasoning |
| **Adversarial environment middleware** | adversary type + rule set | Generates the WAF/IDS/sandbox layer | Pattern-attack reflex |
| **Misleading-comment plant** | the false claim text | Inserts authoritative-sounding wrong comments in the codebase | Social engineering by authority |

### Tier 2 — Pipeline + LLM-augmented

| Lever | Human declares | LLM/pipeline does | Targets |
|---|---|---|---|
| **Realistic codebase synthesis** | scale + framework | Generates plausible surrounding code so the bug doesn't stick out | Pattern-matching laziness |
| **Convention proliferation** | convention spread spec | LLM authors realistic-looking docs/comments that obscure the convention choice | Domain-knowledge probing |
| **Anti-cheat hardening** | (auto-triggered) | Runs the "please hack" trial against env, finds shortcuts, plugs them, repeats | Reward hackability |
| **Test-instruction alignment audit** | (auto-triggered) | LLM reads instruction in isolation, predicts tests; flags mismatches | Test-coupling-to-implementation antipattern |

### Tier 3 — Irreducibly human, do not automate

- **The core insight** (Solution Crux). This is lived engineering experience.
- **Outcome-level verification semantics.** Tests for `varswap-volconv` require knowing real quant conventions — no LLM should be deciding which convention is "right."
- **Lever selection.** A weak author wants to add 5 levers; a good author knows one well-placed lever beats five sloppy ones. The pipeline should *recommend* a small lever set per seed (1-3), not let the human pile on.

## Step 4 — Difficulty calibration loop

The blog: "testing against models is the best diagnostic we have." Close this loop in the pipeline:

1. Generate task variant with lever set L₀.
2. Run K SOTA agents on it (5-10 trials).
3. If pass-rate too high → escalate (add a layer, scatter further, tighten WAF). If too low → before adding hints, **check whether agents fail for the human's predicted reason**. If they fail for a different reason, the task is hard for the wrong reason — fix that, don't just lower difficulty.
4. Cross-reference the trajectories. If models fail at "the agent uses nano and gets stuck in interactive mode" (real example from the blog), that's environmental friction, not problem difficulty — strip it.

This is where automation pays off most: a human can't run 20 agent trials per variant per night; a pipeline can. The pipeline tunes lever knobs to land in *fails-for-the-predicted-reason at the predicted rate*.

# Tensions and risks worth attention

1. **Authenticity vs. construction.** The blog's strongest claim is that good tasks are *real problems someone had*. Construction-pattern levers are by definition synthetic. My instinct: the kernel must be real (from the human seed); construction should *mimic real-world messiness*, not contrive it. A "5-layered obstacle course" only works if each layer corresponds to a real-world thing (real WAFs, real honeypots, real gadget-disabling) — otherwise it reads as a puzzle, not engineering.

2. **Where do good seeds actually come from?** The blog's vision: "every week you spend a few hours solving a problem, that's a task." The hiring funnel should be biased toward people who *do real engineering* and can articulate why a problem was hard, not toward people who *like to write benchmarks*. The seed form is the single best filter — gate hiring on it, not on resume.

3. **Lever sprawl.** Resist adding 30 levers. The corpus shows that 1-3 well-chosen levers beat a stack. The pipeline should *propose* a minimal lever set per seed, with rationale, and let the human approve.

4. **The LLM-failure-mode trap.** A pipeline tuned to exploit *current* LLM weaknesses ages out fast — exactly what happened to TB2. Levers tied to *engineering essence* (state-machine correctness, multi-stage discovery, convention traps) age slower than levers tied to specific weaknesses (long context, TUI navigation). Bias the lever taxonomy toward the former.

5. **Verifiability scales worse than difficulty.** It's easier to add a lever that makes a task harder than to add one that keeps verification clean. Every lever needs a verifiability impact check: does adding this lever require the tests to know things that aren't outcome-level? If yes, it's a tax.

6. **The "real problem" leak risk.** If seeds come from real engineering work, you'll get pull-from-life tasks — but those may contain proprietary knowledge, embargoed CVEs, or material the human doesn't have rights to share. Build a clearance step into the seed form before any pipeline work.

# Concrete first move

Don't build the lever pipeline first. Build the **seed-vetting tool** first.

1. Take the PDF form, add the three additional fields above (failure-mode hypothesis / hidden-knowledge audit / adversarial sketch).
2. Build a cheap LLM-graded vetting pass on each field with the blog's antipatterns as negative examples.
3. **Critical experiment**: run a SOTA model on the seed's *Instruction Crux alone*, no environment. If the model can answer it directly, the difficulty is in the environment, not the problem — yellow flag per the blog.
4. Calibrate against ground truth: take the 30 merged TB3 tasks, recover their seeds, run the vetter — it should pass them. Take the publicly-known rejected proposals or your own batch of 50 weak seeds — it should reject them.

Once you have a robust seed-vetter, you'll have a much clearer picture of *which levers humans actually need versus which are aesthetic*. Grow the lever taxonomy from observed seed-to-task gaps, not from up-front design.

---

## Short answer to the underlying question

**Does difficulty come from expert humans or LLM failure modes?**

Both, but at different layers. The *kernel* is irreducibly human (a real problem, with real stakes, articulated by someone who lived it). The *amplification* is where LLM failure modes come in — the construction patterns above are deliberate exploitations of where models break (long-horizon, no-signal pipelines, convention defaults, social-engineering compliance). The pipeline's job is to take an authentic kernel and apply targeted amplification — never to manufacture difficulty from scratch.
