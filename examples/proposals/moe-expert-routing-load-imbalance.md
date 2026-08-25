# Task Proposal: moe-expert-routing-load-imbalance

**Domain:** ML training infrastructure / Mixture-of-Experts (MLOps)

## Overview

A small Mixture-of-Experts (MoE) transformer with 8 experts per layer and a top-2 gating router is given to the agent in a broken state. The model has been training at nominally healthy loss for ~12k steps, but on inspection two failure modes are observable. First, expert utilization is grossly imbalanced: per-layer logs show that two experts in each layer absorb ~70% of the tokens while two others receive <2%. Second, the model significantly underperforms a pinned FLOP-matched dense baseline checkpoint that was trained on the same data and seed for the same number of steps — the dropout-in-experts behavior, where some experts effectively never get trained, has cratered the model's effective capacity.

The agent has the full training script, the model code, the broken-state optimizer + model checkpoint at step 12k, the training data shards, the loss curves, the pinned dense baseline checkpoint, and a small held-out evaluation slice. The training environment is configured for deterministic operation: CUDA deterministic mode is on, the dispatch kernel uses a deterministic sort, dataloader seeds are fixed, and the verifier uses a fixed RNG seed for any tie-breaking. The agent is told the goal is to fix the training pipeline such that a fresh run from initialization on a fixed compute budget produces a balanced, performant MoE.

## Instruction Crux

Modify the training pipeline such that, on a fresh run from a fixed initialization seed and fixed compute budget (same dataset, same steps, same hardware, deterministic mode on), the resulting model satisfies on the verifier's held-out eval slice: (a) per-layer expert load coefficient of variation (`std/mean` of tokens-per-expert) below 0.2 for every MoE layer, (b) eval perplexity strictly lower than the pinned dense baseline checkpoint's perplexity (no required margin — just lower), and (c) no expert in any layer receives less than 1% of total routed tokens. The verifier averages metrics over three independent seeds (provided by the verifier, not the agent) and requires the average to clear each gate. Wall-clock training time per seed must remain within 25% of the broken run's wall-clock on the verifier's hardware (the budget is set with slack for the standard fp32-router fix).

The agent is free to change the router, the auxiliary loss, the gating function, the initialization, the optimizer, the routing precision, or anything else. The model's parameter count and per-token FLOPs must stay within 2% of the original. No method is prescribed; the deliverable is a modified training script.

## Solution Crux

There are three coupled root causes, and any one of them alone does not fix the imbalance.

First, the router uses a top-k softmax gate without a load-balancing auxiliary loss. In MoE training this is known to collapse to a small number of "winning" experts: tokens routed to a given expert make that expert better, which makes the router prefer it more — a positive feedback loop. The fix is a Switch-Transformer-style auxiliary loss: a load-balancing term `α · N · Σ f_i · P_i` where `f_i` is the fraction of tokens dispatched to expert `i` and `P_i` is the mean router probability for that expert. The aux loss must be tuned to a non-trivial coefficient (literature: ~0.01); too small leaves the imbalance, too large suppresses specialization and hurts perplexity.

Second, the router computes its softmax in fp16 (matching the activation dtype), which under top-2 routing produces tiny numerical differences between near-tied experts that magnify across steps. The fix is to compute the router logits and softmax in fp32 even when the rest of the model is mixed-precision; this is standard practice in production MoE codebases (Mixtral, DeepSeekMoE) for exactly this reason.

Third, when an expert is mostly underutilized for many consecutive batches its router logits drift to extreme values, making the routing decision sticky regardless of new gradient signal — the fix is either (a) using a router-z-loss (`α_z · log(Σ exp(logits))²`) to keep the logits bounded, or (b) using a noisy / Gumbel top-k gate during the early training phase to force exploration. Without one of these, even with the aux loss, the load CoV stabilizes around 0.3 and at least one expert in some layer typically falls under the 1% floor.

Common wrong fixes: just tuning learning rate or batch size; raising the aux-loss coefficient to large values (it suppresses specialization and hurts perplexity); switching to top-1 routing (improves load balance but hurts perplexity). Production MoE recipes (Mixtral, DeepSeekMoE, GLaM) all use the combination above; the recipe is documented but the diagnosis from "model trained, eval bad" to "router needs three coupled fixes" is the hard part. The task author has reproduced the oracle solution clearing the three gates by a wide margin across five seeds: median load CoV ~0.08, perplexity ~9% below the dense baseline, minimum-expert-fraction ~6%. The thresholds (0.2 CoV, "any improvement" perplexity, 1% minimum) are deliberately set with substantial slack against the oracle's measured spread to make the gates robust to remaining nondeterminism.

## Difficulty Crux

The difficulty is that the symptoms — bad eval, lopsided expert utilization — admit many surface-level explanations: bad data, bad LR, wrong tokenization, wrong attention mask, bug in the dispatch layer. The agent has to recognize this is a routing-collapse pattern (not a data or hyperparameter problem) and then assemble three independent fixes that interact: any one alone is a partial fix that doesn't meet the joint gate. The router-precision fix in particular is non-obvious — it looks like a numerical curiosity until you've debugged it. The whole package is the standard MoE pretraining recipe, well-known to a small community (a few dozen production MoE teams) but not standard ML knowledge.

The task is hard not only because the diagnostic chain is non-trivial, but because partial fixes look like they're working. An agent that applies just the aux loss will see the CoV drop from extreme imbalance to ~0.3, perplexity improve a little, and incorrectly conclude it has solved the task — only to fail the minimum-expert-fraction gate or the joint perplexity gate. The "looks-like-it's-working" trap is what separates this from a task that can be solved by a single recipe lookup.

The task is not hard from volume (the diff is dozens of lines) or formatting; it is hard because the joint constraint requires the full recipe, not any one component.

## Verification Note

The verifier launches a fresh training run from the agent's modified script on three independent fixed seeds, runs each to completion on the same compute budget under CUDA deterministic mode, then evaluates each resulting checkpoint on (a) a held-out perplexity slice (frozen, distinct from any training shard), (b) per-layer expert utilization computed over the eval set, and (c) per-seed wall-clock. It asserts the seed-averaged perplexity is below the pinned dense baseline's perplexity, the seed-averaged per-layer load CoV is < 0.2 for every layer, the seed-averaged minimum-expert-fraction is > 1% in every layer, and each seed's wall-clock is within 25% of the broken-run baseline. Three-seed averaging absorbs the residual MoE-dispatch nondeterminism that deterministic mode does not fully suppress. The verifier re-runs the training each time, so the gate is not gameable by editing a checkpoint. The thresholds were calibrated against the oracle solution's five-seed measurements to leave large margin. No LLM judge.
