---
title: "Your Postgres isn't slow because of Postgres: filesystem, sharding and index type, benchmarked"
date: 2026-10-21
lastmod: 2026-10-21
tags: [postgresql, benchmark, sharding, index, filesystem]
excerpt: "After the metrics post, the same Head of Platform asked a harder question: if Postgres is fine for app data, why does it crawl in production? I benchmarked the three suspects nobody measures: the filesystem, the sharding model, and the index type."
---

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "Your Postgres isn't slow because of Postgres: filesystem, sharding and index type, benchmarked",
  "description": "After the metrics post, the same Head of Platform asked a harder question: if Postgres is fine for app data, why does it crawl in production? I benchmarked the three suspects nobody measures: the filesystem, the sharding model, and the index type.",
  "author": {
    "@type": "Person",
    "name": "Platform Engineering Team"
  },
  "publisher": {
    "@type": "Organization",
    "name": "Platform Engineering Blog"
  },
  "datePublished": "2026-10-21",
  "dateModified": "2026-10-21",
  "mainEntityOfPage": {
    "@type": "WebPage",
    "@id": "https://example.com/blog/postgres-sharding-index-filesystem"
  },
  "keywords": ["postgresql", "benchmark", "sharding", "index", "filesystem"],
  "articleSection": "Engineering"
}
</script>

<meta property="og:title" content="Your Postgres isn't slow because of Postgres: filesystem, sharding and index type, benchmarked">
<meta property="og:description" content="I benchmarked the three suspects nobody measures: the filesystem, the sharding model, and the index type.">
<meta property="og:type" content="article">
<meta property="og:article:published_time" content="2026-10-21">
<meta property="twitter:card" content="summary">

# Your Postgres isn't slow because of Postgres: filesystem, sharding and index type, benchmarked

In [the metrics benchmark](/blog/prometheus-vs-postgres-metrics) I concluded that PostgreSQL *can* store metrics, but every sample pays for a tuple header, index entries, WAL, and a repeated label document. The Head of Platform read it and asked the obvious follow-up:

> "Fine. Our app database isn't metrics. It's users, logins, orders. Why does that one crawl?"

I had opinions again. So I ran a second benchmark: one user table, four filesystems, four sharding models, eight index types, 10M rows by default and a 50M/100M sweep. Everything below is a number from that harness — and where I only have arithmetic rather than a measurement, I say so.

## The short version

- **A point lookup is 0.11–0.20 ms when routed to one shard table, and 4.9–19.9 s through a 32-branch `UNION ALL` view.** That is four orders of magnitude, from routing alone.
- **Sharding did nothing for writes.** One table vs 32 tables was flat at 10M, 50M and 100M rows: the WAL fsync lane is per instance, not per table.
- **Adding connections hurts after ~16.** Writes: p95 3 ms at 16 connections → 82 ms at 128 → 232 ms at 500, with throughput *falling*. The cause is one `WALWriteLock` and one `ProcArrayLock`.
- **Reads scale further, then hit the CPU.** 5.2k queries/s at 1 connection, 10.1k at 16, then 7.4k at 256 with p95 up 26×.
- **Index choice is worth 1 800×** on the shape it matches: BRIN answers a 7-day range in 1.7 ms where a sequential scan takes 3.1 s. The same BRIN index is 56 KB; a GiST index on the same table is 511 MB.
- **Filesystem changes the write bill 1.4–2× and index build time 1.3–3.4×**, but not the routing win.

## Setup

- **Hardware:** 4 vCPU, 8 GB, one container per leg, PostgreSQL `latest`, WAL on the filesystem under test, `shared_buffers` 1 GB, `max_connections` 600.
- **Filesystems:** ext4, xfs, btrfs, zfs, each on its own loop device with realistic mount options; every scenario runs on all four.
- **Schema:** `users(user_id bigint PK, email citext UNIQUE, name, created_at)`, 10M rows seeded server-side, then a closed-loop k6 insert phase (one small transaction per arrival) for 90 s.
- **Sharding models:**
  - **M0** — one table, one pool, 16 connections.
  - **M1** — pool-per-schema: P schemas, one table each, `user_id % P` picks the pool; P8 = 128 connections, P32 = 500.
  - **M2** — 32 tables `users_XX` behind one pool, 16 connections.
