---
title: "Prometheus vs PostgreSQL as a Metrics Store: An Interview Question, Benchmarked"
date: 2026-10-18
lastmod: 2026-10-18
tags: [prometheus, postgresql, benchmark, metrics, time-series]
excerpt: "A head of platform once asked why Prometheus or InfluxDB is better than PostgreSQL for metrics. I answered that PostgreSQL can do it too. That was the trap. I benchmarked all three on identical data. Here are the measured numbers."
---

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "Prometheus vs PostgreSQL as a Metrics Store: An Interview Question, Benchmarked",
  "description": "A head of platform once asked why Prometheus or InfluxDB is better than PostgreSQL for metrics. I benchmarked all three on identical data. Here are the measured numbers.",
  "author": {
    "@type": "Person",
    "name": "Platform Engineering Team"
  },
  "publisher": {
    "@type": "Organization",
    "name": "Platform Engineering Blog"
  },
  "datePublished": "2026-10-18",
  "dateModified": "2026-10-18",
  "mainEntityOfPage": {
    "@type": "WebPage",
    "@id": "https://example.com/blog/prometheus-vs-postgres-metrics"
  },
  "keywords": ["prometheus", "postgresql", "benchmark", "metrics", "time-series"],
  "articleSection": "Engineering"
}
</script>

<meta property="og:title" content="Prometheus vs PostgreSQL as a Metrics Store: An Interview Question, Benchmarked">
<meta property="og:description" content="A head of platform once asked why Prometheus or InfluxDB is better than PostgreSQL for metrics. I benchmarked all three on identical data. Here are the measured numbers.">
<meta property="og:type" content="article">
<meta property="og:article:published_time" content="2026-10-18">
<meta property="twitter:card" content="summary">

# Prometheus vs PostgreSQL: An Interview Question, Benchmarked

## The question I answered wrong

During a Platform Engineering interview at a consumer bank (name withheld), the Head of Platform asked me a question that sounded simple:

> "Why use Prometheus or InfluxDB for metrics instead of PostgreSQL?"

I fell into the trap. I answered that PostgreSQL can do it too — you can store metrics in a Postgres table and query with SQL.

He nodded. Only afterward did I realize the real question: not "can PostgreSQL store metrics," but "do you know what you pay for that convenience?" I didn't have a good answer.

After the interview, I benchmarked all three systems on identical data, identical query load, and identical hardware limits. This post is the answer I wish I had given — not opinion, but measured numbers.

## Key Takeaways

- **Disk:** Prometheus **12.68 bytes/sample** vs PostgreSQL normalized **540.09 B/sample** vs PostgreSQL JSONB **1,024.56 B/sample**. Prometheus is 43× smaller than normalized Postgres and 81× smaller than JSONB.
- **Ingest:** Prometheus spends 0.5 s actually writing plus ~72 s of pacing delay (a shield against WAL backpressure, not a tuning knob). PostgreSQL COPY: 0.6 s normalized / 1.4 s JSONB.
- **Breaking point** (k6 load test, **one query shape only**: `sum(rate(http_requests_total[5m]))`, ramp 10→200→2000 req/s, p95 threshold 500 ms): PostgreSQL normalized completed 469,604 iterations at p50 0.5 ms; Prometheus completed 176,266 at p50 1.4 s; PostgreSQL JSONB completed only 278 at p50 66 s.
- **Query scenarios defined:** five shapes (q1–q5) are specified in `benchctl/shared/queries.py` with PromQL and SQL equivalents; q1 was measured via k6, q2–q5 are now measurable with the new query-sweep phase.
- **Parity:** sample counts match within 0.6% (−800 of 144,000, inside the 2% tolerance). Sums differ by ~3× and are recorded as diagnostic only.
- **Caveat:** Prometheus was not yet compacted (45-minute window is below the ~3 h head threshold), CPU/RAM was not captured in CI.

## What Prometheus actually did: four "refusals"

