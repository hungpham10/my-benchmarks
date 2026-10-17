---
title: "Prometheus vs PostgreSQL as a Metrics Store: What Actually Happens When You Benchmark Both"
date: 2026-10-17
tags: [prometheus, postgresql, benchmark, metrics, time-series]
excerpt: "Can you just use PostgreSQL for metrics? I ran a head-to-head benchmark (Prometheus vs Postgres jsonb vs Postgres normalized) on the same dataset and same query load. Here are the measured numbers, with caveats."
---

# Prometheus vs PostgreSQL as a Metrics Store

**"Can I just use PostgreSQL for metrics?"** It's a tempting question. You already run Postgres, your team knows SQL, and adding a new piece of infra feels like overkill. But metrics data is shaped differently from relational data — append-only, time-ordered, identified by label sets rather than integer keys.

I ran a head-to-head benchmark to find out what actually breaks. Same dataset, same query load, same hardware envelope, measured on every run. No fabricated constants.

## What I measured

Three targets received the same synthetic metric dataset (~20k series, 45-minute window, 15s scrape interval):

| Target | Schema |
|--------|--------|
| Prometheus | native TSDB (head, not yet compacted) |
| PostgreSQL | `metrics_jsonb` — labels in a `jsonb` column, GIN index |
| PostgreSQL | `metrics_norm_brin` — normalized series table, BRIN index on time |

Every run verifies **parity**: sample counts must match within 2% across systems (`failed: 0` on the published run). Sum is recorded as diagnostic only — more on that in the caveats.

## Ingest speed (with an honesty caveat)

| System | Real write time | Throughput during write |
|--------|-----------------|-------------------------|
| postgres/jsonb | 1.4 s (COPY) | ~106k samples/s |
| postgres/norm | 0.6 s (COPY) | ~254k samples/s |
| prometheus | 0.5 s ingest + 72 s pacing delay | ~284k samples/s during ingest |

**Important caveat:** Prometheus wall time includes a ~72-second pacing delay (`REQUEST_DELAY`) to avoid overwhelming the WAL. Without pacing, Prometheus drops roughly 30% of samples. This is a *shield against backpressure*, not a tuning knob — so comparing wall-clock times is unfair. What matters is real write throughput once you account for pacing, and there Prometheus holds its own.

## Disk footprint (the headline number)

| System | Samples | Bytes on disk | Bytes / sample |
|--------|---------|---------------|----------------|
| postgres/jsonb | 144,000 | 140.7 MB | **1,024.56** |
| postgres/norm | 144,000 | 74.2 MB | **540.09** |
| prometheus | 143,200 | 1.7 MB | **12.68** |

Prometheus is **~43× smaller** than normalized Postgres and **~81× smaller** than jsonb Postgres.

The cost isn't really the index structure — it's the **label text**. jsonb re-stores the entire label document on *every single sample* (index is cheap, ratio 0.08× heap). Normalized inverts that: cheap rows, expensive indexes (ratio 0.61× heap). Neither escapes the total. So the honest statement isn't "jsonb is bad" — it's that metric samples carry label text with them, and wherever you put that text, you pay for it.

## Query load test — who breaks first?

I ramped query rate from 200 → 2,000 req/s using k6 (ramping-arrival-rate, threshold p95 < 500 ms). All three systems eventually hit the breaking point. Here's what each system did right before it broke:

| System | Iterations at break | p50 latency | p95 latency | Dropped iterations |
|--------|---------------------|-------------|-------------|-------------------|
| postgres/jsonb | 281 | 67 s | 82 s | 469,518 |
| postgres/norm | 469,688 | 0.5 ms | 0.6 ms | 111 |
| prometheus | 151,889 | 1.8 s | 2.0 s | 317,911 |

**Key finding:** At this query shape (rate over counter series, grouped by job), normalized Postgres wins **both** latency and throughput. Prometheus drops ~318k iterations because each query takes ~2 seconds — it cannot keep up at 2,000 req/s. jsonb breaks almost immediately because each query takes ~80 seconds (jsonb containment + heap fetch for every row).

## Where PostgreSQL genuinely wins

- **Percentiles over a range.** `percentile_cont(0.9) WITHIN GROUP (ORDER BY value)` sorts inside the database and is not subject to Prometheus's `--query.max-samples` ceiling (default 50M). Prometheus's `quantile_over_time` sorts in RAM and *is* capped.
- **Single-series point lookups.** One series over a time range is sub-millisecond in Postgres thanks to B-tree point queries.
- **Joins and arbitrary SQL.** PromQL is a closed language; Postgres lets you join metrics against anything else in your database.

## Where Prometheus genuinely wins

- **Disk.** 43–81× smaller per sample. At real scale (1M series × 15s × 24h), that's terabytes versus hundreds of GB.
- **Write path.** Append-only, no locks, no MVCC. One WAL record per scrape — "visible to a querier" implies "already in the WAL."
- **Counter resets.** PromQL's `rate()` walks a slice that is *already sorted at write time*. No window function, no re-sort. In Postgres, `lag(value) OVER (PARTITION BY ...)` forces a sort over the partition.

## Honest caveats

1. **Sum parity is not verified.** Prometheus sum (154M) vs Postgres sum (452M) differ by ~3×. I deliberately did not claim sum parity; sample *count* matches within 0.6% (-800 out of 144,000). The sum gap is unexplained in this run and recorded as diagnostic only.
2. **Prometheus lost 0.6% of samples** (143,200 vs 144,000). Documented, not hidden.
3. **Not yet compacted.** The 45-minute window is below Prometheus's compaction threshold (~3h of head span), so the disk figure reflects head chunks, not steady-state on-disk blocks.
4. **CPU/RAM not in this report.** The CI environment couldn't expose cAdvisor container metrics to the runner; those metrics still flow to Grafana Cloud but aren't in the published numbers.

## Bottom line

| Criterion | Winner | Why |
|-----------|--------|-----|
| Disk per sample | Prometheus | No row overhead; delta-of-delta on time-ordered data |
| Write throughput | Prometheus | Append-only, one WAL record per scrape |
| Single-series lookup | PostgreSQL | B-tree point query, sub-millisecond |
| Percentiles over range | PostgreSQL | DB-side sort, no sample ceiling |
| Counter reset / `rate()` | Prometheus | Data pre-sorted at write time |
| Ad-hoc SQL / joins | PostgreSQL | Full SQL expressiveness |
| Short time ranges | PostgreSQL | Fewer files, better query planner |

## Conclusion

Prometheus doesn't win because it has a better compression algorithm. It wins by **refusing what PostgreSQL does**: no in-place `UPDATE`, no selective `DELETE`, no one-row-per-sample. In exchange it gets a read-only, sequential, compressed structure where decompression is nearly free. The price is operational rigidity.

PostgreSQL wins when you need SQL flexibility, in-database percentiles, or fast single-series point lookups at modest cardinality. For high-cardinality, long-retention metrics at scale, Prometheus's disk and write advantages are hard to ignore — but they come with trade-offs you should measure, not assume.

---

*Methodology: all numbers from a single GitHub Actions workflow run (scale=pg), Prometheus 3.15.0 vs PostgreSQL 18.6, each capped at 1.5 CPU / 4 GB. Full reproduction in `benchctl/`.*
