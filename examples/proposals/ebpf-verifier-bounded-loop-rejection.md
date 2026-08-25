# Task Proposal: ebpf-verifier-bounded-loop-rejection

**Domain:** Kernel observability / eBPF (Linux systems)

## Overview

A production eBPF program — an XDP/tc-style packet classifier used as part of an observability sidecar — fails to load on the production kernel (6.1 LTS) with `BPF program too large` plus a verifier rejection citing "infinite loop" and "register R3 unbounded" further in the verifier log. The same program loads fine on the developer's laptop kernel (6.6) and against a public BPF Type Format dump from a 6.6 build. The program is moderate-sized (~600 lines of restricted C compiled to BPF) and uses helpers, tail calls, a per-CPU map, and a fixed-iteration loop over an in-packet TLV-style header.

The agent is given the C source, the compiled object, the verifier log (~3,000 lines, much of it state explosion), the kernel config of the production kernel, a known-good baseline program that does roughly half what the broken one does and loads cleanly, and a held-out set of pcap traces the verifier will use to score the fix.

## Instruction Crux

Produce a modified eBPF program (same source language and same compiled object format) that (a) loads successfully on the production kernel without bypassing the verifier, (b) passes a functional test where the verifier replays held-out pcap traces and asserts the program produces the same per-packet classification decisions as a reference Python implementation, and (c) runs within a fixed per-packet ns budget on the verifier's hardware (measured via `bpftool prog profile`). The program may not be split arbitrarily into many tail calls (the verifier enforces ≤ 4 program objects in the tail-call array, matching the operational constraint of not bloating the BPF object catalog).

The agent is free to restructure the source: rewrite loops as bounded `#pragma unroll`'d sequences, swap helpers, change the per-CPU map's key/value layout, lift work into the user-space loader, or convert hot paths to BPF_LOOP / bpf_for_each helpers where supported.

## Solution Crux

There are three interacting problems and the program needs all three addressed.

First, the loop over TLV headers uses a counter bounded by a value read from packet data. On kernel 6.6 the verifier proves the bound via range analysis on the read. On 6.1 the verifier is less aggressive about cross-block range tracking — it loses the bound when the loop body has an early-continue path — and rejects the program as potentially infinite. The fix is to clamp the counter at the top of every iteration with an explicit `if (i >= MAX_TLVS) break;` (the verifier sees this as a hard upper bound regardless of where the original bound came from), and to assert the loop body's branches keep the bound visible by reading `i` from a stack slot rather than letting the compiler reorder.

Second, the program calls a helper that returns a `u32` size used later as an index into a per-CPU map. On 6.1 the verifier's value tracking for that helper is conservative and produces an `unbounded` range, so the index is rejected. The fix is to clamp the value (`val &= 0xff;`) at the call site before any use, which gives the verifier a tight range it can carry through subsequent instructions.

Third, the program is genuinely close to the verifier's instruction-budget ceiling (1M insns on 6.1, raised to 4M on 6.6), and the state explosion the verifier logs is real. The fix is to restructure the hot path so the compiler emits fewer redundant paths — concretely, by hoisting a constant-pointer arithmetic expression out of a switch arm, by removing a redundant `__builtin_memcpy` that LLVM was lowering as a long inline sequence, and by reducing the function-inlining depth via `__attribute__((noinline))` on the cold paths. With all three changes, the program loads and stays well under the budget.

Wrong-but-tempting fixes: (a) splitting the program into many tail calls — exceeds the operational tail-call limit; (b) compiling with `-O0` — produces an even larger, more-state-explosion program; (c) replacing the bounded loop with a CO-RE `bpf_for_each_map_elem` — doesn't apply to packet TLVs; (d) marking the program `unprivileged` to skip parts of the verifier — disabled in the production kernel; (e) using `bpf_loop()` — only available on 5.17+ and the production kernel uses it, but the agent has to wire it up correctly with the right callback signature, and the rest of the program still has to satisfy the verifier.

## Difficulty Crux

The difficulty is interpreting the verifier log and reasoning about what range analysis the verifier *actually* does on a specific kernel version. The verifier's behavior is a moving target — what's accepted on 6.6 is rejected on 6.1 because the range-tracking improvements that landed in between are absent. The error messages are local but the cause is global: "register R3 unbounded" is mechanically true but the actual fix may be hundreds of lines away. The agent has to know how the verifier tracks bounds, where it loses precision, and how to write source that holds the verifier's hand without sacrificing correctness or performance. This is graduate-level systems work; eBPF practitioners at network and observability teams routinely deal with it but the path from "won't load" to "load + perf + correctness" is a multi-part puzzle.

The task is not hard from volume — the program is ~600 lines. It is hard because the diagnostic surface is the verifier log, which is famously hostile, and because the fix requires understanding the production kernel's verifier semantics specifically — not eBPF in the abstract.

## Verification Note

The verifier (a) attempts to load the agent's compiled program on a pinned 6.1 kernel via `bpftool prog load` and asserts it loads, (b) attaches it to a test interface, replays held-out pcap traces, captures per-packet classification decisions, and compares to a reference Python implementation, (c) runs `bpftool prog profile` and asserts mean per-packet ns under a budget (set with margin from the oracle solution's measurements), (d) asserts the loaded program object count is ≤ 4. All four gates must pass. Deterministic given pinned kernel, pinned pcaps, and pinned reference. No LLM judge.
