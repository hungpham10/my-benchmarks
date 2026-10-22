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

The interview was for a Platform Engineering role. The Head of Platform skipped the usual questions and asked:

> "Why can't we just use PostgreSQL for metrics?"

I said what I believed: metrics are append-mostly, a series is identified by its labels, and time-ordered data compresses better than rows. He wasn't convinced, and I can't blame him. I could say what I believed, not why. So I ran the benchmark.

## The short version

- **Disk:** Prometheus 12.18 bytes/sample, PostgreSQL normalized 370.88, PostgreSQL JSONB 838.25. That is 30× and 69× smaller.
- **One series, 30 days, 5-second scrape:** 6.3 MB on Prometheus, 192.3 MB on normalized Postgres, 434.5 MB on JSONB. At 10,000 series that is $4.70, $304 and $688 per month of GCP storage.
- **Query latency:** Prometheus was fastest on four of five query shapes. Postgres won the fifth: a single series over 15 minutes.
- **Counter rate across all series:** 3.0 ms on Prometheus, 63.9 ms on normalized Postgres, 874.3 ms on JSONB.
- **Under load:** over the same ramp, Prometheus completed 169,841 queries, normalized Postgres 4,894, JSONB 443.

Either can be the right choice. It depends on the workload.

## Setup

- **Data:** 1,600 series (2 jobs × 400 instances × 2 metric families), 180 samples each at 15 s intervals, 288,000 samples per backend. The generator is seeded, so every run is identical.
- **Limits:** each store ran in a container capped at 1.5 CPU and 4 GB RAM.
- **Postgres schemas:** "use Postgres for metrics" can mean very different table designs, so I tested two.
  - **JSONB:** one row per sample, the whole label set in a `jsonb` column with a GIN index. This is what most teams write first.
  - **Normalized:** labels in a small `series_dim` table, samples stored as `(series_id, time, value)`, BRIN on time and a btree on `(series_id, time)`. This is roughly the best Postgres can do here.

## The Postgres schemas

Here is what the two look like. If both lose, the result is about Postgres; if only one loses, it is about the schema.

```sql
-- Schema 1: labels as a document. The layout most teams write first.
CREATE TABLE metrics_jsonb (
    time    timestamptz      NOT NULL,
    labels  jsonb            NOT NULL,
    value   double precision NOT NULL
) PARTITION BY RANGE (time);

CREATE INDEX metrics_jsonb_labels_idx ON metrics_jsonb USING gin (labels jsonb_path_ops);
CREATE INDEX metrics_jsonb_time_idx   ON metrics_jsonb (time);

-- Schema 2: identity in its own table, BRIN on time.
CREATE TABLE series_dim (
    id       int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    metric   text NOT NULL,
    job      text NOT NULL,
    instance text NOT NULL,
    region   text NOT NULL,
    service  text NOT NULL,
    UNIQUE (metric, job, instance)
);

CREATE TABLE metrics_norm_brin (
    time      timestamptz      NOT NULL,
    series_id int              NOT NULL,
    value     double precision NOT NULL
) PARTITION BY RANGE (time);

CREATE INDEX metrics_norm_brin_time_brin_idx
    ON metrics_norm_brin USING brin (time) WITH (pages_per_range = 32);
CREATE INDEX metrics_norm_brin_series_time_idx
    ON metrics_norm_brin (series_id, time);
```

Both parents are partitioned by day on `time`. The loader creates the partitions at run time, because the dataset is timestamped in the future and `initdb` only sees an empty `PGDATA`.

The first schema is the one most teams reach for: a row per sample, the whole series identity in a JSON document, GIN for containment. It is also the one that stores `env="production"` again on every row.

The second is the best Postgres can do for this access pattern: a 4-byte `series_id` instead of the label text, BRIN on time because samples are append-only and roughly time-ordered, and a btree on `(series_id, time)` for the per-series lookups. BRIN is the Postgres-native answer to append-only data: it stores min/max per page range and costs kilobytes rather than megabytes. Whether it helps here is one of the things the run measures.

## Disk

