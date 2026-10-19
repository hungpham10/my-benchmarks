
---
title: "Why can't we just use PostgreSQL for metrics? I benchmarked it"
date: 2026-10-19
lastmod: 2026-10-19
tags: [prometheus, postgresql, benchmark, metrics, time-series]
excerpt: "In an interview, the Head of Platform asked me why PostgreSQL can't be used for metrics. I gave the textbook answer and had no numbers to back it. So I ran the benchmark."
---

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "Why can't we just use PostgreSQL for metrics? I benchmarked it",
  "description": "In an interview, the Head of Platform asked me why PostgreSQL can't be used for metrics. I gave the textbook answer and had no numbers to back it. So I ran the benchmark.",
  "author": {
    "@type": "Person",
    "name": "Platform Engineering Team"
  },
  "publisher": {
    "@type": "Organization",
    "name": "Platform Engineering Blog"
  },
  "datePublished": "2026-10-19",
  "dateModified": "2026-10-19",
  "mainEntityOfPage": {
    "@type": "WebPage",
    "@id": "https://example.com/blog/prometheus-vs-postgres-metrics"
  },
  "keywords": ["prometheus", "postgresql", "benchmark", "metrics", "time-series"],
  "articleSection": "Engineering"
}
</script>

<meta property="og:title" content="Why can't we just use PostgreSQL for metrics? I benchmarked it">
<meta property="og:description" content="In an interview, the Head of Platform asked me why PostgreSQL can't be used for metrics. I gave the textbook answer and had no numbers to back it. So I ran the benchmark.">
<meta property="og:type" content="article">
<meta property="og:article:published_time" content="2026-10-19">
<meta property="twitter:card" content="summary">

# Why can't we just use PostgreSQL for metrics? I benchmarked it

The interview was for a Platform Engineering role at a consumer finance company. I was ready for the usual: describe a system you built, defend a trade-off. The Head of Platform skipped all that and asked:

> "Why can't we just use PostgreSQL for metrics?"

I answered with what I understood at the time: metrics are append-mostly, a series is identified by its labels, and time-ordered data compresses much better than rows. Honestly, I hadn't prepared for this question, and I didn't know either system very deeply. I tried my best to explain, but he still wasn't convinced, and I can't blame him. I could say what I believed, not why.

After the interview I kept wanting a proper reason to choose one over the other. "Prometheus already supports everything" is the easy answer, and I left it out on purpose. This post is what I found after a few weeks of digging. The conclusion is that either one can be the reasonable choice, depending on what you need, and the rest of the post shows where each one fits.

So I built a benchmark. Same data, three stores, five query shapes, and every number below comes from one green CI run.

## The short version

- **Disk:** Prometheus 12.18 bytes/sample, PostgreSQL normalized 370.88, PostgreSQL JSONB 838.25. That is 30× and 69× smaller.
- **Latency:** Prometheus was fastest on four of the five query shapes. Normalized Postgres won the fifth, a single series over 15 minutes (0.14 ms vs 0.52 ms).
- **Counter rate across all 1,600 series:** 3.0 ms on Prometheus, 63.9 ms on normalized Postgres, 874.3 ms on JSONB. Roughly 21× and 290×.
- **Saturation:** with the same load ramp, Prometheus completed 169,841 queries, normalized Postgres 4,894, JSONB 443.
- **Ingest:** Postgres COPY loaded the data in 0.9 s (normalized) and 2.7 s (JSONB). Prometheus took 0.7 s of actual writing, plus 144 s of pauses I added on purpose.
- **Not covered:** CPU and RAM, and a compacted Prometheus. More on both at the end.

## Setup

The dataset is 2 jobs × 400 instances × 2 metric families, which makes 1,600 series. Each has 180 samples, 15 seconds apart, so 288,000 samples per backend over a 45-minute window. The generator is seeded, so every run produces identical values. Each store ran in a container capped at 1.5 CPU and 4 GB RAM. Everything here is from run `gh-232.1`.

Parity check: Prometheus kept 286,400 samples and Postgres kept 288,000, a 0.56% difference, inside the 2% tolerance I set for the gate. I also compare sums, but only as a diagnostic. PromQL's instant query and Postgres's `DISTINCT ON` mean different things by "last value", so the sums differ by about 3× and that tells me nothing about data quality.

## Disk