- **Index lab:** a separate 10M-row `docs` table with one access-method index per leg: `brin_time`, `btree_name`, `hash_rating`, `gist_point`, `spgist_point`, `spgist_name`, `gin_trgm`, plus `none`.

The executor is closed-loop, so throughput is capped by the slowest component rather than by a request generator. Absolute numbers move ±40% between runs on a shared runner; I only compare ratios measured inside the same leg.

## 1. The filesystem is a 2× write tax, not a mystery

Throughput for the plain insert loop at 10M rows, k6 closed loop, p95 in parentheses:

| Model (connections) | ext4 | xfs | btrfs | zfs |
|---|---|---|---|---|
| M0 — 1 table (16) | 6 157 (3 ms) | 4 793 (5 ms) | 4 484 (6 ms) | **8 862 (3 ms)** |
| M1-P8 — 128 | 5 028 (82 ms) | 4 243 (75 ms) | 4 239 (72 ms) | 7 230 (60 ms) |
| M1-P32 — 500 | 5 481 (189 ms) | 3 975 (227 ms) | 4 007 (232 ms) | 6 577 (160 ms) |
| M2 — 32 tables (16) | 6 757 (4 ms) | 4 571 (6 ms) | 4 283 (6 ms) | **8 915 (3 ms)** |

At 50M and 100M rows the spread stays:

| FS | 50M, 1 table | 50M, 32 tables | 100M, 1 table | 100M, 32 tables |
|---|---|---|---|---|
| ext4 | 7 168 | 6 659 | 6 854 | 6 612 |
| xfs | 4 632 | 4 519 | 4 523 | 4 568 |
| btrfs | 4 448 | 4 328 | 4 978 | 5 106 |

Two honest notes. First, zfs led the 10M matrix and did not lead the 50M/100M sweep — those are different runs on a noisy runner, so I will not crown a winner from it; what repeats is the *size* of the gap, 1.4–2×, not its direction. Second, this is a small-commit workload: one insert per transaction, `synchronous_commit=on`. That is exactly where filesystem commit behaviour shows up, and it is also why the spread is smaller than folklore suggests — group commit amortises most of the fsync cost.

Index build time shows the same filesystem effect more cleanly, because there is no concurrency to hide it:

| Index | build ext4 | build zfs |
|---|---|---|
| BRIN on `created_at` | 5.6 s | 16.7 s |
| GiST on `point` | 10.2 s | 35.9 s |
| btree on `name` | 24.6 s | 42.2 s |

Same bytes written, 1.3–3.4× the time on zfs. If your platform team argues about filesystems for a week, this table is the argument: it changes the constant, not the architecture.

Reads behave differently, and this is where the "zfs is fast" claim needs splitting:

| Read (winning index) | ext4 | xfs | btrfs | zfs |
|---|---|---|---|---|
| BRIN range, warm p50 | 1.71 ms | 1.70 ms | 2.50 ms | **1.36 ms** |
| BRIN range, cold p50 | — | 73.7 ms | 99.3 ms | **71.4 ms** |
| full seq scan, cold | — | **3 103 ms** | 3 164 ms | 4 062 ms |
| hash equality, warm p50 | 0.25 ms | 0.23 ms | 0.24 ms | **0.16 ms** |

Warm, zfs has the lowest median on every indexed shape — but a *worse* p95 on three of four (ARC keeps the median low and the tail fat). Cold, it is level with xfs on indexed reads and slower on a full heap scan, because the measured ZFS properties are stock: recordsize 128K, compression off. None of this matters next to the routing gap below (seconds), which is why I do not recommend choosing a filesystem for point reads.