| System | On disk | Bytes / sample |
|--------|---------|----------------|
| Prometheus | 3.3 MB | 12.18 |
| PostgreSQL normalized | 101.9 MB | 370.88 |
| PostgreSQL JSONB | 230.2 MB | 838.25 |

<svg role="img" aria-labelledby="chart-disk-title chart-disk-desc" viewBox="0 0 640 230" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-disk-title">Disk footprint — bytes per sample</title>
  <desc id="chart-disk-desc">Horizontal bar chart comparing bytes per sample: Prometheus 12.18, PostgreSQL normalized 370.88, PostgreSQL JSONB 838.25.</desc>
  <rect x="80" y="30" width="1" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="90" width="28" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="150" width="64" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Prometheus</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Postgres normalized</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Postgres JSONB</text>
  <text x="86" y="59" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">12.18</text>
  <text x="112" y="119" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">370.88</text>
  <text x="148" y="179" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">838.25</text>
</svg>

Most of the gap is repeated identity. JSONB stores the full label document on every row, so `env="production"` is written a million times. The normalized schema avoids that, but still pays for a row header, index entries and WAL per sample. Prometheus stores each label set once, then only a timestamp and a value. Timestamps are delta-of-delta encoded and values use Gorilla XOR, so a steady scrape costs close to nothing.

What does that mean in practice? One series scraped every 5 seconds for 30 days:

| Store | One series | 10,000 series |
|-------|-----------|---------------|
| Prometheus | 6.3 MB | 63 GB |
| PostgreSQL normalized | 192.3 MB | 1.92 TB |
| PostgreSQL JSONB | 434.5 MB | 4.35 TB |

This is extrapolated from the measured bytes per sample, so treat it as an order of magnitude.

On GCP (`us-central1`, list prices as of October 2026, storage only), Hyperdisk Balanced is $0.08 per GiB-month and Cloud SQL for PostgreSQL SSD is $0.17 per GiB-month:

| Store | 1 series | 1,000 series | 10,000 series |
|-------|----------|--------------|---------------|
| Prometheus, Hyperdisk Balanced | $0.0005 | $0.47 | $4.70 |
| PostgreSQL normalized, Cloud SQL SSD | $0.03 | $30 | $304 |
| PostgreSQL JSONB, Cloud SQL SSD | $0.07 | $69 | $688 |

At 10,000 series that is $304 a month before the instance, and $688 if the labels stay in JSONB. HA storage doubles the Cloud SQL rate, backups are billed separately, and GCP bills in GiB while the sizes above are decimal, so a naive multiply comes out about 7% higher.

## Query latency

The five query shapes, run on all three stores (one connection, warm cache, p50 of 30 runs):

| Shape | Prometheus | Postgres normalized | Postgres JSONB |
|-------|-----------|---------------------|----------------|
| q1: rate across all counters | 3.0 ms | 63.9 ms | 874.3 ms |
| q2: sum by job | 2.6 ms | 31.1 ms | 71.0 ms |
| q3: p90 per instance, 1 h | 9.5 ms | 12.2 ms | 45.5 ms |
| q4: one job, full window | 1.6 ms | 7.1 ms | 21.8 ms |
| q5: one instance, 15 min | 0.52 ms | **0.14 ms** | 0.23 ms |

<svg role="img" aria-labelledby="chart-q-title chart-q-desc" viewBox="0 0 640 270" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-q-title">Query latency by shape — p50, milliseconds (log scale)</title>
  <desc id="chart-q-desc">Grouped bars comparing p50 latency for five query shapes across Prometheus, PostgreSQL JSONB, and PostgreSQL normalized. Prometheus wins q1 through q4; normalized Postgres wins q5.</desc>
  <text x="16" y="20" font-family="system-ui, sans-serif" font-size="12" fill="currentColor" fill-opacity="0.7">p50 ms (log scale)</text>
  <g font-family="system-ui, sans-serif" font-size="11" fill="currentColor">
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
  <g font-family="system-ui, sans-serif" font-size="11" fill="currentColor" fill-opacity="0.7">
    <rect x="420" y="52" width="10" height="10" fill="#10b981" rx="1"/><text x="436" y="61">Prometheus</text>
    <rect x="420" y="70" width="10" height="10" fill="#ef4444" rx="1"/><text x="436" y="79">Postgres JSONB</text>
    <rect x="420" y="88" width="10" height="10" fill="#3b82f6" rx="1"/><text x="436" y="97">Postgres normalized + BRIN</text>
  </g>