| System | Samples kept | On disk | Bytes / sample |
|--------|--------------|---------|----------------|
| Prometheus TSDB | 286,400 | 3.3 MB | 12.18 |
| PostgreSQL normalized | 288,000 | 101.9 MB | 370.88 |
| PostgreSQL JSONB | 288,000 | 230.2 MB | 838.25 |

<svg role="img" aria-labelledby="chart-disk-title chart-disk-desc" viewBox="0 0 640 240" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-disk-title">Disk footprint — bytes per sample</title>
  <desc id="chart-disk-desc">Horizontal bar chart comparing bytes per sample: Prometheus 12.18, PostgreSQL normalized 370.88, PostgreSQL JSONB 838.25.</desc>
  <rect x="80" y="30" width="1" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="90" width="28" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="150" width="64" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Prometheus</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres normalized</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres JSONB</text>
  <text x="86" y="59" font-family="system-ui, sans-serif" font-size="12" fill="#374151">12.18</text>
  <text x="112" y="119" font-family="system-ui, sans-serif" font-size="12" fill="#374151">370.88</text>
  <text x="148" y="179" font-family="system-ui, sans-serif" font-size="12" fill="#374151">838.25</text>
</svg>

Most of the gap is label text. JSONB repeats the whole label document on every row, and `env="production"` gets stored a million times because Postgres doesn't intern strings. The normalized schema keeps rows small, but it still carries label text per row and the `(series_id, time)` index adds its own weight. Prometheus stores each label set once in its index and writes each sample as a timestamp and a value.

Those two numbers are cheap because of how they are encoded. Timestamps use delta-of-delta, so with a steady scrape interval they cost about one bit each. Values use Gorilla XOR. None of this needed a compaction pass. It happens as samples arrive, because they arrive in time order.

## Why Prometheus is smaller and faster

Prometheus gets there by not doing four things Postgres has to do:

| Prometheus doesn't | What it saves |
|--------------------|---------------|
| In-place UPDATE / DELETE | Writes are append-only and time-ordered |
| One row per sample | The label set is the identity, so labels aren't repeated per sample |
| General-purpose B-tree indexes | An inverted index over series refs, not rows |
| MVCC row versioning | No 23-byte tuple header per row, no row-level WAL records |

The last two explain most of the bytes-per-sample gap.

## Ingest

| System | Write time | Throughput while writing |
|--------|-----------|--------------------------|
| PostgreSQL JSONB | 2.7 s (COPY) | ~106k samples/s |
| PostgreSQL normalized | 0.9 s (COPY) | ~311k samples/s |
| Prometheus | 0.7 s | ~421k samples/s |

Prometheus's wall-clock time was much longer, about 144 s more. That is my own `REQUEST_DELAY`, a pause after each remote-write batch so the WAL doesn't get flooded. It says nothing about Prometheus's speed, so compare the write times above and ignore wall clock. Postgres had no such pause.

## The five queries

The labels q1–q5 are just handles. Here is what each one actually runs; every shape exists to probe a different claim.

| Query | What I'm measuring | PromQL | Postgres SQL (normalized schema) |
|-------|--------------------|--------|----------------------------------|
| q1 | Rate across every counter series | `sum(rate(http_requests_total[5m]))` | `lag(value) OVER (PARTITION BY series_id ORDER BY time)`, then sum the deltas |
| q2 | Queue depth grouped by job | `sum by (job) (queue_depth)` | `GROUP BY job` with `avg(value)` |
| q3 | p90 per instance for `job-00`, one hour | `quantile_over_time(0.9, queue_depth{job="job-00"}[1h])` | `percentile_cont(0.9) WITHIN GROUP (ORDER BY value)`, grouped by instance |
| q4 | Average queue depth for `job-00`, full window | `sum(queue_depth{job="job-00"})` | `avg(value)` where `job = 'job-00'` |
| q5 | One instance, last 15 minutes | `queue_depth{instance="host-0000"}` | `avg(value)` where `instance = 'host-0000'` |

The JSONB schema runs the same SQL against one `labels` document (`labels->>'job'`, `labels @> '{"__name__":...}'`) instead of joining `series_dim`.

The table also makes one asymmetry visible. Prometheus runs each PromQL as an instant query at the end of the window; Postgres aggregates across the window. For q1 that means Prometheus reads the last five minutes while Postgres walks all 45. So this compares what the shape costs on each engine, not whether the two scan the same number of rows. I point out where that changes the reading.

