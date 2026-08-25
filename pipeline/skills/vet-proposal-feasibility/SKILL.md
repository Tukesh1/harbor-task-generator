---
name: vet-proposal-feasibility
description: Find the breakable angles in a task proposal BEFORE Stage 2 — a creativity AID, not a reject gate. Every task is breakable against a frontier model with the right angle; this surfaces HOW (the self-verifiability read, candidate omission/data-quirk angles, which levers fit, and the expert-fairness path) so Stage 2 starts with a running head start instead of rediscovering it.
---

# Find a proposal's breakable angles (a Stage-2 head start)

A task is useful to us when it is **both** model-breaking (a frontier model fails all k
trials) **and** fair (a responsible domain expert solves it from `instruction.md` + the
agent-visible environment). **Every task is breakable with enough creativity** — this skill
does **not** accept/reject seeds; it gives each proposal a **breakable-angle brief** so Stage 2
doesn't burn rounds rediscovering the angle. (Stage 2 is expensive — ~$50+ and up to 15 rounds
per seed — so a good brief pays for itself.)

## The diagnostic lens: is the OBVIOUS answer self-verifiable?
The single most useful read on a proposal:

> **"If the model produces the obvious answer, can it cheaply CHECK that answer itself —
> implement the stated approach AND property-test / re-derive / sanity-check it green?"**

- **If NO** (the obvious answer is genuinely hard to self-verify) → the difficulty is intrinsic;
  a fair break can rest on the core problem itself. Easy mode — note the core as the angle.
- **If YES** (the model can self-check the obvious answer — most clean, fully-specifiable tasks)
  → the break must come from something the model **won't think to self-check**: an **OMISSION**
  (a step it doesn't realize it needs) or a **DATA-QUIRK** (a structural property of the data it
  won't inspect for). This is NOT a reason to drop the seed — it is the signal for *where the
  angle lives*. A frontier model implements any stated rule and self-tests it, so on a
  self-verifiable seed the angle is always "what would a diligent expert do that the model skips,
  and how do I make the model's own checks stay green on the wrong answer?"

## Tells that the OBVIOUS framing is self-verifiable (→ look for the omission/data-quirk angle)
These used to read as "drop" signals; treat them instead as *"the surface framing is
self-checkable — find the angle underneath."* The proposal:
- frames it as **reconcile / repair / dedup / recover / audit / fix-the-bug** to match a result;
- centers on an **idempotency/dedup key**, a **normalization/matching rule**, a
  **supersession/precedence/ordering convention**, a **watermark/partition-close policy**, a
  **checkpoint key**, or a **threshold/tolerance**;
- has "non-obvious steps" that are really conventions a senior engineer would just write down;
- relies on a threshold doing the work (`near_miss`): the model nearly passes.

When you see these, the obvious answer is self-verifiable → **the break is an unstated step the
diligent expert takes that the model omits, or a data structure the model mishandles by default.**

## The angle brief (what to produce per proposal — instead of a verdict)
For each proposal, surface:
1. **Obvious answer + self-verifiable?** State the answer a strong model would produce and whether
   it could self-check it green (use the lens + tells above).
2. **What's SPECIAL about THIS variant** that a strong-but-generalist agent would get wrong even
   applying real care — the domain subtlety only a specialist respects.
3. **Omission angle** — the step a responsible, accountable expert would take that the model won't
   realize it needs (so its own checks stay green on the wrong answer).
4. **Data-quirk angle** — a structural property of the data (restatements, duplicate keys, mixed
   grains, off-grid timestamps, late versions) that carries the catch, discoverable by inspecting
   the samples (never hidden only in tests/).
5. **Levers that fit** — which of A–N (incl. L omission / M data-quirk / N intertwined-series); can
   several be intertwined so no single one leaks?
6. **Expert-fairness path** — how a diligent expert still solves it, for `difficulty_explanation`
   (keeps `harbor analyze` + the reviewer satisfied even when the task is brutal).

## Case studies — self-verifiable framings, and the FAIR-breaking angle
Our two earlier seeds broke the model only by **hiding the rule in tests/** — unfair, rejected for
`task_specification`. The lesson is not "drop them"; it's *"keep the catch, but move it from
hidden-tests to data-discoverable (an omission a diligent expert would catch)"*:
- **eval-harness-contamination-audit** — the break hinged on whole-token-vs-character matching, with
  the rule only in the oracle. **Fair angle:** make the matching ambiguity a property visible in the
  data the expert would inspect (e.g. obvious token-boundary collisions in the sample corpus a
  careful auditor would test for), so the diligent expert handles it and the model's substring
  default ships green.
- **webhook-ledger-idempotent-replay** — the break hinged on a temporal refund gate documented only
  in solution/tests. **Fair angle:** surface restatement/late-arrival structure in the visible
  ledger data so a diligent reconciler version-resolves before aggregating (the omission), instead
  of stating — or hiding — the gate as a rule.

## When triaging a list — output format
One line per proposal:
`<name> — obvious-answer self-verifiable? Y/N — best angle (omission|data-quirk|intrinsic) in ≤8 words — fitting levers`
plus, for each, a 2–4 line angle brief (items 1–6 above). Never a KEEP/FIX/DROP verdict — every
seed gets an angle; flag only genuine non-starters (no plausible real-world scenario at all) for a
human look. Judge from the proposal's actual difficulty/solution crux, never the filename.