Prometheus does not win because it has a better compression algorithm. It wins because it **refuses what PostgreSQL does**. Each refusal is an architectural decision with measurable cost:

| # | Prometheus refuses | Architectural consequence |
|---|-------------------|--------------------------|
| 1 | In-place `UPDATE` / `DELETE` | Append-only, time-ordered writes only |
| 2 | One row per sample | The label set *is* the identity; no per-row label repetition |
| 3 | General-purpose B-tree indexes | Inverted postings index (series refs, not rows) |
| 4 | MVCC visibility columns | No 23–24 byte tuple header per row, no per-sample WAL |

The last two account for most of the bytes-per-sample gap below.

## Same data, same load

All three systems received the same synthetic dataset (~20k series, 15 s scrape interval, 45-minute window). Each system ran under the same constraint: **1.5 CPU / 4 GB RAM**. Every run verifies sample parity within 2% (`failed: 0`). Sums are diagnostic only — they are not part of the verified claim.

## Disk footprint (chart and table)

| System | Samples kept | Bytes on disk | **Bytes / sample** |
|--------|--------------|---------------|--------------------|
| Prometheus TSDB | 143,200 | 1.7 MB | **12.68** |
| PostgreSQL Normalized | 144,000 | 74.2 MB | **540.09** |
| PostgreSQL JSONB | 144,000 | 140.7 MB | **1,024.56** |

<svg role="img" aria-labelledby="chart-disk-title chart-disk-desc" viewBox="0 0 640 240" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-disk-title">Disk footprint — bytes per sample</title>
  <desc id="chart-disk-desc">Horizontal bar chart comparing bytes per sample: Prometheus 12.68, PostgreSQL normalized 540.09, PostgreSQL JSONB 1,024.56.</desc>
  <rect x="80" y="30" width="11" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="90" width="33" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="150" width="64" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Prometheus</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres normalized</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres JSONB</text>
  <text x="93" y="59" font-family="system-ui, sans-serif" font-size="12" fill="#374151">12.68</text>
  <text x="95" y="119" font-family="system-ui, sans-serif" font-size="12" fill="#374151">540.09</text>
  <text x="95" y="179" font-family="system-ui, sans-serif" font-size="12" fill="#374151">1,024.56</text>
</svg>

### Why PostgreSQL uses more space

The cost is not the index structure — it is the **label text**.

- **PostgreSQL JSONB** stores the entire label document on *every row*. Even if `env="production"` repeats millions of times, PostgreSQL has no system-level string interning. A GIN index tells Postgres which rows match, but every match still requires a heap fetch to read the value.
- **PostgreSQL Normalized** inverts this: cheap rows, expensive indexes (index-to-heap ratio ~0.61). But it still stores label text at least once per row.
- **Prometheus** stores each label set once in its inverted index (postings list). Subsequent samples are just two numbers: timestamp and value.

The most important compression happens when the first sample enters the engine: **delta-of-delta** on timestamps and **Gorilla XOR** on values. When scrape intervals are stable, successive timestamp deltas are nearly zero, so timestamps cost roughly 1 bit each. A 120-sample chunk spends about 15 bytes on timestamps — not because compaction improves compression, but because Prometheus writes in time order from the start.

## Ingest speed: real work, not wall-clock

| System | Actual write time | Throughput during write |
|--------|------------------|------------------------|
| PostgreSQL JSONB | 1.4 s (COPY) | ~106k samples/s |
| PostgreSQL Normalized | 0.6 s (COPY) | ~254k samples/s |
| Prometheus | 0.5 s ingest + 72 s pacing delay | ~284k samples/s during ingest |

Important caveat: Prometheus wall time includes ~72 seconds of **pacing delay** (`REQUEST_DELAY`) to avoid overwhelming the WAL. Without pacing, Prometheus drops roughly 30% of samples. This is a backpressure shield, not a tuning knob — comparing wall-clock times directly is unfair. What matters is real write throughput once pacing is accounted for, and there Prometheus holds its own.