## Query latency

One connection, warm cache, 30 measured runs after 3 warm-ups, with an explicit `time=` at the end of the dataset window. Every query returned rows. The harness records how many rows each query matched and refuses to report a latency for an empty result, which would otherwise look like the fastest possible query.

| Query | Shape | Prometheus p50 / p95 | Postgres JSONB p50 / p95 | Postgres normalized + BRIN p50 / p95 |
|-------|-------|----------------------|--------------------------|--------------------------------------|
| q1 | Rate across every counter | 3.0 / 4.5 ms | 874.3 / 910.8 ms | 63.9 / 65.1 ms |
| q2 | Sum by job | 2.6 / 4.0 ms | 71.0 / 77.8 ms | 31.1 / 32.6 ms |
| q3 | p90 per instance, 1 h | 9.5 / 18.7 ms | 45.5 / 86.2 ms | 12.2 / 12.4 ms |
| q4 | One job, full window | 1.6 / 1.7 ms | 21.8 / 22.0 ms | 7.1 / 7.3 ms |
| q5 | One instance, 15 minutes | 0.52 / 0.58 ms | 0.23 / 0.38 ms | 0.14 / 0.32 ms |

<svg role="img" aria-labelledby="chart-q-title chart-q-desc" viewBox="0 0 640 270" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-q-title">Query latency by shape — p50, milliseconds (log scale)</title>
  <desc id="chart-q-desc">Grouped bars comparing p50 latency for five query shapes across Prometheus, PostgreSQL JSONB, and PostgreSQL normalized. Prometheus wins q1 through q4; normalized Postgres wins q5.</desc>
  <text x="16" y="20" font-family="system-ui, sans-serif" font-size="12" fill="#6b7280">p50 ms (log scale)</text>
  <g font-family="system-ui, sans-serif" font-size="11" fill="#374151">
    <text x="16" y="70">q1</text><text x="16" y="115">q2</text><text x="16" y="160">q3</text><text x="16" y="205">q4</text><text x="16" y="250">q5</text>
  </g>
  <g>
    <rect x="50" y="52" width="12" height="16" fill="#10b981" rx="2"/>
    <rect x="66" y="70" width="110" height="16" fill="#ef4444" rx="2"/>
    <rect x="66" y="88" width="89" height="16" fill="#3b82f6" rx="2"/>
    <rect x="50" y="97" width="21" height="16" fill="#10b981" rx="2"/>
    <rect x="66" y="115" width="83" height="16" fill="#ef4444" rx="2"/>
    <rect x="66" y="133" width="70" height="16" fill="#3b82f6" rx="2"/>
    <rect x="50" y="142" width="38" height="16" fill="#10b981" rx="2"/>
    <rect x="66" y="160" width="77" height="16" fill="#ef4444" rx="2"/>
    <rect x="66" y="178" width="50" height="16" fill="#3b82f6" rx="2"/>
    <rect x="50" y="187" width="21" height="16" fill="#10b981" rx="2"/>
    <rect x="66" y="205" width="61" height="16" fill="#ef4444" rx="2"/>
    <rect x="66" y="223" width="40" height="16" fill="#3b82f6" rx="2"/>
  </g>
  <g font-family="system-ui, sans-serif" font-size="11" fill="#6b7280">
    <rect x="420" y="52" width="10" height="10" fill="#10b981" rx="1"/><text x="436" y="61">Prometheus</text>
    <rect x="420" y="70" width="10" height="10" fill="#ef4444" rx="1"/><text x="436" y="79">Postgres JSONB</text>
    <rect x="420" y="88" width="10" height="10" fill="#3b82f6" rx="1"/><text x="436" y="97">Postgres normalized + BRIN</text>
  </g>
</svg>

Prometheus won q1 to q4, Postgres normalized won q5. Three things in this table are worth a closer look.

q1 is the counter-reset shape. In Postgres you write `lag() OVER (PARTITION BY ...)`, which has to order and walk every row. JSONB also pays a heap fetch per row, which is where the 874 ms comes from. Normalizing and joining a small dimension table brings it to 64 ms. Prometheus reads a slice that was already sorted when it was written, and takes 3 ms. The windows differ here as noted above: Prometheus answers a five-minute rate, Postgres walks the whole 45-minute window, so part of the gap is volume rather than algorithm. Even so, the window function plus heap fetch is doing most of the damage.

