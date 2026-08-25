# Task Proposal: spsc-ring-intermittent-tear

**Domain:** Concurrent systems (SWE)

## Overview

A "fast" lock-free single-producer single-consumer (SPSC) ring buffer is given in C11, used to hand packets between a network thread and a worker thread on a production fleet. The buffer passes every functional test on the developer's x86 laptop and passes ThreadSanitizer on x86 in CI, but in production an assertion fires intermittently: the consumer occasionally sees a packet whose `length` field is zero while its payload bytes are filled in. The failure is rare (roughly once per few hundred million enqueue/dequeue cycles), entirely absent from staging, and was bisected to the ring-buffer commit rather than the network or worker code.

The agent has the source of the ring buffer, a small reproducer harness, the production failure logs and SoC information from a sample of the production hosts, and access to representative production-class hardware in the test environment. The agent is not told what class of bug this is.

## Instruction Crux

Produce a ring-buffer implementation that, when dropped into the same harness, passes both (a) a long-running stress test of 10^9 enqueue/dequeue pairs across two pinned cores on production-class hardware without ever observing a torn packet, and (b) a stateless model-checker pass over the implementation that exhaustively explores executions allowed by the C11 memory model and finds no execution in which the consumer observes the published tail while still seeing stale or zero payload bytes.

The behavior of the API is fixed: a single producer calls `ring_push(ring, packet)`, a single consumer calls `ring_pop(ring, &packet)`, and the consumer must see the full packet contents written by the producer before it observes the incremented tail. No locks may be introduced and the steady-state push/pop must remain on the order of tens of nanoseconds — the agent is free to use whichever synchronization mechanism it wants (C11 atomics, inline assembly, hardware fences) so long as both gates pass.

## Solution Crux

The original code uses `atomic_store_explicit(&tail, new_tail, memory_order_relaxed)` after a plain (non-atomic) write of the packet payload. On x86 this happens to work because x86 is effectively TSO and stores are seen in program order; the production hardware uses a weaker memory model, on which the store to `tail` can become globally visible *before* the payload store retires, so the consumer reads the new tail and then reads stale payload bytes — including a zero `length` from a prior generation.

The right fix establishes a release/acquire publication relationship between the producer's payload writes and the consumer's payload reads: any mechanism that orders the payload writes before the tail publish, and the tail load before the payload reads, will pass. The canonical C11 form is `memory_order_release` on the tail store paired with `memory_order_acquire` on the tail load. Equivalent fixes — an explicit `DMB ISH` between the payload write and the tail store paired with the right consumer-side barrier, hand-rolled `LDAR/STLR` in inline assembly, or `memory_order_seq_cst` everywhere — are also accepted by the model checker so long as the publication property holds. The non-obvious step is recognizing that this is specifically a publication-ordering bug rather than an ABA, an off-by-one, or a torn-read at the data-race level, and producing a fix the model checker can prove correct rather than a fix that just happens to not reproduce in five minutes of stress.

## Difficulty Crux

The bug is invisible to the most common tooling: TSAN on x86 does not see it, single-machine stress on x86 does not see it, and even on the affected hardware the bug is rare enough that running for a minute does not reproduce. The agent has to (1) form the hypothesis that this is a publication-ordering bug under a weaker memory model rather than an ABA bug, an off-by-one in the indices, a torn-read of the tail counter, or a cache-line false-sharing artifact — all of which are plausible-looking alternatives given the symptom; (2) figure out a synchronization mechanism that is both sufficient and cheap enough to hit the perf bar; and (3) be able to certify the fix against a formal model of the C11 memory model, rather than just "it didn't repro in 10 minutes." This is the kind of bug that takes experienced systems engineers days, and most "fixes" that pass casual testing are wrong in ways the verifier catches.

Difficulty here is conceptual: it does not come from format requirements, prescribed steps, runtime length, or task size. The code is small. The hard part is reasoning about an unexpected, hardware-dependent observation when none of the local symptoms point cleanly at the right hypothesis.

## Verification Note

Two gates. The functional gate runs the agent's implementation under the supplied harness on production-class hardware for 10^9 push/pop pairs with adversarial scheduling and asserts no torn packet is observed. The formal gate runs a stateless C11 model checker (e.g., GenMC) against the agent's source as-is — no rewriting, no signature substitution — and asserts that the property `consumer_sees_published_tail ⇒ consumer_sees_payload_writes` holds on every execution allowed by the C11 memory model. The model checker is agnostic to which primitive the agent uses; C11 atomics, hardware fences, and inline assembly all reduce to the same execution-level reasoning. Both gates are deterministic and re-running the verifier on a correct fix or the broken original produces the same verdict every time. No LLM judge.