Note: those 72 seconds are **Prometheus remote-write** pacing, not PostgreSQL COPY. Postgres runs COPY unpaced, so the numbers above are real for each system.

## Query load test: who breaks first?

I used k6 to ramp query rate from 10 to 2,000 requests per second (`ramping-arrival-rate`, open model), with a p95 threshold of 500 ms. All three systems hit the breaking point (k6 exit code 99).

**Important: the k6 leg tested exactly one query shape — q1 counter rate:**

```
sum(rate(http_requests_total[5m]))
```

This is the PromQL for "counter rate across every counter series." PostgreSQL answers the same shape with `lag() OVER (PARTITION BY ...)` window functions over the time window. Here is what each system did right before it broke:

| System | Iterations at break | p50 latency | p95 latency | Iterations dropped |
|--------|---------------------|-------------|-------------|--------------------|
| PostgreSQL JSONB | 278 | 66 s | 81 s | 469,522 |
| PostgreSQL Normalized | 469,604 | 0.5 ms | 0.7 ms | 196 |
| Prometheus | 176,266 | 1.4 s | 3.1 s | 293,533 |

<svg role="img" aria-labelledby="chart-break-title chart-break-desc" viewBox="0 0 640 240" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-break-title">Breaking point — iterations achieved before p95 exceeds 500 ms</title>
  <desc id="chart-break-desc">Horizontal bar chart: PostgreSQL normalized 469,604 iterations, Prometheus 176,266 iterations, PostgreSQL JSONB 278 iterations.</desc>
  <rect x="80" y="30" width="64" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="90" width="24" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="150" width="1" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres normalized</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Prometheus</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres JSONB</text>
  <text x="95" y="59" font-family="system-ui, sans-serif" font-size="12" fill="#374151">469,604</text>
  <text x="95" y="119" font-family="system-ui, sans-serif" font-size="12" fill="#374151">176,266</text>
  <text x="95" y="179" font-family="system-ui, sans-serif" font-size="12" fill="#374151">278</text>
</svg>

**Main finding:** At this query shape (calculating `rate()` over counter series, grouped by `job`), normalized PostgreSQL wins on both latency and throughput. Prometheus dropped about 293k iterations because each query takes roughly 1–3 seconds — it cannot keep up at 2,000 req/s. JSONB breaks almost immediately because each query takes about 80 seconds (JSONB containment plus heap fetch for every row).

Note: Prometheus p95 latency varies between runs (~1.9–3.1 s). Latency at the breaking point is noisier than the mean — treat it as a range, not a single number.

## Five query scenarios — measured vs defined

The benchmark defines **five query shapes** in `benchctl/shared/queries.py`, each with a PromQL version and equivalent SQL for both PostgreSQL schemas. Only **q1** was executed through the k6 load test in run `gh-219.1`. The query-sweep phase (added to `benchctl/driver/run.py`) now measures per-query latency for all five shapes; results from that phase will be reported in a follow-up run.

| # | Scenario | PromQL | SQL equivalent | Measured in gh-219.1 |
|---|----------|--------|----------------|----------------------|
| q1 | Counter rate across every counter series | `sum(rate(http_requests_total[5m]))` | `lag() OVER (PARTITION BY ...)` window over 5m range | **Yes** — k6 breaking point |
| q2 | Grouped aggregation by job | `sum by (job) (queue_depth)` | `GROUP BY job` with `avg(value)` | No — defined, not measured |
| q3 | Per-series p90 over 1 hour | `quantile_over_time(0.9, queue_depth{job="job-00"}[1h])` | `percentile_cont(0.9) WITHIN GROUP` | No — defined, not measured |
| q4 | Selective wide: one job across full window | `sum(queue_depth{job="job-00"})` | `WHERE s.job = 'job-00'` + `avg(value)` | No — defined, not measured |
| q5 | Short range: single series, 15 minutes | `queue_depth{instance="host-0000"}` | `WHERE s.instance = 'host-0000'` + `avg(value)` | No — defined, not measured |