</svg>

- **q1** is the biggest gap. Postgres needs `lag() OVER (PARTITION BY ...)`, which orders and walks every row, and JSONB adds a heap fetch per row. Prometheus reads a slice that was already sorted when written. Part of this gap is window size: Prometheus computes a 5-minute rate, Postgres walks all 45 minutes.
- **q3** surprised me. I expected percentiles to favor Postgres, but Prometheus still won at this scale. I wouldn't draw a conclusion until N is much larger.
- **q5** is a clean Postgres win. A short lookup on one series is a B-tree hit, and 0.14 ms is hard to beat.

## Under load

I ramped q1 from 10 to 2,000 requests per second with k6. All three systems eventually fell over, so this shows saturation, not latency.

| System | Completed | p95 |
|--------|-----------|-----|
| Prometheus | 169,841 | 1.8 s |
| PostgreSQL normalized | 4,894 | 5.1 s |
| PostgreSQL JSONB | 443 | 52.5 s |

<svg role="img" aria-labelledby="chart-break-title chart-break-desc" viewBox="0 0 640 230" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-break-title">Iterations completed over the whole ramp</title>
  <desc id="chart-break-desc">Horizontal bar chart of completed iterations: Prometheus 169,841, PostgreSQL normalized 4,894, PostgreSQL JSONB 443.</desc>
  <rect x="80" y="30" width="64" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="90" width="2" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="150" width="1" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Prometheus</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Postgres normalized</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Postgres JSONB</text>
  <text x="148" y="59" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">169,841</text>
  <text x="86" y="119" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">4,894</text>
  <text x="86" y="179" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">443</text>
</svg>

Prometheus served about 35× more queries than normalized Postgres and 383× more than JSONB.

## Where Postgres is the better choice

- Short, single-series lookups (q5).
- Percentiles with no sample cap. `percentile_cont` isn't bound by Prometheus's `--query.max-samples`, which matters at very large N.
- Joins. PromQL can't join metrics to customers or transactions; SQL can.
- Retention. Dropping a partition is O(1), while Prometheus has to rewrite blocks at the next compaction.

## What I'd say now

> PostgreSQL can store metrics, but every sample pays for a tuple header, index entries, WAL, and the label identity it has to restate. In my test that came to 30× to 69× more disk and up to about 290× slower on a counter-rate query, though part of that last gap is the wider window Postgres scanned. Prometheus avoids all of it: no UPDATE, no per-sample row, near-free compression. In return it is rigid. If you need joins, uncapped percentiles, or long retention next to business data, Postgres is a reasonable choice and sometimes the better one.

| Situation | Use |
|-----------|-----|
| Infrastructure monitoring, real-time alerts | Prometheus |
| High cardinality, disk cost matters | Prometheus |
| Metrics joined to business data, long retention | PostgreSQL |
| Ad-hoc SQL reporting, uncapped percentiles | PostgreSQL |

My interview answer was half right. Disk and `rate()` were worse on Postgres, as I said. But I blamed the index, and the data points at identity: the two Postgres schemas hold the same rows and still differ by 2.3×, which is the label document repeated on every row. And I didn't expect Postgres to win any shape, but it won one.

## Limitations

- The scale is small (1,600 series, 288,000 samples). It shows the shape of the costs, not how they grow, and q3 could flip.
- The run lasted 45 minutes with no compaction, so the disk figures are for head data.
- The engines don't always scan the same window (q1 is the clearest case), so latencies compare cost per shape, not identical work.
- Prometheus kept 0.56% fewer samples than Postgres, and I haven't found out why.
- I measured no CPU or RAM, and used `latest` images without recording digests.

The full code is in `benchctl/` at `github.com/hungpham10/my-benchmarks`; the numbers come from run `gh-232.1`.

*Follow-up: the same Head of Platform asked why the **application** database crawls. I benchmarked the filesystem, the sharding model and every built-in index type — [read it here](/blog/postgres-sharding-filesystem-en).*
