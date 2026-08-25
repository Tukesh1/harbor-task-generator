# Task Proposal: prometheus-cardinality-explosion

**Domain:** Observability / Site reliability engineering

## Overview

A Prometheus-based metrics stack is in a degraded state across two related symptoms. The Prometheus server's memory usage has climbed from a steady ~6 GB to over 60 GB and is OOM-killed every few hours. Independently, several dashboards backed by recording rules have started returning empty data for some queries while others return correct data, with no clear pattern by metric or by team. A recent deploy introduced two new services into the scrape pool, but the operators cannot point at a single offending metric — the Prometheus instance scrapes ~3,000 targets across ~40 teams, and the new services are deeply embedded in the platform.

The agent is given (a) read access to the live Prometheus instance via the HTTP API, (b) the `prometheus.yml` scrape config + the active recording rules + the active alerting rules, (c) the service-discovery output, (d) a dump of `pprof` heap + goroutine profiles from the degraded instance, (e) a 24-hour TSDB block from when the symptom started, and (f) a held-out set of dashboard queries that the verifier will use to score the fix.

## Instruction Crux

Produce a set of changes (to scrape config, relabel rules, recording rules, and/or service instrumentation) such that, when the Prometheus instance is restarted against the verifier's reproducer (replaying the same scrape traffic shape and same query workload for 30 minutes), (a) Prometheus memory stays under 8 GB throughout, (b) the held-out dashboard query set returns the same set of result series and values as a held-out reference run (within a small floating-point tolerance), (c) the active-series count stays under the operator-set ceiling of 5M, and (d) no scrape target is dropped — every target the original config intended to scrape still has at least one sample in the final TSDB block.

The agent is free to add `metric_relabel_configs`, change recording-rule definitions, change query patterns, or push patches to the service instrumentation. No method is prescribed.

## Solution Crux

There are two coupled bugs.

The first is a cardinality explosion: one of the newly-deployed services emits a histogram label `endpoint` derived from the raw HTTP request URL — including the path parameters (`/users/12345/orders/9876`). Each unique URL contributes a unique label-value pair, and across ~50M requests/day this produces millions of distinct series, none of which are useful (the team intended `endpoint="/users/:id/orders/:id"`). The right fix is a `metric_relabel_configs` rule at scrape time that drops the offending series for that job, paired with an instrumentation patch to use the route template rather than the raw URL going forward. Just dropping at scrape time leaves the underlying instrumentation broken (the rule will hit the same explosion the next time a new endpoint shows up); just patching instrumentation leaves the existing high-cardinality series in TSDB which Prometheus will continue to memory-map. Both are required.

The second is a recording-rule misordering: a rule group `aggregations` depends on metrics that are themselves produced by a rule group `rates`, but the two groups evaluate independently with no ordering guarantee, and after a recent rule-file split the `aggregations` group occasionally evaluates with stale or missing `rates` results — producing the intermittent empty queries on dashboards. The right fix is to either (a) merge the two groups into a single rule group (within a group, rules evaluate sequentially in declared order), or (b) add a recording rule chain that does not cross group boundaries. Wrong-but-tempting fix: cranking up `evaluation_interval` — does not address the dependency ordering.

Wrong-but-tempting alternative fixes for the cardinality side: (a) raising the Prometheus memory limit — kicks the can; (b) enabling chunked storage block compaction — does not affect active series; (c) blanket-dropping the entire offending metric name — drops legitimate observability for the service; (d) sharding Prometheus — solves the immediate memory pressure but adds operational complexity and doesn't address the underlying bug. Wrong-but-tempting fixes for the dashboard side: (a) adding `default()` to queries to mask the empties — hides the bug at query time but the dashboards' aggregations still depend on stale data.

## Difficulty Crux

The difficulty is locating the explosion. With ~3000 targets, ~40 teams, and thousands of metric series, "which label is causing the cardinality" is not visible from any single page of Prometheus's UI. The agent has to know how to find it: `topk` on `count by (__name__, job) ({__name__=~".+"})`, or examine `prometheus_tsdb_symbol_table_size_bytes`, or look at heap profiles for the per-symbol allocations, or query the metadata endpoint for label-value cardinality per series. Once located, the dual fix (scrape-time drop + upstream instrumentation patch) is required to actually hold under future deploys; either alone passes a one-shot check but fails the gate's intent.

The recording-rule bug is even more subtle: it presents as "intermittent empty dashboards" with no error in any log. The agent must form the hypothesis that rule groups evaluate independently and that cross-group dependencies are not safe, which is documented Prometheus behavior but is exactly the kind of footgun that bites teams a year into operating Prometheus when they split rule files for tidiness.

The task is not hard from volume or formatting. It is hard because the diagnostic surface (3000 targets, 40 teams, two coupled symptoms) is realistic SRE-scale and the fixes span configuration, recording-rule structure, and upstream instrumentation.

## Verification Note

The verifier (a) restarts a Prometheus instance with the agent's modified config + rule files, (b) replays a pinned 30-minute scrape traffic shape and query workload (same targets, same series, same scrape interval), (c) samples memory and active-series count over the run, (d) runs the held-out dashboard query set against the instance and compares to a frozen reference, (e) checks all original scrape targets have samples in the final TSDB block. All four gates must pass. The reproducer is deterministic given pinned synthetic-service simulators. No LLM judge.