## 2. Sharding: what it buys and what it doesn't

I expected table sharding to help writes. It didn't. At every size, 1 table vs 32 tables is within noise, and the reason is in the source: every commit ends in `XLogFlush` (`xlog.c:2780`), which takes the single instance-wide `WALWriteLock`; `ProcArrayEndTransaction` (`procarray.c:667`) takes `ProcArrayLock` exclusively per commit. Neither lock knows which table you wrote to. Splitting a table into 32 tables gives you 32 heaps and 32 btree roots — and exactly one fsync lane.

What sharding *does* buy is the read path, but only if the application routes:

| Read | Through `users_all` view | Routed to one shard |
|---|---|---|
| login, ext4 100M | 8 436 ms | **0.14 ms** |
| by_id, ext4 100M | 4 939 ms | **0.11 ms** |
| login, xfs 100M | 15 747 ms | **0.20 ms** |
| login, btrfs 100M | 14 657 ms | **0.13 ms** |

The routed number is flat from 50M to 100M rows. The view number grows with shard count, because `UNION ALL` over 32 tables runs 32 index scans and `LIMIT 1` cannot stop an Append. This is the single most important result in the whole benchmark: **sharding is a routing decision, not a PostgreSQL feature.** If your query layer does not know the shard key, sharding makes reads worse, not better.

Model M1 is the cautionary tale. It has 8–31× more connections than M0 and still loses 13–26% throughput with 20–60× worse p95. Pool-per-schema partitions namespace and relcache; it does not partition the commit path. More pools on one instance means more backends queueing on the same two locks.

## 3. Index types: each one wins exactly one shape

Same 10M-row table, one extra index per leg. The table below is the whole argument for "pick the index for the query, not for the column":

| Query shape | Winner | p50 / p95 | No index |
|---|---|---|---|
| 7-day range on `created_at` | **BRIN** | 1.7 / 4.7 ms | 3 086 / 3 130 ms |
| Equality on `rating` (~100 rows) | **hash** | 0.23 / 1.0 ms | 472 / 519 ms |
| kNN, `ORDER BY loc <-> point` | **GiST** | 0.41 / 1.24 ms | 1 117 / 1 172 ms |
| Prefix `LIKE 'User 1234%'` | **SP-GiST** | 0.22 / 0.83 ms | 0.30 / 638 ms |
| Substring `LIKE '%12345%'` | GIN (stable 4.2 ms) | 4.2 / 4.7 ms | 0.24 / 96 ms |

The footprint differences are just as large:

| Index | Size at 10M rows |
|---|---|
| BRIN | **56 KB** |
| GIN (trigram) | 178.6 MB |
| btree | 300.8 MB |
| hash | 306.6 MB |
| SP-GiST | 337–429 MB |
| GiST | 510.8 MB |

BRIN is 5 400× smaller than GiST because it stores block-range summaries instead of per-row entries. It is also nearly free to maintain: within noise of the no-index leg on ext4 and btrfs, and the cheapest index on every filesystem.

The write tax of the others is real but smaller than I expected — and it depends on the filesystem:

| Index | ext4 | xfs | zfs |
|---|---|---|---|
| BRIN | −0.6% | −1.1% | −4.4% |
| btree | −1.3% | −3.4% | +6.1% |
| GIN | −1.5% | −4.7% | −9.1% |
| SP-GiST (point) | −3.8% | −11.2% | −20.1% |

On ext4, even the worst access method costs under 4% of insert throughput, because the transaction still has to wait for one WAL flush either way. On zfs, index maintenance lands in the same window and costs 20%. "Indexes are expensive to write" is true, but the size of the bill is a property of the whole stack.

Two caveats I will not paper over:

- The prefix query is won by SP-GiST only because the btree leg was defined as `CREATE INDEX ON docs (name)`. Under a non-C collation a plain btree cannot serve `LIKE 'prefix%'`; it fell back to a scan (663 ms p95). With `text_pattern_ops` it would be competitive. That is an index definition detail, not an access-method verdict.
- The substring query does **not** prove GIN is faster. It has `LIMIT 100` and the table is ordered by id, so the sequential scan finds its first 100 matches after ~13k rows and exits in 0.2 ms; GIN pays a constant 4.2 ms. GIN is the only stable leg, but "stable" is not "faster". I am rerunning this shape with a pattern that matches late rows before making a claim.

## 4. How many connections is "enough"? Two different knees

This is the question I got wrong for years. The answer is not "more is better" or "fewer is better" — it is a knee, and the knee is in a different place for reads and writes.

**Writes**, from the matrix above:

| connections | throughput | p95 |
|---|---|---|
| 16 | 6 157–8 862 | 3–6 ms |
| 128 | 5 028–7 230 | 60–82 ms |
| 500 | 5 481–6 577 | 160–232 ms |

Throughput stops rising and then falls; latency grows roughly with queue length. The mechanism is the fsync convoy: one `WALWriteLock`, one flush leader at a time, and every extra backend is another customer in the same queue. Past ~16 backends on four cores you are not buying parallelism, you are buying wait time.

**Reads**, measured on a point lookup with 1/4/16/64/256 connections:

| connections | throughput | p50 | p95 |
|---|---|---|---|
| 1 | 5 165 q/s | 0 ms | 1 ms |
| 4 | 9 304 q/s | 0 ms | 1 ms |
| **16** | **10 060 q/s** | 1 ms | 3 ms |
| 64 | 9 484 q/s | 4 ms | 35 ms |
| 256 | 7 439 q/s | 28 ms | 78 ms |

Reads do scale — 2× from 1 to 16 connections — because they do not queue on a WAL flush. They queue on CPU. The knee is at 16 connections for 4 vCPU, i.e. `cores × 2–4`; past it, throughput drops 26% and p95 grows 26×.

<svg role="img" aria-labelledby="chart-knee-title chart-knee-desc" viewBox="0 0 620 220" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-knee-title">Point-read throughput against connection count</title>
  <desc id="chart-knee-desc">Bar chart of point-read throughput: 5,165 queries per second at 1 connection, 9,304 at 4, 10,060 at 16, 9,484 at 64, 7,439 at 256.</desc>
  <g font-family="system-ui, sans-serif" font-size="12" fill="currentColor">
    <line x1="80" y1="180" x2="590" y2="180" stroke="currentColor" stroke-opacity="0.25"/>
    <rect x="100" y="107" width="60" height="73" fill="#3b82f6" rx="2"/>
    <text x="130" y="99" text-anchor="middle">5 165</text>
    <text x="130" y="198" text-anchor="middle">1</text>
    <rect x="190" y="49" width="60" height="131" fill="#3b82f6" rx="2"/>
    <text x="220" y="41" text-anchor="middle">9 304</text>
    <text x="220" y="198" text-anchor="middle">4</text>
    <rect x="280" y="38" width="60" height="142" fill="#10b981" rx="2"/>
    <text x="310" y="30" text-anchor="middle">10 060</text>
    <text x="310" y="198" text-anchor="middle">16</text>
    <rect x="370" y="46" width="60" height="134" fill="#f59e0b" rx="2"/>
    <text x="400" y="38" text-anchor="middle">9 484</text>
    <text x="400" y="198" text-anchor="middle">64</text>
    <rect x="460" y="75" width="60" height="105" fill="#ef4444" rx="2"/>
    <text x="490" y="67" text-anchor="middle">7 439</text>
    <text x="490" y="198" text-anchor="middle">256</text>
    <text x="80" y="24" font-size="13">queries / second, 10M-row point lookup, 4 vCPU</text>
  </g>
</svg>