q3 surprised me. I expected percentiles to be Postgres's home turf: `percentile_cont` sorts inside the database with no sample limit, while `quantile_over_time` sorts in RAM under `--query.max-samples`. At 400 series and 45 minutes Prometheus still won (9.5 ms vs 12.2 ms). I'd expect that to change at much larger N, so I'm not drawing a conclusion from it.

q5 is a clean Postgres win. Postgres averages 60 rows from one series, a B-tree lookup, and 0.14 ms is hard to beat. Prometheus answers the selector at a single instant, so it reads one sample rather than a range. If someone tells you Prometheus wins every shape, check their table.

## Load test

I used k6 to ramp from 10 to 2,000 requests per second (an open model, `ramping-arrival-rate`) with a p95 threshold of 500 ms. All three crossed it (exit code 99), so this is a saturation probe and I wouldn't read it as a latency measurement. It only ran one query shape, q1:

```
sum(rate(http_requests_total[5m]))
```

On Postgres the equivalent is the `lag() OVER (PARTITION BY ...)` query.

| System | Completed | Dropped | p50 | p95 |
|--------|-----------|---------|-----|-----|
| Prometheus | 169,841 | 299,958 | 1.6 s | 1.8 s |
| PostgreSQL normalized | 4,894 | 464,906 | 3.6 s | 5.1 s |
| PostgreSQL JSONB | 443 | 469,356 | 41.4 s | 52.5 s |

<svg role="img" aria-labelledby="chart-break-title chart-break-desc" viewBox="0 0 640 240" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-break-title">Iterations completed over the whole ramp</title>
  <desc id="chart-break-desc">Horizontal bar chart of completed iterations: Prometheus 169,841, PostgreSQL normalized 4,894, PostgreSQL JSONB 443.</desc>
  <rect x="80" y="30" width="64" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="90" width="2" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="150" width="1" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Prometheus</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres normalized</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres JSONB</text>
  <text x="148" y="59" font-family="system-ui, sans-serif" font-size="12" fill="#374151">169,841</text>
  <text x="86" y="119" font-family="system-ui, sans-serif" font-size="12" fill="#374151">4,894</text>
  <text x="86" y="179" font-family="system-ui, sans-serif" font-size="12" fill="#374151">443</text>
</svg>

Each row adds up to about 469,800, the full ramp. So "completed" counts the whole run, not the point where p95 crossed 500 ms. Over the same ramp Prometheus served about 35× more queries than normalized Postgres and 383× more than JSONB.

The 1.6 s for Prometheus is p50 with hundreds of clients in flight. The same query takes 3 ms on one connection, and the rest is queueing. The latency table tells you what a query costs, and this one tells you how much load the system takes before the tail falls over.

## What I measured, and how

Five numbers, each with a rule for what counts:

| Metric | How it was measured | What it answers |
|--------|---------------------|-----------------|
| Disk | Bytes on disk after loading 288,000 samples, divided by samples kept. Includes the WAL for Prometheus and the indexes for Postgres. | What does one sample cost to keep? |
| Query latency | p50/p95 over 30 runs after 3 warm-ups, one connection, warm cache, explicit `time=` at the window end. Empty results are rejected. | What does this query shape cost? |
| Ingest | Wall time for COPY (Postgres) and remote-write (Prometheus), with Prometheus's 144 s pacing excluded from throughput. | How fast does the write path accept data? |
| Saturation | k6 open-model ramp from 10 to 2,000 req/s, p95 threshold 500 ms, iterations completed over the whole ramp. | How much load before the tail collapses? |
| Parity | Sample count per backend, within 2%. | Are the three stores holding the same data? |

What keeps the comparison fair: one seeded dataset, the same 1.5 CPU / 4 GB cap on every container, and a fixed expression per backend for each of the five shapes. Where the engines don't do identical work, the caveats at the end say so.

## Where Postgres is the better choice

- Single-series, short-range lookups. q5 shows it: 0.14 ms vs 0.52 ms.
- Percentiles with no sample cap. `percentile_cont` isn't bound by `--query.max-samples` (default 50M), and at very large N that matters.
- Joins. PromQL can't join metrics to customers or transactions, and SQL can.
- Retention. Dropping a partition is O(1). Prometheus writes tombstones and rewrites blocks at the next compaction.

