# Task Proposal: postgres-correlated-predicate-regression

**Domain:** Database systems / Data engineering (SWE)

## Overview

A production PostgreSQL 16 database is given to the agent. The database contains the schema and data of a real-shaped order-processing workload: an `orders` table (~5M rows), a `line_items` table (~30M rows), and a few dimension tables (`customers`, `products`, `warehouses`). One reporting query — a join between `orders` and `line_items` filtered by both a date range and a regional grouping — used to run in ~80 ms after the last release and now consistently takes ≥45 seconds across reruns. Nothing in the SQL has changed, no rows have been deleted, and the most recent maintenance window left the tables "looking healthy" from a cursory check.

The agent has full superuser access to the database and a wrapped CLI that runs the offending query with `EXPLAIN (ANALYZE, BUFFERS)`. It does _not_ have shell access to the data-generation pipeline, and the truth of "what plan should be chosen" is not stated. The verifier draws test parameters from a continuous (date_range, region_id) space, so memoized / hard-coded result tables enumerating only the development parameters will not pass.

## Instruction Crux

Restore the failing query to a state where it returns the same result set and completes in under 250 ms wall-clock on at least 5 consecutive runs in a fresh session. The schema, the indexes, the data, and the SQL text itself are all things the agent may change; whatever it does, the application — which only knows about a small interface table called `reporting_v1` — must still see results identical to a frozen reference run. The agent is not told whether a planner change, a schema change, a query rewrite, or some combination is required, and is not told which join or filter is the culprit.

The end state, then, is a database whose plan for the reporting query no longer touches the wide hash join over the date-region filter, and whose `reporting_v1` view (or table) returns row-for-row the same rows in any order as the reference.

## Solution Crux

Two interacting issues caused the regression and both must be addressed for the target latency to hold across all held-out parameters.

The first is statistical. The query filters `orders` on both `order_date >= $1` and `region_id = $2`. These two columns are strongly correlated — the data load was staggered by region — so the joint selectivity of the two predicates is much smaller than the product of their individual selectivities, which is what the planner assumes by default. Under that bad estimate, PostgreSQL builds an enormous hash from `line_items` and joins against what it expects to be a large `orders` result, when in fact a parameterized index scan + nested-loop would be near-instant. The non-obvious move is to install **extended statistics** (`CREATE STATISTICS ... (dependencies, mcv) ON (order_date, region_id) FROM orders`) and re-`ANALYZE`. Naive substitutes — running `ANALYZE` again, dropping and recreating individual single-column indexes, adding a composite index, increasing `default_statistics_target`, raising `work_mem`, or pinning the plan with `pg_hint_plan` — either don't help or only mask the regression on a single parameter combination and re-introduce it under a slightly different region.

The second issue is physical. A `products` dimension table has been heavily updated in place by a backfill job, leaving roughly 40% dead tuples; autovacuum was throttled to a near-stop during the maintenance window and never caught up. The visibility map is stale, so even after the planner picks the right join order, the index-only scan on `products` falls back to heap fetches and dominates wall-clock. `VACUUM (ANALYZE)` (or rebuilding the table) restores the visibility map and gets the join down to its proper cost. An agent that fixes only the statistics will see the plan shape change but still miss the ≤250 ms target; an agent that only vacuums won't help at all because the planner still picks the wrong join. The diagnostic chain is therefore: bad row estimate (planner side) plus bloated heap (physical side), neither obvious from a single look at `EXPLAIN`.

## Difficulty Crux

The difficulty is in correctly partitioning blame between the planner and the storage layer. `EXPLAIN ANALYZE` shows a bad join and a row-count misestimate (e.g., "estimated 4M, actual 9K"), but the misestimate is not labeled "correlation bug" — it looks like a stale-stats problem, which is the obvious-and-wrong diagnosis. Meanwhile the `products` heap bloat does not look like the bottleneck on a first read of the plan, because once the row estimate is wrong the join order obscures it. The agent has to recognize _two_ independent root causes, neither of which is announced by the diagnostic surface, and apply two qualitatively different fixes — a planner-side feature (extended statistics) and a physical-side fix (vacuum / visibility-map restoration). Either fix alone changes the plan enough to look promising while still missing the latency bar.

This is hard the way real on-call DB tuning is hard: it's an unexpected system state, the diagnostic surface is dense and ambiguous, and the resolution requires composing knowledge across the planner and the storage engine. It is _not_ hard because of corner-case enumeration, formatting, or sheer volume.

## Verification Note

The verifier (a) runs the agent's `reporting_v1` view against a frozen reference table of expected rows for three held-out (`date_range`, `region_id`) parameter pairs and checks set equality, and (b) runs each parameterized query five times in a fresh connection on a warm cache and asserts each run completes in under 250 ms. No EXPLAIN text is parsed and no implementation choice is checked — the agent is free to use extended statistics, materialized views, query rewriting, or any other mechanism. Determinism comes from a fixed seed in the data load and a fixed checkpoint of the reference rows. Re-running the verifier against either the bad pre-fix DB or a correct fixed DB gives the same verdict every time.