The practical rule: **size the pool from the knee, not from the number of application servers.** A connection pool that grows without a measured knee is a queue with extra steps. If you need more write capacity than one instance's fsync lane, the answer is another instance — a replica for reads, a shard for writes — not another 100 connections.

## What I got wrong while measuring

This post took four attempts because I kept measuring the wrong thing, and the wrong numbers looked plausible. I am keeping the list because it is the most reusable part:

1. **No `ANALYZE` after seeding.** A read-only leg never inserts, so autoanalyze never fires; the planner believed a 10M-row table was empty and chose sequential scans. Write legs hid the bug, because their inserts happened to trigger autoanalyze.
2. **`random()` in a `WHERE` clause.** `random()` is volatile; `is_pseudo_constant_clause` (`clauses.c:2113`) rejects volatile expressions, so no index can be used. Every "point lookup" was a sequential scan with `LIMIT 1`: 5.97 s per login, 12 queries in 60 s. Moving the randomisation into the client fixed it — 0.19 ms per login.
3. **A `citext` cast that never matched the index.** `citext → text` is an IMPLICIT cast, `text → citext` is only ASSIGNMENT, so `email = <text expression>` compares as text and misses the citext index. One `::citext` changes a 4.6 s scan into 0.2 ms.
4. **A scan benchmark that scanned nothing.** `now() - interval '1 day'` matched 13k of 10M rows; `count(*)` was answered by an index-only scan. Neither shape can show a parallel plan. The fix (365 days, `sum(user_id)`) produced a real scan — and an inconclusive worker result, which I am reporting as inconclusive.

## What I'd say now

> PostgreSQL is rarely the slow part. The filesystem changes write cost by 1.4–2×, sharding changes read cost by four orders of magnitude *if you route*, and the index type changes a query by up to 1 800×. Connection count is a knee, not a dial: past ~4× your cores, more connections buy queueing, and the WAL fsync lane makes the write knee arrive earlier and hurt more.
>
> If a Postgres is crawling, measure in this order: the query plan and its index, then the routing (is it scanning every shard?), then the connection count against the knee, and only then the filesystem. Most teams start at the last one.

| Symptom | First thing to check |
|---|---|
| Point lookups slow as data grows | Is the query routed, or scanning a UNION view? |
| Writes slow under load, latency spiky | Connection count vs fsync lane; p95, not average |
| One dashboard query slow | Index type vs shape (range → BRIN, kNN → GiST) |
| Everything 20–40% slower after a migration | Filesystem/commit behaviour — same bytes, different bill |
| Disk growing faster than rows | Index footprint: 56 KB vs 511 MB on the same column |

## Limitations

- 10M rows for the index lab, 50M/100M for the sharding sweep. The 200M point was dropped because ~80 GB exceeds the runner disk.
- 4 vCPU, 8 GB, one container per leg. Absolute throughput moves ±40% between runs; only within-leg ratios are evidence.
- The zfs 50M/100M leg was still running when this was written; the zfs numbers above are from the 10M matrix and the index lab.
- Btree height (3 levels at 10M, 4 at 100M) is computed from page fan-out, not measured with `pageinspect`.
- The prefix and substring shapes carry `LIMIT 100` on an id-ordered table, which lets a sequential scan exit early; see the caveats in section 3.
- The parallel-worker experiment is inconclusive at this size and CPU count. I am not claiming a win in either direction.
- The read sweep was run on ext4 only; filesystem effects on the read path are second-order here (routed reads were 0.11–0.20 ms on every FS).

The harness, scenarios and raw artifacts are in `benchctl/` at
[github.com/hungpham10/my-benchmarks](https://github.com/hungpham10/my-benchmarks);
the consolidated numbers and the list of runs are in
`benchctl/reports/2026-10-21-pg-index-shard-read.md`. The earlier metrics
benchmark is [here](/blog/prometheus-vs-postgres-metrics).
