# Task Proposal: fused-attention-shape-regression

**Domain:** GPU performance / Kernel engineering (MLOps)

## Overview

A single-GPU inference service runs a transformer block whose fused-attention CUDA/Triton kernel was tuned for head dimensions that are powers of two (64, 128). After a model architecture change, the deployed model now uses a head dimension of 96 with sequence length 1153 (a prime, set deliberately by the modeling team to avoid an unrelated padding artifact), and on that combination the kernel achieves only ~25% of the throughput it gets on the old shape — far worse than the small change should justify. Standard publicly-available attention implementations also underperform on this shape: FlashAttention-2's default split-K heuristic regresses on prime sequence lengths with non-power-of-two head dimensions, so wholesale-porting it is not a free pass to the perf target.

The agent has the source of the existing kernel (Triton or CUDA), an Nsight Compute / Nsight Systems profile, a benchmark harness that measures TFLOPS and verifies numerical correctness against an unfused PyTorch reference, and a held-out set of shapes (batch sizes, sequence lengths including primes and small-prime products, head dims including 80, 96, and 112) that the verifier will score on. The agent is not told what the bottleneck is, and is told the GPU SKU and the pinned versions of the reference implementations the verifier will compare against.

## Instruction Crux

Produce an attention kernel such that, on the verifier's held-out shapes (each including at least one non-power-of-two head dimension and at least one prime sequence length), the achieved TFLOPS is at least 88% of the *best* of cuBLAS-backed reference and pinned FlashAttention-2 per shape, AND the kernel's output matches an FP32 reference within an FP16-appropriate tolerance (max abs error and max rel error bounds the verifier supplies). The end state is a single kernel entry point at a fixed Python path the verifier imports.

The agent is free to rewrite the kernel in Triton, CUDA, or CUTLASS; to change tile shapes, vector widths, swizzling, masking strategy, or how the kernel handles tails; or to swap in a different algorithmic approach to attention (e.g., FlashAttention-style tiling, online softmax, split-K) — including wholesale porting of a public implementation, though this alone is known not to meet the perf bar on the held-out shapes. The goal is performance + correctness on the held-out shapes; no method is prescribed.

## Solution Crux

The original kernel was tuned with a tile that packs cleanly into 128-byte transactions when head dim is a power of two. On head dim 96, the same tile produces a leading dimension that no longer aligns to the L1 transaction width: every load misses by a few bytes and the kernel ends up doing roughly 1.33× as many memory transactions per element as it should. The bottleneck reads as "shared-memory bank conflicts" or "low DRAM throughput" in the profile, but neither label captures the root cause, which is that the tile shape and the head dim are coprime in an unfortunate way.

The non-obvious correct moves are some combination of: pad K and V in shared memory so the in-tile leading dim is a multiple of the vector width (changing the on-chip layout, not the in-memory layout); choose a different BLOCK_K (e.g., 16 or 32 along head dim) that re-aligns the transaction; or restructure the kernel to load 96 → padded-128 in shared and consume the padded copy. A typical wrong fix is to switch to a smaller tile entirely, which "improves" the profiler's bank-conflict number but cuts arithmetic intensity and lowers TFLOPS further. Another wrong fix is to wrap the head dim with a pad-mask-unpad in Python before the kernel, which moves the cost off the kernel but doesn't satisfy the throughput gate on the supplied harness.

## Difficulty Crux

The reason this is hard is the gap between what the profiler tells you and what the fix actually is. The profiler will surface several plausible-looking secondary symptoms (low warp occupancy, shared-mem bank conflicts, suboptimal cache hit rate), and most of those can be locally improved without fixing the root cause. The agent must reason about how tile shape interacts with memory transaction width on the target SM, why head dim 96 specifically breaks an assumption that head dim 128 did not, and how to restructure the on-chip layout without sacrificing the algorithmic intensity that makes fused attention fast in the first place. This is exactly the kind of work that gets paid for at ML infra teams — and it generally separates engineers who can write a kernel from engineers who can make one fast on an arbitrary shape.

The task is not hard from volume (a kernel is on the order of a few hundred lines), formatting (the entry point is a single function with a fixed signature), or environment friction. It is hard because the right hypothesis about *what's slow and why* is non-obvious from any single metric.

## Verification Note

The verifier (a) imports the agent's kernel at a fixed Python path, (b) runs it on a held-out set of (batch, seq_len, head_dim, num_heads) shapes — including head dims 80, 96, and 112 and at least one prime sequence length per shape — and asserts numerical correctness vs an FP32 reference within standard FP16 attention tolerance, and (c) measures wall-clock TFLOPS via CUDA events with 50 warmup iterations and 21 timed iterations, takes the median, and asserts ≥ 88% of the better of cuBLAS-backed reference and pinned FlashAttention-2 per shape. The GPU SKU (H100 PCIe 80GB), the CUDA toolkit version, and the reference implementations are pinned in the verifier environment, and the 88% threshold is set with measured noise floor on the verifier hardware (typical run-to-run median spread < 2%). Deterministic given the harness seed, the held-out shape list, and the pinned environment. No LLM judge.
