# Task Proposal: nccl-allreduce-pipeline-deadlock

**Domain:** Distributed ML training / GPU collective communication (MLOps)

## Overview

A multi-GPU training job for a large transformer hangs partway into training, between several minutes and an hour after the first step. The training job uses 8 GPUs split as 2-way tensor parallel × 4-way pipeline parallel and started shipping recently after a switch from pure data parallelism. The hang is silent: no exception, no NCCL error, no Python traceback. `nvidia-smi` shows all 8 GPUs at 100% utilization, indefinitely. `py-spy dump` shows each rank parked inside a NCCL collective on a CUDA stream. Restarting the job and re-running on the same data + seed reproduces the hang at a different but reachable step.

The agent has the training source (model + pipeline-parallel schedule + data loader + optimizer), the NCCL debug logs (`NCCL_DEBUG=INFO`), the `py-spy` dumps from each rank, the `nvprof`/`Nsight Systems` trace from the last clean step before the hang, and a small reproducer that triggers a hang within ~10 minutes on the verifier's hardware. The agent is not told what's causing the deadlock.

## Instruction Crux

Modify the training pipeline such that, on the verifier's reproducer + two additional held-out configurations (different micro-batch sizes, different pipeline schedules), the job runs to completion for a fixed number of steps without hanging, produces a final loss within 1% of a reference run for that step count, and steady-state throughput is at least 90% of the reference throughput. The model parameters, the data, and the parallelism degrees are fixed. The agent is free to change the pipeline schedule, the communication primitives, the order of operations, or anything else, so long as the model and the data are unchanged.

## Solution Crux

The hang is a NCCL collective ordering violation introduced by the pipeline-parallel schedule. NCCL collectives are tied to CUDA streams, and the order in which collectives are *posted* across ranks must match across ranks for a given communicator — i.e., if rank 0 posts `allreduce(A); allreduce(B)`, rank 1 must post the same two collectives in the same order on the same communicator. The new pipeline-parallel schedule has each rank issue `allreduce` for its tensor-parallel gradient sync interleaved with `send/recv` for pipeline activation/gradient passes, and the interleaving differs across ranks because each rank only sees its own slice of the pipeline. Under the 1F1B schedule with bubble compression, the rank doing the warmup phase issues collectives in an order that, while logically correct, doesn't match across the TP group — and NCCL deadlocks when collective N on rank 0 matches collective M ≠ N on rank 1.

The right fix has two parts. First, use separate NCCL communicators for tensor-parallel collectives and pipeline-parallel collectives. NCCL guarantees ordering only within a single communicator, so isolating the two semantic categories breaks the ordering coupling. Second, ensure that collectives within the TP communicator are issued in the same order across TP ranks — this requires forcing a single collective-issue path through the code (the original code had two paths: one for the forward-only phase, one for the backward phase, and the order they fired collectives differed). Equivalent passing solution: use NCCL's `groupStart()`/`groupEnd()` to atomically post a batch of collectives, which serializes intra-batch ordering by construction.

Wrong-but-tempting fixes: (a) adding `cuda.synchronize()` between collectives — masks the deadlock briefly under some schedules and is a real perf killer (kills the 90% throughput gate); (b) increasing `NCCL_BUFFSIZE` — does nothing for an ordering bug, is the standard kneejerk fix for NCCL hangs; (c) switching to gloo for one of the collectives — works but devastates throughput; (d) running everything on a single communicator and serializing via a global mutex — works in lockstep but blows throughput by ~3×.

## Difficulty Crux

The bug is invisible: there is no error, no log, and the symptom (GPUs at 100%, collectives parked) is identical to many other failure modes (network issues, hardware faults, slow ranks, slow PCI traffic). The agent has to recognize that this is a NCCL collective-ordering deadlock and not a network or hardware issue, which requires understanding NCCL's ordering guarantees, the semantics of communicators, and how the pipeline-parallel schedule interleaves with tensor-parallel collectives. Then they have to design a fix that maintains throughput — naive serialization is a passing fix for correctness but fails the perf gate. The fix is documented (Megatron-LM, DeepSpeed, NeMo all do something similar), but the path from "training hangs occasionally" to "TP and PP need separate communicators with deterministic ordering" is the work.

This is exactly the kind of bug ML infrastructure teams at frontier labs hunt every quarter; the deeper distributed-training literature describes the pattern but the diagnostic chain from symptoms to root cause is non-obvious to anyone who hasn't seen it before.

The task is not hard from volume — the diff is dozens, not thousands, of lines. It is hard because the diagnostic surface looks like several unrelated failure modes and the constraint includes maintaining throughput, which rules out naive correctness-only fixes.

## Verification Note

The verifier runs the agent's modified training on the reproducer + two held-out configurations (varying micro-batch size and pipeline schedule), each on 8 GPUs of a pinned SKU. It asserts: (a) the job runs to completion without hanging within a fixed wall-clock budget, (b) final loss within 1% of a reference run for the same step count + seed, (c) steady-state throughput (tokens/sec) ≥ 90% of reference. All three configurations must pass. Determinism comes from fixed seeds, fixed data shards, and pinned NCCL + CUDA versions. No LLM judge.
