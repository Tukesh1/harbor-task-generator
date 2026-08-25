# Task Proposal: rust-async-mutex-await-deadlock

**Domain:** Concurrent systems / Async Rust (SWE)

## Overview

A production Rust service built on Tokio has been gradually accumulating latency under load. On a fresh start it serves p99 in single-digit milliseconds; after roughly an hour of sustained traffic, p99 climbs into the seconds, throughput halves, and `tokio-console` shows a steady backlog of tasks that are reachable but never make progress. Restarting the process resets the symptom. The service uses a moderate amount of internal state — a few shared caches, a metrics aggregator, a connection pool — and the team's last refactor moved several of those behind `Arc<Mutex<...>>` using `std::sync::Mutex`. Code review passed; clippy is clean; the unit tests are green.

The agent has the service source, a load generator that reproduces the slow climb in roughly 5 minutes by amplifying the traffic shape, the `tokio-console` snapshot from a degraded run, the `perf` profile during degradation, and a held-out set of traffic patterns the verifier will replay. The agent is not told that the issue is in how locks interact with the async runtime.

## Instruction Crux

Modify the service such that under the verifier's held-out traffic patterns (each ~10 minutes long, on the verifier's hardware), p99 latency stays below 50 ms throughout the run, no task in `tokio-console` is in `Frozen`/blocked-but-not-progressing state for more than 100 ms, and throughput is at least 95% of a freshly-started baseline run. The service's public API (HTTP endpoints, response shape) and on-disk persistence behavior must be preserved. The agent is free to swap synchronization primitives, restructure ownership, replace data structures, or change the executor configuration.

## Solution Crux

The root cause is that several call paths hold a `std::sync::Mutex` (a blocking mutex) across an `.await` point. When the future is pending, Tokio parks the task on the executor's worker thread — but the worker still owns the lock. Another worker thread (or the same one, scheduling a different task) tries to acquire the same lock and blocks, which on Tokio's multi-threaded executor stalls a whole worker thread and any task scheduled on it. Under steady load the workers progressively block each other in a pseudo-deadlock — not a true deadlock, just a tail of progressively starved tasks — and the queue grows. The compiler does not catch this: `std::sync::Mutex`'s guard is `Send`, so the future remains `Send`, and the program type-checks fine.

The right fix is to either (a) swap `std::sync::Mutex` for `tokio::sync::Mutex` everywhere the lock is held across an await (a `tokio::sync::Mutex` releases the worker when the future is parked), or (b) restructure the code so the standard mutex is only held in non-async, short critical sections and released before any `.await` — this is generally faster and is the idiomatic Tokio recommendation when the critical section is genuinely short. Both options pass the gate; choosing depends on lock-held durations. A complementary issue: one of the caches uses `RwLock` and the holding pattern is write-heavy under stress, producing writer starvation under Tokio's scheduling — the fix is either to switch that cache to a `DashMap` / sharded design or to redesign the access pattern so writes don't fan out under load.

Wrong-but-tempting fixes: (a) increase the Tokio worker thread count — masks the symptom briefly and then degrades worse because more workers means more parked-task contention; (b) wrap the offending code in `spawn_blocking` — moves the lock acquire to a blocking pool but the lock-across-await problem persists and now the blocking pool itself becomes the bottleneck; (c) sprinkle `tokio::task::yield_now()` — does nothing here, the workers are still parked on the lock; (d) swap to `parking_lot::Mutex` — slightly faster acquire but identical semantics under await.

## Difficulty Crux

The bug is invisible to standard tooling: clippy does not flag holding a `std::sync::Mutex` across `.await` in the general case (there is a lint, but it's `clippy::await_holding_lock` and most projects don't turn it on; and even with it on, the violations are spread across many sites and require structural fixes). `tokio-console` *can* surface the symptom but it requires interpretation — what you see is "many tasks idle" not "lock-across-await." The agent has to form the hypothesis that this is a runtime/lock interaction rather than a backpressure or memory issue (both of which produce the same kind of slow-climb p99 graph), then go find every site where a blocking mutex is held across an await — including ones that look fine until you trace the call into a third-party crate that does internal async work.

This is exactly the kind of bug senior Tokio practitioners hunt routinely; it has appeared in many real Rust services and the fix is well-documented to those who've seen it once. To someone who hasn't, the path from symptom to root cause is non-obvious and the wrong-fix paths all look reasonable.

## Verification Note

The verifier runs the service against three held-out traffic patterns for 10 minutes each on dedicated hardware, measures p99 latency throughout, captures `tokio-console` task states, and runs a contention detector that scans the binary for `std::sync::Mutex`/`std::sync::RwLock` guards held across await points (via a static analysis on the resulting Rust code or via runtime instrumentation in the verifier build). Pass conditions: p99 < 50 ms over the full run, no task frozen > 100 ms, throughput within 5% of fresh-start baseline, and the static analysis finds zero await-holding-lock violations. Three patterns; all three must pass. Deterministic given fixed seeds and pinned toolchain. No LLM judge.