**Winner expectations** (from architectural intent, not gh-219.1 measurement): q1 → Prometheus; q2 → even; q3 → PostgreSQL; q4 → Prometheus; q5 → PostgreSQL. These are hypotheses to verify with the query-sweep phase, not claims from this run.

## What PostgreSQL genuinely wins

If this post only said "PostgreSQL is slow," it would be wrong. The architecture shows PostgreSQL genuinely wins in three areas:

1. **Percentiles over a time range.** `percentile_cont(0.9) WITHIN GROUP (ORDER BY value)` sorts inside the database and is not subject to Prometheus's `--query.max-samples` ceiling (default 50M). Prometheus's `quantile_over_time` sorts in RAM and is capped.
2. **Single-series point lookups.** Retrieving one series at one timestamp is under 1 ms in PostgreSQL thanks to B-tree / BRIN point lookups.
3. **Complex SQL and joins.** PromQL is a closed language. PostgreSQL lets you join metrics directly with business data (customers, transactions) in a single query.

## Honesty caveats — the answer I wish I had given

1. **Sum parity is not verified.** The Prometheus sum (154M) vs PostgreSQL sum (452M) differ by ~3×. I deliberately did not claim sum parity; sample *count* matches within 0.6% (−800 out of 144,000). The sum gap is unexplained and recorded as diagnostic only.
2. **Prometheus lost 0.6% of samples** (143,200 vs 144,000). Documented, not hidden.
3. **Not yet compacted.** The 45-minute window is below Prometheus's compaction threshold (~3 h of head span), so the disk figure reflects head chunks, not steady-state on-disk blocks.
4. **CPU/RAM is absent from this report.** The CI environment could not expose cAdvisor container metrics to the runner; those metrics flow to Grafana Cloud but are not published here.
5. **Only q1 was measured under load.** The k6 leg tested `sum(rate(http_requests_total[5m]))` only. q2–q5 are defined in the query catalog with PromQL + SQL equivalents, and the query-sweep phase can measure them in a follow-up run.
6. **Some figures are literature, not measured.** The ~1.37 B/sample Gorilla figure and TimescaleDB's 90–95% compression claim come from papers and documentation, not from this benchmark.

## The answer I wish I had given

If I met the Head of Platform again, I would not say "PostgreSQL can do it too." I would say:

> "PostgreSQL can store metrics, but you pay for it: tuple headers, index bloat, per-sample WAL, and a heap fetch every time you ask 'what is the value of this label?' Prometheus refuses those costs — no in-place UPDATE, no selective DELETE, no one row per sample. In exchange it gets a read-only, sequential, compressed structure where decompression is nearly free. The price is operational rigidity."

| Situation | Choose |
|-----------|--------|
| Infrastructure monitoring, Kubernetes, real-time alerting | **Prometheus** |
| Multi-year time-series retention, joining metrics with business data | **PostgreSQL + TimescaleDB** (or hybrid) |
| SQL percentiles without a sample ceiling, ad-hoc reporting | **PostgreSQL** |
| High cardinality, long retention, disk cost is critical | **Prometheus** |

## Reproduce

The entire benchmark runs automatically on GitHub Actions at `github.com/hungpham10/my-benchmarks` (directory `benchctl/`). The numbers in this post come from run `gh-219.1` on the `main` branch. You can run `scale=smoke` (~3 minutes) to verify parity checks yourself, then `scale=pg` (~31 minutes) for the full picture. The query-sweep phase (q1–q5 per-query latency) is part of the `scale=pg` pipeline.

---

*All figures come from a single GitHub Actions workflow run (scale=pg, run `gh-219.1`), Prometheus 3.15.0 vs PostgreSQL 18.6, each capped at 1.5 CPU / 4 GB. The company name and the Head of Platform are withheld by request.*