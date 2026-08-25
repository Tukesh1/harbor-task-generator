# Task Proposal: rocksdb-write-stall-l0-compaction

**Domain:** Storage engines / Database systems (SWE)

## Overview

A service backed by an embedded RocksDB instance is failing its write-side p99 SLO. The application's writers commit small key/value pairs at a steady ~80 MB/s and have always done so without backpressure, but after a recent traffic-shape change — same total throughput, but with a long tail of larger values mixed in — every minute or two the writers stall for hundreds of milliseconds at a time. Average throughput is unchanged, but the p99 latency has gone from ~3 ms to ~600 ms. The operator's first response (more memory, more compaction threads, bigger memtable) reduced the symptom slightly but did not fix it, and at some configurations made it worse.

The agent is given the RocksDB configuration in use, a workload generator that reproduces the stalls on a fixed seed, the LOG file from a stalled run, the output of `rocksdb_dump --stats`, and read access to the live DB. It is told the SLO must hold and it has full freedom to retune RocksDB options, change the column-family schema, change the compaction strategy, or restructure how the application writes — but it is not told which knob is responsible.

## Instruction Crux

Produce a configuration + (optional) application-side change such that on the same workload generator and seed, p99 write latency stays under 25 ms over a 10-minute run on the verifier's hardware, total throughput stays within 10% of the original, and no point in the run exceeds the configured stall trigger as logged by RocksDB itself. The application API and the on-disk durability guarantees (`sync=true`, WAL enabled, no relaxed durability) must be preserved.

The deliverable is a `rocksdb.opts`-style options file (consumed via `LoadOptionsFromFile`) plus, if needed, a patched writer module exposing the same public API. Method is not graded: the agent may change compaction style, level multipliers, write-buffer counts, rate limiter, partitioning, or how the application batches — anything that holds the SLO across the held-out workload seeds the verifier draws.

## Solution Crux

The stall is caused by L0 → L1 write stalls, not by memtable pressure or by the disk. The mixed-value-size traffic causes the memtable to flush at irregular intervals: small batches produce many small L0 files quickly, while a single large value can trigger a flush at low fill. The result is bursts of L0 file creation that occasionally push the L0 file count past `level0_slowdown_writes_trigger` (default 20) and momentarily past `level0_stop_writes_trigger` (default 36), at which point RocksDB blocks all writers until compaction catches up.

The right fix has two interacting parts. First, decouple L0 → L1 compaction throughput from the bursty flush rate: raise `max_background_compactions` enough to drain L0 (but not so high that compaction itself becomes the bottleneck), and crucially set `level0_file_num_compaction_trigger` lower so compaction starts before stalls trigger. Second, smooth the flush bursts at their source — either via `min_write_buffer_number_to_merge` >= 2 (so multiple memtables merge into one larger L0 file rather than several small ones), or by enabling subcompactions so a single L0→L1 task parallelizes across the key range. The wrong-looking-right fixes are: (a) just raising the slowdown/stop triggers, which masks the symptom but lets L0 grow so large that read amplification destroys query latency; (b) increasing the memtable size, which delays the stall but makes each stall longer when it does fire; and (c) switching to universal compaction, which removes L0-stop but trades write stalls for occasional huge read-amplification spikes that fail the same SLO from the query side.

## Difficulty Crux

The hard part is reading the LOG and the stats dump correctly. The symptoms look like classic "compaction can't keep up" — pending compaction bytes are high, disk is busy, threads are saturated — and the obvious-and-wrong response is to give compaction more resources. But the actual chokepoint is structural: L0 files are produced faster than any single-threaded L0→L1 job can consume them, and giving compaction more threads doesn't help because L0 compactions in leveled mode are serialized per-CF unless subcompactions are explicitly enabled. The agent has to triage between memtable pressure, L0 pressure, pending-compaction-bytes pressure, and disk IO pressure — all of which RocksDB surfaces in overlapping ways — and then realize that the right fix changes the *shape* of L0, not the rate at which it's drained. This is the kind of tuning that storage engineers at companies running RocksDB at scale spend weeks getting right; the wrong knob is usually tried first because it looks right in the LOG.

The task is not hard because of volume (the config file is short), formatting (it's a flat key=value file), or sheer steps. It is hard because the diagnostic surface is dense and several reasonable hypotheses misdirect.

## Verification Note

The verifier runs the agent's options file + writer against a frozen workload generator (fixed seed, fixed value-size distribution, fixed total bytes) for 10 minutes on dedicated hardware. It records per-write latency from the application side, computes p99 over the full run, and asserts < 25 ms. It also parses the RocksDB LOG to assert zero entries containing `Stalling writes` or `Stopping writes`. It asserts total throughput is within 10% of a reference baseline. Three different held-out workload seeds are used; all three must pass. Deterministic given fixed seeds and pinned RocksDB version. No LLM judge.