## What this doesn't show

1. The two engines' sums differ by about 3× (149.1M vs 452.1M) because they define "last value" differently. Only the sample count is a real check.
2. Prometheus lost 0.56% of samples. I haven't tracked down why.
3. Nothing was compacted. 45 minutes is below the roughly 3 h head span, so the disk number is for head chunks and steady-state blocks may look different.
4. There are no CPU or RAM figures. The CI runner couldn't read container metrics. They go to Grafana Cloud but aren't in this post.
5. The query sweep is single-connection and warm. It measures query cost only. The k6 run is the concurrency view.
6. The scale is small, 1,600 series and 288,000 samples. It shows the shape of the costs and says little about how they grow. q3 especially could flip.
7. I used `prom/prometheus:latest` and `postgres:latest` and didn't record image digests.
8. The ~1.37 B/sample Gorilla figure and TimescaleDB's 90-95% compression claim are from papers and docs, not from my runs.
9. The two engines don't always scan the same window. Prometheus answers an instant query at the end of the window; the Postgres side aggregates across the window. q1 is the clearest case, a five-minute rate against a 45-minute walk. The table compares what the shape costs on each engine, not identical work.

## What I'd say now

If he asked again, I wouldn't say "Postgres can't do it", because it can. I'd say it can, here is what it costs, and here is where it wins:

> PostgreSQL can store metrics, but every sample pays for a tuple header, index entries, WAL, and a heap fetch whenever you filter on a label. In my test that came to 30× to 69× more disk and up to about 290× slower on a counter-rate query, though part of that last gap is the wider window Postgres had to scan. Prometheus drops all of that: no UPDATE, no per-sample row, and decompression is nearly free. In return it's rigid. If you need joins, percentiles without a sample cap, or years of retention next to business data, Postgres is a reasonable choice and sometimes the better one.

So the answer depends on the workload, and the table below is how I'd decide.

| Situation | Use |
|-----------|-----|
| Infrastructure monitoring, Kubernetes, real-time alerts | Prometheus |
| Years of retention, metrics joined to business data | PostgreSQL + TimescaleDB, or a hybrid |
| SQL percentiles without a cap, ad-hoc reporting | PostgreSQL |
| High cardinality and disk cost matters | Prometheus |

## Was my guess right?

In the interview I made two claims without evidence. Here is how they held up.

| What I said | What I measured | Verdict |
|-------------|-----------------|---------|
| Postgres will use much more disk, mainly because of its indexes. | 30× more per sample than Prometheus on the normalized schema, 69× for JSONB. But the two Postgres schemas hold identical rows and still differ by 2.3× (838 vs 371 B/sample), and that gap is the label document repeated on every row. | Right that disk is the problem; wrong that the index is the main cause. |
| `rate()` will be slower on Postgres. | q1: 3.0 ms on Prometheus, 63.9 ms normalized, 874.3 ms JSONB. | Right in direction. The size depends on schema and window: part of the gap is Postgres scanning 45 minutes against Prometheus's 5. |
| I did not expect Postgres to win any shape. | q5: 0.14 ms normalized vs 0.52 ms Prometheus. | Missed. Short, single-series lookups are a Postgres strength. |

Both claims were directionally right, which is why the follow-up "why?" was the real test. But the mechanism I gave was too simple. I blamed the B-tree. The measurements point at identity instead: JSONB stores the whole label document on every row, and the 2.3× gap between the two Postgres schemas — same rows, different layout — is that repetition. The normalized schema avoids it but still keeps an index entry per sample. Prometheus writes the label set once, then only a timestamp and a value.

So the interview answer was half right. The cost is real, but it is mostly the cost of restating identity, not the index, and the speed gap depends on the query shape rather than being universal. That is also why the conclusion is a decision table and not a winner.

## Reproduce it

Everything runs on GitHub Actions at `github.com/hungpham10/my-benchmarks`, in `benchctl/`. The numbers come from run `gh-232.1` on `main`, which took about 33 minutes. `scale=smoke` takes about 3 minutes and just shows the pipeline working. `scale=pg` is the full comparison and includes the q1 to q5 sweep.

*Measured figures are from one `scale=pg` run, using `prom/prometheus:latest` and `postgres:latest`, each capped at 1.5 CPU / 4 GB. The company and the Head of Platform are not named, by request.*
