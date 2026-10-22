---
title: "Make a Student Project's Postgres Handle Minimal Viable Production Sizing"
date: 2026-10-21
lastmod: 2026-10-22
lang: en
tags: [postgresql, benchmark, filesystem, sharding, index, storage]
author: Platform Engineering Team
excerpt: "One 4-vCPU box, 10 million users, about 10,000 reads a second. I tested the filesystem, the index type, the table layout and the connection pool, and most of the advice didn't survive measurement."
description: "One 4-vCPU box, 10 million users, about 10,000 reads a second. I tested the filesystem, the index type, the table layout and the connection pool, and most of the advice didn't survive measurement."
---

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "Make a Student Project's Postgres Handle Minimal Viable Production Sizing",
  "description": "One 4-vCPU box, 10 million users, about 10,000 reads a second. I tested the filesystem, the index type, the table layout and the connection pool, and most of the advice didn't survive measurement.",
  "inLanguage": "en",
  "author": {
    "@type": "Person",
    "name": "Platform Engineering Team"
  },
  "publisher": {
    "@type": "Organization",
    "name": "Platform Engineering Blog"
  },
  "datePublished": "2026-10-21",
  "dateModified": "2026-10-22",
  "mainEntityOfPage": {
    "@type": "WebPage",
    "@id": "https://example.com/blog/postgres-sharding-filesystem-en"
  },
  "keywords": ["postgresql", "benchmark", "filesystem", "xfs", "ext4", "btrfs", "zfs", "sharding", "index", "sizing"],
  "articleSection": "Engineering"
}
</script>

<meta property="og:title" content="Make a Student Project's Postgres Handle Minimal Viable Production Sizing">
<meta property="og:description" content="One 4-vCPU box, 10 million users, about 10,000 reads a second. I tested the filesystem, the index type, the table layout and the connection pool, and most of the advice didn't survive measurement.">
<meta property="og:type" content="article">
<meta property="og:article:published_time" content="2026-10-21">
<meta name="twitter:card" content="summary">

# Make a Student Project's Postgres Handle Minimal Viable Production Sizing

Imagine you're a second-year student and you've just built the backend for a class project. A users table, a sign-up endpoint, a login endpoint, Postgres on one small server. It works on your laptop. Then your friends start using it, and someone asks the question: would this survive real traffic?

That's what I wanted to answer, with numbers. Postgres has a lot of dials, and everyone has advice about them: put the data directory on XFS, split big tables, add an index, make the connection pool bigger. Most of that advice arrives with no measurements attached.

The first dial I got stuck on was the filesystem. Most people around me pick ext4, because it's the first filesystem you meet when you learn Linux and the default on most distros. The rest pick XFS, because they read somewhere, or a colleague told them, that XFS is what you use for databases. Ask why and almost nobody has a number. I didn't either, which is why this post exists.

So I measured. On one small box, I ran the same workload on four common filesystems, with every one of Postgres's built-in index types, under three different ways of arranging tables, and asked which setup actually performs best.

**What "minimum viable production" means here.** One server (4 vCPU, 8 GB RAM), one Postgres instance, a table of 10 million users. Writes are small insert transactions, roughly 4,000 to 9,000 rows a second depending on the setup. Reads are point lookups, around 10,000 queries a second at best. That's real traffic for a lot of small products. It is not a high-scale system, and I didn't test updates, bulk inserts, or anything that needs more than one machine.

Most of what people tell you to change turned out not to matter much. The one I spent the longest on, the filesystem, mattered least.

## The short version

- **Splitting a table into 32 on one instance does nothing for writes.** One table vs 32 tables stayed within noise at 10M, 50M and 100M rows, on every filesystem, in two separate rounds. A commit still goes through one `WALWriteLock` and one `ProcArrayLock`, and neither lock knows which table you wrote to.
- **Sharding helps reads only when the application routes to the right shard**, and the win shows up in tail latency and cold cache: warm p50 barely moves (under 0.1 ms), warm p95 improves 5–16×, cold reads improve 39–120×.
- **An early four-orders-of-magnitude read gap was a harness artifact**, not a sharding benefit: no `ANALYZE` after seeding, plus `random()` in `WHERE`, turned every branch of the view into a sequential scan.
- **No filesystem was reliably fastest.** The same filesystem on the same scenario moved 1.95× between two rounds (btrfs 4,448 → 8,656 rows/s), which is more than the 1.4–1.9× gap between filesystems.
- **XFS isn't slow, but nothing here shows it beating ext4** on this workload. All four land within about 2× of each other.
- **An fsync microbenchmark doesn't predict write throughput.** btrfs had the slowest single fsync (561 µs) and still wrote 66% faster than XFS in round 2.
- **Connection count is a knee, not a dial.** Writes peaked at 16 connections and p95 grew to 232 ms at 500 while throughput fell. Reads hit their knee at about 16 connections on 4 vCPU.
- **Index choice is worth up to 1,800×** on the right query shape: BRIN answers a 7-day range in 1.7 ms where a sequential scan takes 3.1 s.
- **The whole setup fits in one table**: one `users` table, one pool of ~16 connections, one index per query shape, ext4 or XFS, and `ANALYZE` after seeding. That's the "minimum viable production" answer; the caveats are at the end.

## What I'm going to measure

Three things, in this order.

**1. Postgres's built-in index types.** btree (the default), hash, BRIN, GiST, SP-GiST and GIN. Each one is built for a different query shape, so each gets the shape it's meant for: a time range for BRIN, equality for hash, nearest-neighbour for GiST, prefix matching for SP-GiST, substring search for GIN, and name lookups for btree. For every shape I also ran the same query with no index, to see what the index buys.

**2. Three ways of arranging tables.** Same instance, same data, same 10M rows, same 90 seconds. Only the layout and the connection pools change. The schema-sharded model appears twice, with 8 and 32 pools, which is why the table has four rows for three models.

| Model | Layout | Connections | Real-world situation | Question it answers |
|---|---|---|---|---|
| **M0** | 1 `users` table, 1 pool | 16 | The "just use Postgres" app: one table, the ORM points at it. Baseline. | What happens with no sharding? |
| **M1-P8** | 8 schemas, one table each, one pool each; `user_id % 8` picks the pool | 128 | Split by tenant or service: each group gets its own pool, expecting more pools to mean parallel writes. | Do more pools and connections raise throughput? |
| **M1-P32** | 32 schemas, one table each, one pool each | 500 | The same mechanism pushed to 32 pools, to see whether it keeps scaling. | And with even more? |
| **M2** | 32 tables `users_00`…`users_31` in 1 schema, 1 pool; the application hashes `user_id` to a suffix | 16 | Application-level sharding: split the table so each one is smaller, still one instance and one pool. | Does splitting tables help before you split instances? |

M0 is the baseline. M2 is what you do when you want smaller tables but can't add instances yet. M1 is what teams do when they split by tenant or service and give each group its own pool. Because M1 raises the connection count 8 to 31 times at the same moment it splits the schemas, its result says more about connections than about sharding. I'll say so again when we get there.

**3. Four filesystems.** ext4, XFS, btrfs and ZFS, each running everything above.

### How the runs were set up

**Hardware and workload.** Every run is one PostgreSQL container on 4 vCPU and 8 GB RAM, `shared_buffers` at 1 GB, with the WAL on the filesystem under test. The layout experiments use `users(user_id, email, name, created_at)`, seeded with 10 million rows (50M and 100M for the scale runs), then k6 hammers it with inserts for 90 seconds, one small transaction at a time. The index experiments use a separate 10M-row `docs` table, with exactly one index type per run.

On the version: the containers ran the `latest` image and I didn't record the digest, so there's no exact server version to quote. For the source walk further down I used the PostgreSQL 18.6 tree in `sources/postgres`, so the line numbers I cite are 18.6 line numbers.

**Filesystems.** All mounted with `noatime` and no compression, because with compression on I'd be benchmarking the compressor.

There's a trap here that I nearly fell into: a container can't format a filesystem. To test XFS you have to create it on the Docker host and bind-mount it into the container. I used a sparse file attached to a loop device with `--direct-io=on`. Without that flag, the host's page cache swallows every write and the filesystem under test never touches the disk. You end up benchmarking the host's RAM.

This costs two things. First, every run lands on a fresh runner with a different backing disk, which is the largest source of noise in this whole post. Second, **ext4 ran natively, without a loop device**, while the other three did not. So ext4 can't be compared directly with the rest, and it has no cold-read numbers. I'll repeat that every time ext4 and XFS appear in the same sentence.

**Warm and cold reads.** Postgres reads 8 KB pages, and a page can already sit in `shared_buffers` or in the OS page cache. If it does, the read never touches the disk.

- *Warm*: run the workload once so the data is in RAM, then measure. This tells you whether the plan and the routing are any good. It tells you nothing about the filesystem, because the disk isn't involved.
- *Cold*: restart the container, drop the host page cache, force reads down to the disk. This is the only measurement that can answer "is this filesystem fast at reading". Each query shape gets its own cache reset, because a single sequential scan warms the whole table.

ZFS is the exception. `drop_caches` doesn't clear the ARC (ZFS's own cache), so "cold" isn't reproducible there: the same query measured 71 ms in one run and 743 ms in another. Treat ZFS cold numbers as an upper bound.

**How to read the numbers.** The load generator is closed-loop, so throughput is capped by the slowest component. On a shared runner, absolute numbers move about ±40% between runs, so I only compare ratios within a single run, unless I say a comparison is across two rounds.

### What was measured, and what wasn't

This is not a full matrix, and I'd rather say so now than let you find out. The index experiments ran on a single table (M0). The layout experiments ran on `users` with only its primary key and unique email index. I never crossed the two, so there is no cell like "GiST × 32 tables × XFS". Filesystem coverage is uneven too:

| Experiment | ext4 | XFS | btrfs | ZFS |
|---|---|---|---|---|
| Index sizes (10M) | ✓ | ✓ | ✓ | ✓ |
| Index build time (10M) | ✓ | ✓ | ✓ | ✓ (128K and 8k) |
| Index lookups (10M) | ✓ | ✓ | ✓ | ✓ |
| Write cost per index type | ✓ | ✓ | BRIN only | ✓ (128K and 8k) |
| Writes, M0/M1/M2 at 10M | ✓ | ✓ | ✓ | ✓ |
| Writes, 1 vs 32 tables at 50M/100M (two rounds) | ✓ | ✓ | ✓ | 50M only, one round |
| Warm reads, view vs routed at 50M | ✓ | ✓ | ✓ | ✓ |
| Cold reads at 50M | none | ✓ | ✓ | ✓ (unstable) |
| `pg_test_fsync`, `pg_stat_io` | none | ✓ | ✓ | ✓ |
| Read connection sweep | ✓ | — | — | — |

The index *sizes* are identical on all four filesystems, because they're data, not layout. The index *lookup* numbers below were measured on XFS; on the other three filesystems the same shapes stayed within ±40%. If you need a conclusion about a combination that isn't in this table, you'll have to measure it yourself.

## Part 1: Index types

Same `docs` table, 10M rows, one extra index per run. The whole argument for "pick the index for the query, not for the column" fits in one table. p50 / p95 in ms, measured on XFS:

| Query shape | Winner | p50 / p95 | No index (p50 / p95) |
|---|---|---|---|
| 7-day range on `created_at` | BRIN | 1.7 / 4.7 ms | 3,086 / 3,130 ms |
| Equality on `rating` (~100 rows) | hash | 0.23 / 1.0 ms | 472 / 519 ms |
| kNN, `ORDER BY loc <-> point` | GiST | 0.41 / 1.24 ms | 1,117 / 1,172 ms |
| Prefix `LIKE 'User 1234%'` | SP-GiST | 0.22 / 0.83 ms | 0.30 / 638 ms |
| Substring `LIKE '%12345%'` | GIN | 4.2 / 4.7 ms | 0.24 / 96 ms |

The range query is the headline: BRIN answers in 1.7 ms where a sequential scan takes 3.1 seconds, a ratio of about 1,800. Keep that number in mind, because nothing else in this post comes close to it.

Index size at 10M rows tells a second story:

| Index | Size |
|---|---|
| BRIN | 56 KB |
| GIN (trigram) | 178.6 MB |
| btree | 300.8 MB |
| hash | 306.6 MB |
| SP-GiST | 337–429 MB |
| GiST | 510.8 MB |

BRIN on `created_at` is more than 9,000 times smaller than the GiST index on `point` (56 KB against 511 MB), because it stores one summary per block range instead of one entry per row. It's also nearly free to maintain: within noise of the no-index run on ext4 and btrfs.

The write tax of the other index types is real, but smaller than I expected, and it depends on the filesystem. Change in insert throughput compared with no index:

| Index | ext4 | XFS | ZFS (128K) | ZFS (8k) |
|---|---|---|---|---|
| BRIN | −0.6% | −1.1% | −4.4% | +0.3% |
| btree | −1.3% | −3.4% | +6.1% | −0.5% |
| GIN | −1.5% | −4.7% | −9.1% | −1.4% |
| SP-GiST (point) | −3.8% | −11.2% | −20.1% | −4.4% |
| GiST (point) | −3.5% | −6.2% | −13.3% | −9.5% |

On ext4 even the worst access method costs under 4% of insert throughput, because the transaction still has to wait for one WAL flush either way. On ZFS at the default 128K record size, index maintenance competes for the same window and costs up to 20%; at 8k the worst case drops to 9.5%. "Indexes are expensive to write" is true, but how expensive depends on the whole stack, filesystem included. (The `+6.1%` for btree on ZFS 128K is inside the noise. Read it as "no measurable cost".)

Two caveats I won't paper over:

- **SP-GiST wins the prefix query partly because of how I defined the btree.** It was a plain `CREATE INDEX ON docs (name)`, and under a non-C collation a plain btree can't serve `LIKE 'prefix%'`, so it fell back to a scan (663 ms p95). With `text_pattern_ops` it would be competitive. That's an index definition detail, not a verdict on the access method.
- **The substring row doesn't prove GIN is faster.** The query has `LIMIT 100` and the table is ordered by id, so the sequential scan finds its first 100 matches after about 13,000 rows and exits in 0.2 ms. GIN pays a constant 4.2 ms. GIN is the only variant that's *stable*, but stable isn't the same as faster.

## Part 2: Table layouts

### Does splitting a table speed up writes?

Before running anything, I was sure it would. Split one table into 32 and you get 32 heaps, 32 btrees, less page contention. It sounds like it has to be faster. Results at 10M rows, k6 closed loop, rows/s with p95 in parentheses:

| Model (connections) | ext4 | XFS | btrfs | ZFS |
|---|---|---|---|---|
| M0 — 1 table (16) | 6,157 (3 ms) | 4,793 (5 ms) | 4,484 (6 ms) | 8,862 (3 ms) |
| M1-P8 — 128 | 5,028 (82 ms) | 4,243 (75 ms) | 4,239 (72 ms) | 7,230 (60 ms) |
| M1-P32 — 500 | 5,481 (189 ms) | 3,975 (227 ms) | 4,007 (232 ms) | 6,577 (160 ms) |
| M2 — 32 tables (16) | 6,757 (4 ms) | 4,571 (6 ms) | 4,283 (6 ms) | 8,915 (3 ms) |

| Model | ext4 | XFS | btrfs | ZFS |
|---|---|---|---|---|
| **M0** (baseline) | 6,157 | 4,793 | 4,484 | 8,862 |
| **M2** — 32 tables | 6,757 (+9.7%) | 4,571 (−4.6%) | 4,283 (−4.5%) | 8,915 (+0.6%) |
| **M1-P8** — 128 | 5,028 (−18.3%) | 4,243 (−11.5%) | 4,239 (−5.5%) | 7,230 (−18.4%) |
| **M1-P32** — 500 | 5,481 (−11.0%) | 3,975 (−17.1%) | 4,007 (−10.6%) | 6,577 (−25.8%) |

*Rows/s at 10M rows; the percentage is the change relative to that filesystem's M0.*

M2 is almost the same as M0. On ext4 it's 10% ahead, on ZFS 0.6% ahead, on XFS and btrfs slightly behind, and p95 barely moves. Splitting does shuffle a bit of page contention, but it changes nothing at the place that actually limits you.

M1 is worse than M0, even with 8 to 31 times as many connections: 5 to 26% lower throughput depending on the filesystem, and a p95 more than ten times worse, over 60 times in one case. A pool per schema partitions the namespace, the relcache and the pools. It doesn't partition the commit path. More pools just means more people queueing at the same door.

### Why: there's one fsync lane

I read the Postgres source to find where that door is. Every commit goes through `RecordTransactionCommit` and ends in `XLogFlush`, which takes the instance-wide `WALWriteLock`. One backend becomes the leader and flushes the WAL for the whole group while the others wait for its result instead of flushing themselves. In parallel, `ProcArrayEndTransaction` takes `ProcArrayLock` exclusively on every commit. Neither lock knows which table you just wrote to.

> **Note — source walk via MCP codegraph.** I bound the PostgreSQL tree at `sources/postgres` to the codegraph MCP (`codegraph_init`, `index=false`) and followed the call graph; every location below was re-checked with a direct grep. `CommitTransaction` (`xact.c:2228`) calls `RecordTransactionCommit()` (`xact.c:2365`), which ends in `XLogFlush(XactLastRecEnd)` (`xact.c:1502`). Inside `XLogFlush` (`xlog.c:2780`) the flush is gated by a single `WALWriteLock`; line 2854 uses `LWLockAcquireOrWait(WALWriteLock, LW_EXCLUSIVE)`, so one backend becomes the leader and the rest wait for its result. The comment right above says the point is to keep "a good rate of group committing". `codegraph_callers(XLogFlush)` returns 27 callers — commit, checkpoint, buffer flush, walsender and others — and none of them knows the target table. In parallel, `CommitTransaction` calls `ProcArrayEndTransaction` (`xact.c:2389`), which takes `ProcArrayLock` exclusively (`procarray.c:667`; branches at lines 684 and 716). Neither lock path sees a table name. These line numbers hold for PostgreSQL 18.6, the tree in `sources/postgres`.

So splitting one table into 32 gives you 32 heaps, 32 btree roots, and still exactly one fsync lane.

### Again at 50M and 100M

I ran the whole thing a second time at 50M and 100M, in two rounds a few hours apart. A round here means one complete sweep — every filesystem and both table layouts, run in one sitting on one runner. The runner, and the disk behind it, change between rounds. I'm not using this table to rank filesystems. I'm using it to see whether one table and 32 tables differ:

| FS | round | 50M, 1 table | 50M, 32 tables | 100M, 1 table | 100M, 32 tables |
|---|---|---|---|---|---|
| ext4 | 1 | 7,168 | 6,659 | 6,854 | 6,612 |
| ext4 | 2 | 4,898 | 4,869 | 5,006 | 5,047 |
| XFS | 1 | 4,632 | 4,519 | 4,523 | 4,568 |
| XFS | 2 | 5,218 | 5,880 | 5,757 | 5,089 |
| btrfs | 1 | 4,448 | 4,328 | 4,978 | 5,106 |
| btrfs | 2 | 8,656 | 8,739 | 8,899 | 8,473 |
| ZFS (`recordsize=8k`) | 2 | 8,909 | 8,895 | not measured | not measured |

Within each row, the gap between one table and 32 tables is at most about 13%, and it goes both ways. That 13% is XFS round 2 at 50M (5,218 against 5,880) — the largest within-row gap in the table, and still smaller than the 1.95× swing between rounds of the same filesystem. Every other row is tighter. For this workload, splitting a table on one instance doesn't buy write throughput.

**Scope of that conclusion.** I only measured inserts in small transactions, with `synchronous_commit` on, 4 vCPU, and the WAL on the same disk as the data. For bulk inserts, heavy UPDATE traffic, or vacuum and bloat, partitioning may help in ways this post doesn't measure.

### Connections: writes and reads break at different points

The write table above already shows it: 16 connections gives the best throughput, while 128 and 500 make throughput *fall* and p95 grow with the queue. On four cores, past roughly 16 writers you aren't buying parallelism, you're buying wait time.

Reads behave differently, because they queue on CPU rather than on a WAL flush. I swept 1, 4, 16, 64 and 256 connections of point lookups on the 10M-row table (ext4 only):

| Connections | Throughput (q/s) | p95 |
|---|---|---|
| 1 | 5,165 | 1 ms |
| 4 | 9,304 | 1 ms |
| 16 | 10,060 | 3 ms |
| 64 | 9,484 | 35 ms |
| 256 | 7,439 | 78 ms |

<svg role="img" aria-labelledby="chart-knee-title chart-knee-desc" viewBox="0 0 640 220" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-knee-title">Point-read throughput against connection count</title>
  <desc id="chart-knee-desc">Bar chart of point-read throughput: 5,165 queries per second at 1 connection, 9,304 at 4, 10,060 at 16, 9,484 at 64 and 7,439 at 256.</desc>
  <g font-family="system-ui, sans-serif" font-size="12" fill="currentColor">
    <line x1="80" y1="180" x2="600" y2="180" stroke="currentColor" stroke-opacity="0.25"/>
    <rect x="100" y="107" width="60" height="73" fill="#3b82f6" rx="2"/>
    <text x="130" y="99" text-anchor="middle">5,165</text>
    <text x="130" y="198" text-anchor="middle">1</text>
    <rect x="195" y="49" width="60" height="131" fill="#3b82f6" rx="2"/>
    <text x="225" y="41" text-anchor="middle">9,304</text>
    <text x="225" y="198" text-anchor="middle">4</text>
    <rect x="290" y="38" width="60" height="142" fill="#10b981" rx="2"/>
    <text x="320" y="30" text-anchor="middle">10,060</text>
    <text x="320" y="198" text-anchor="middle">16</text>
    <rect x="385" y="46" width="60" height="134" fill="#f59e0b" rx="2"/>
    <text x="415" y="38" text-anchor="middle">9,484</text>
    <text x="415" y="198" text-anchor="middle">64</text>
    <rect x="480" y="75" width="60" height="105" fill="#ef4444" rx="2"/>
    <text x="510" y="67" text-anchor="middle">7,439</text>
    <text x="510" y="198" text-anchor="middle">256</text>
    <text x="80" y="22" font-size="13">queries / second, 10M-row point lookup, 4 vCPU</text>
  </g>
</svg>

Reads scale about 2× from 1 to 16 connections and then start to fall. The knee sits near 16 connections on 4 vCPU, roughly `cores × 2–4`. Size the pool from the knee, not from the number of app servers. If you need more write capacity than one fsync lane allows, you need another instance, not another 100 connections.

### Reads: why the first sweep was wrong

The first read sweep showed a four-order-of-magnitude gap: 4.9 to 19.9 seconds through a 32-branch `UNION ALL` view, against 0.11 to 0.20 ms for a routed query. I published that gap before I found the cause, and the cause was the harness, not sharding. A read-only run never inserts, so autoanalyze never fired, and the planner treated the 10M-row table as empty. `random()` in `WHERE` is volatile, so no index could be used and every "point lookup" was a sequential scan. The routed branch also used a literal id, which turned the comparison into an index lookup against 32 sequential scans. Fixing both — `ANALYZE`, and randomisation moved to the client — gives the numbers below.

Both paths hit the same 32 tables. Each table holds one shard, with identical columns and indexes:

```sql
CREATE TABLE users_XX (              -- XX = 00 … 31
    user_id    bigint      PRIMARY KEY,
    email      citext      NOT NULL,
    name       text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON users_XX (email);
CREATE INDEX        ON users_XX (created_at);

CREATE VIEW users_all AS
    SELECT * FROM users_00
    UNION ALL SELECT * FROM users_01
    -- … 30 more branches …
    UNION ALL SELECT * FROM users_31;
```

The **view** path sends the statement to `users_all`, and the planner expands all 32 branches. The **routed** path computes the shard in the client and sends the same statement to one table. The probe substitutes a literal id, not a bind parameter. The example below uses one mid-population id, `5,000,000`, which lands in `users_00`:

```sql
-- view: one statement, 32 branches in the plan
SELECT user_id, name FROM users_all
 WHERE email = 'user5000000@example.com' LIMIT 1;

-- routed: 5000000 % 32 = 0, so one table
SELECT user_id, name FROM users_00
 WHERE email = 'user5000000@example.com' LIMIT 1;
```

`by_id` is the same pair with `WHERE user_id = 5000000` instead of the email predicate. Both sides use the same unique email index and the same primary key, so the only difference is the 32-branch `Append`.

50M rows, 32 tables, 16 connections, p50 / p95 in ms, warm cache:

| FS | view `login` | routed `login` | view `by_id` | routed `by_id` |
|---|---|---|---|---|
| ext4 | 0.26 / 9.3 | 0.18 / 1.3 | 0.25 / 2.3 | 0.17 / 0.8 |
| XFS | 0.17 / 7.2 | 0.15 / 1.1 | 0.17 / 1.6 | 0.13 / 0.9 |
| btrfs | 0.11 / 6.2 | 0.07 / 1.2 | 0.11 / 1.3 | 0.06 / 0.7 |
| ZFS | 0.11 / 30.9 | 0.07 / 1.9 | 0.10 / 1.7 | 0.07 / 1.2 |

<svg role="img" aria-labelledby="chart-reads-title chart-reads-desc" viewBox="0 0 640 240" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-reads-title">Warm p95 latency: 32-branch view against routed read</title>
  <desc id="chart-reads-desc">Grouped bar chart of warm p95 latency for the login query at 50 million rows. ext4: 9.3 milliseconds through the view and 1.3 routed. XFS: 7.2 and 1.1. btrfs: 6.2 and 1.2. ZFS: 30.9 and 1.9.</desc>
  <g font-family="system-ui, sans-serif" font-size="12" fill="currentColor">
    <line x1="60" y1="190" x2="610" y2="190" stroke="currentColor" stroke-opacity="0.25"/>
    <rect x="82" y="141" width="34" height="49" fill="#ef4444" rx="2"/>
    <rect x="120" y="183" width="34" height="7" fill="#10b981" rx="2"/>
    <text x="99" y="133" text-anchor="middle" font-size="10">9.3</text>
    <text x="137" y="179" text-anchor="middle" font-size="10">1.3</text>
    <text x="118" y="208" text-anchor="middle">ext4</text>
    <rect x="222" y="154" width="34" height="36" fill="#ef4444" rx="2"/>
    <rect x="260" y="184" width="34" height="6" fill="#10b981" rx="2"/>
    <text x="239" y="146" text-anchor="middle" font-size="10">7.2</text>
    <text x="277" y="180" text-anchor="middle" font-size="10">1.1</text>
    <text x="258" y="208" text-anchor="middle">XFS</text>
    <rect x="362" y="159" width="34" height="31" fill="#ef4444" rx="2"/>
    <rect x="400" y="184" width="34" height="6" fill="#10b981" rx="2"/>
    <text x="379" y="151" text-anchor="middle" font-size="10">6.2</text>
    <text x="417" y="180" text-anchor="middle" font-size="10">1.2</text>
    <text x="398" y="208" text-anchor="middle">btrfs</text>
    <rect x="502" y="35" width="34" height="155" fill="#ef4444" rx="2"/>
    <rect x="540" y="181" width="34" height="9" fill="#10b981" rx="2"/>
    <text x="519" y="27" text-anchor="middle" font-size="10">30.9</text>
    <text x="557" y="177" text-anchor="middle" font-size="10">1.9</text>
    <text x="538" y="208" text-anchor="middle">ZFS</text>
    <rect x="60" y="16" width="10" height="10" fill="#ef4444" rx="1"/>
    <text x="76" y="25" font-size="11">32-branch view</text>
    <rect x="180" y="16" width="10" height="10" fill="#10b981" rx="1"/>
    <text x="196" y="25" font-size="11">routed read</text>
    <text x="60" y="232" font-size="11" fill-opacity="0.7">p95, ms, warm cache, 50M rows</text>
  </g>
</svg>

And cold, same workload (ext4 has no row here, since it ran natively and the cold phase needs the loop device):

| Cold, 50M, p50 (ms) | view `login` | routed `login` |
|---|---|---|
| XFS | 132.8 | 1.11 |
| btrfs | 78.6 | 1.40 |
| ZFS (unstable) | 69.0 | 1.77 |

Reading these tables:

- **Warm p50 barely differs.** The view is less than 0.1 ms slower than a routed read on every filesystem. Once statistics exist, each branch of the view is an index scan, so the cost is one extra plan node, not 32 scans.
- **Warm p95: routing is 5 to 16 times better for `login`** (1.1 to 1.9 ms against 6.2 to 30.9 ms). The tail comes from the branches that don't contain the row, and from ARC on ZFS. For `by_id` the gap is much smaller.
- **Cold: routing is 39 to 120 times better**, because the view touches 32 index roots instead of one.
- **It's fan-out, not index size.** Each shard's index is roughly 1/32 the size, and the total footprint stays about the same. What changes is how many index roots a query must touch: one when routed, 32 through the view. When cold, that is 32 random reads instead of one.
- **Routing assumes the caller knows the shard.** The routed leg knew it because the harness computed `uid % 32`. A login by email does not: the app has no idea which shard holds that address, so it needs a lookup table (`email → user_id`) or a shard key that matches the query.
- **Cold isn't only a restart effect.** Once the working set no longer fits in RAM, a share of reads behaves like the cold row every day. At 50M rows on an 8 GB box, that is the realistic steady state, not the exception — which is why routing is worth it there, and not at 10M where the table stays cached.
- Routed reads don't slow down from 50M to 100M (ext4 0.18 → 0.18, XFS 0.15 → 0.13, btrfs 0.07 → 0.08).

The practical lesson: a `UNION ALL` view over shards is cheap when the planner has statistics and the predicate can use an index, and a disaster when it doesn't. Before you rewrite the app for routing, run `EXPLAIN (ANALYZE, BUFFERS)` on that exact query. If the plan is 32 sequential scans, the problem is statistics or query shape, not sharding.

## Part 3: Filesystems

This is the part I started the project for, and it's the part where the data says the least.

### The same test, twice, a few hours apart

Same scenario, same filesystem, same commit, 50M rows, one table, two rounds:

| FS | Round 1 | Round 2 | Change |
|---|---|---|---|
| ext4 | 7,168 | 4,898 | −32% |
| XFS | 4,632 | 5,218 | +13% |
| btrfs | 4,448 | 8,656 | +95% |
| ZFS | — | 8,909 | — |

<svg role="img" aria-labelledby="chart-noise-title chart-noise-desc" viewBox="0 0 640 230" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-noise-title">Round-to-round noise on the same filesystem</title>
  <desc id="chart-noise-desc">Grouped bar chart of 50-million-row write throughput in two rounds. ext4: 7,168 rows per second then 4,898. XFS: 4,632 then 5,218. btrfs: 4,448 then 8,656. The same test moved up to 1.95 times between rounds.</desc>
  <g font-family="system-ui, sans-serif" font-size="12" fill="currentColor">
    <line x1="60" y1="185" x2="610" y2="185" stroke="currentColor" stroke-opacity="0.25"/>
    <rect x="105" y="53" width="50" height="132" fill="#3b82f6" rx="2"/>
    <rect x="160" y="95" width="50" height="90" fill="#f59e0b" rx="2"/>
    <text x="130" y="45" text-anchor="middle" font-size="10">7,168</text>
    <text x="185" y="87" text-anchor="middle" font-size="10">4,898</text>
    <text x="157" y="203" text-anchor="middle">ext4</text>
    <rect x="285" y="100" width="50" height="85" fill="#3b82f6" rx="2"/>
    <rect x="340" y="89" width="50" height="96" fill="#f59e0b" rx="2"/>
    <text x="310" y="92" text-anchor="middle" font-size="10">4,632</text>
    <text x="365" y="81" text-anchor="middle" font-size="10">5,218</text>
    <text x="337" y="203" text-anchor="middle">XFS</text>
    <rect x="465" y="103" width="50" height="82" fill="#3b82f6" rx="2"/>
    <rect x="520" y="25" width="50" height="160" fill="#f59e0b" rx="2"/>
    <text x="490" y="95" text-anchor="middle" font-size="10">4,448</text>
    <text x="545" y="17" text-anchor="middle" font-size="10">8,656</text>
    <text x="517" y="203" text-anchor="middle">btrfs</text>
    <rect x="60" y="12" width="10" height="10" fill="#3b82f6" rx="1"/>
    <text x="76" y="21" font-size="11">round 1</text>
    <rect x="150" y="12" width="10" height="10" fill="#f59e0b" rx="1"/>
    <text x="166" y="21" font-size="11">round 2</text>
    <text x="60" y="224" font-size="11" fill-opacity="0.7">rows/s, 50M rows, one table</text>
  </g>
</svg>

Seed time moved with it: the same 50M rows took 950 s on ext4 in one round and 1,137 s in the other, and 872 s versus 628 s on btrfs. That's the tell. Each run gets a fresh runner with a different disk behind it, so I'm partly measuring the runner. The gaps *between* filesystems are 1.4 to 1.9×, and the measurement itself swings 1.95×. With one run per cell, those gaps can't be resolved.

What can be said:

- All four filesystems land within about 2× of each other. None is 10× faster, and none is a mistake.
- In round 2, ZFS led, btrfs was close behind, then XFS, then ext4. In round 1, ext4 led. There is no stable ranking.
- ZFS is the only one that came out nearly identical every time (8,862 and 8,915 at 10M, 8,909 and 8,895 at 50M). Four points from different runners still aren't proof, and the 10M pair used the 128K record size while the 50M pair used 8k.

### So is XFS faster than ext4?

That's the question I started with, so here's the direct answer: I found no evidence that XFS beats ext4 on this workload, and none for the reverse either. At 10M, ext4 led the table at 6,157 rows/s against 4,793 for XFS — but ext4 was the one filesystem that ran without a loop device, so that comparison isn't fair either. At 50M and 100M the order flipped between rounds.

There are things XFS may well do better that I didn't test: many concurrent writers beyond 4 vCPU, very large files, online growth with `xfs_growfs`, reflinks, and behaviour over hours rather than 90 seconds. If someone has numbers for those, I'd like to see them.

### An fsync benchmark doesn't predict throughput

Since commit cost is the bottleneck for this workload, I expected `pg_test_fsync` to explain the differences. It didn't. One 8 KB write plus fsync, next to what Postgres itself measured during the 90-second run, next to round 2 throughput:

| FS | `pg_test_fsync` (1×8KB) | `pg_stat_io` average fsync during the run | Write throughput (round 2) |
|---|---|---|---|
| XFS | 393 µs | 3.7 ms | 5,218 rows/s |
| btrfs | 561 µs | 3.5 ms | 8,656 rows/s |
| ZFS | 210 µs | 2.3 ms | 8,909 rows/s |
| ext4 | not measurable (native) | not measurable | 4,898 rows/s |

btrfs had the slowest single fsync and still wrote 66% faster than XFS. My guess is group commit: filesystems may differ in how many commits get folded into one flush, which a single-fsync microbenchmark can't see. I haven't verified that. `pg_stat_wal` would show it, and I didn't capture it. Also remember that these fsync numbers go through a loop device with direct I/O, so they include that layer.

Either way, the practical point stands: you can't pick a filesystem for a small-commit workload from an fsync microbenchmark, not even PostgreSQL's own. Measure the workload.

### Index builds, where the filesystem shows through

Builds have no concurrency to hide behind, so the times are cleaner. 10M rows:

| Index | ext4 | XFS | btrfs | ZFS (128K) | ZFS (8k) |
|---|---|---|---|---|---|
| BRIN on `created_at` | 5.6 s | 5.5 s | 5.6 s | 16.7 s | 19.4 s |
| GiST on `point` | 10.2 s | 10.5 s | 12.1 s | 35.9 s | 24.7 s |
| btree on `name` | 24.6 s | 25.8 s | 25.7 s | 42.2 s | 31.1 s |
| GIN trigram on `name` | 15.6 s | 15.8 s | 17.5 s | 33.2 s | 19.9 s |

ext4, XFS and btrfs build these indexes within about 20% of each other (GiST is the widest: 10.2 s against 12.1 s on btrfs). The filesystem only shows through on ZFS, where the untuned 128K recordsize pays 1.7–3.5× the ext4 build time for the same bytes. The 8k record size closes much of that gap for the metadata-heavy builds (btree and GIN at about 1.3× ext4, GiST still 2.4×), but that was a separate run, so treat it as indicative rather than a controlled comparison. BRIN stays 3.0 to 3.5× ext4 either way, since it's a fixed cost with almost no I/O to amortise. So it isn't "ZFS builds slowly". It's "ZFS builds slowly for the index type and record size you configured".

### ZFS: the default record size is a trap

A ZFS dataset defaults to `recordsize=128K`. Postgres writes 8 KB pages, so every dirty page can force a read-modify-write of a whole 128 KB record, up to 16× amplification once the working set no longer fits in ARC. In my case the 50M/100M run died after four hours, when the runner ran out of disk and couldn't even write its own log (`No space left on device`).

`recordsize=8k` fixes the disk blow-up. It doesn't create throughput: 8,862 rows/s at 128K (10M) against 8,909 at 8k (50M), though those are different data sizes, so take that as suggestive only. It just lets the same data fit on the runner. Every ZFS number I took before that fix is untuned.

## What I'd take away

I went in looking for the best filesystem and came out with a different question. Here are the five levers side by side, by how much they moved things:

| Lever | What moved | Size of the effect |
|---|---|---|
| Index type | BRIN vs sequential scan on a 7-day range | ~1,800× |
| Routing to the right shard | Routed vs 32-branch view, cold cache / warm p95 | 39–120× / 5–16× |
| Table layout for writes | 1 table vs 32 tables | within noise (≤13%) |
| Connection count for writes | 16 vs 128–500 connections | throughput −5% to −26%, p95 up to 60× worse |
| Filesystem | ext4 / XFS / btrfs / ZFS | ≤2×, below the round-to-round noise |

(Table layout and connection count are separate rows because the first changed nothing and the second changed a lot.)

What I'd do with this:

1. **Fix the query and index before you touch the filesystem.** Run `EXPLAIN (ANALYZE, BUFFERS)`, check statistics, and pick the index for the query shape. That's where the orders of magnitude are.
2. **Don't split tables on one instance to get write throughput.** It isn't there. There's one fsync lane per instance. Shard when you can route reads, or when you can put shards on separate instances.
3. **Size your pool from the knee, not from your app server count.** For this box that was about 16 connections.
4. **If you're picking a filesystem anyway:** ext4 and XFS are both reasonable defaults, and I wouldn't spend an afternoon on the choice. Use ZFS only if your team already runs it and knows its tuning, and then only with `recordsize=8k`. btrfs showed no stable advantage here.

### The minimum viable production config

So, would that student backend survive real traffic? At this size, yes — with this setup:

| Item | Setting | Why |
|---|---|---|
| Filesystem | ext4, or XFS if the host already uses it | ≤2× apart, and under the round-to-round noise |
| `shared_buffers` | 1 GB | the value I ran; the read bottleneck was CPU, not buffer space |
| Pool | one pool, ~16 connections (4 vCPU × 4) | write p95 3 ms at 16, 82 ms at 128, 189 ms at 500 (ext4); the read knee is also 16 |
| Tables | one `users` table | writes move ≤13% either way; while the data is cached the view measures 0.26 ms p50 / 9.3 ms p95 (ext4, 50M), so splitting buys latency only after the working set outgrows RAM — and it costs cross-shard joins, transactions and global email uniqueness |
| Indexes | btree for equality and prefix, BRIN for time ranges, GiST for kNN, GIN only for substring | up to 1,800× on the right shape; write cost stayed under 4% on ext4 |
| Routing | none yet, and none needed with one table | with one table there is no shard to route. If you split, route when the working set outgrows RAM: cold p50 goes from 69–133 ms to 1.1–1.8 ms |
| After seeding | `ANALYZE` | otherwise the planner treats the table as empty |
| ZFS (only if you must) | `recordsize=8k` | 128K filled the disk in four hours |

That's it. The dials people argue about — filesystem, shard count — moved the least. The ones nobody argues about — an index per query shape, statistics, and a pool sized to the box — moved the most. Nothing here depends on a particular Postgres version, except the source line numbers in Part 2.

**One caveat on ZFS.** It topped the write table (8,862 and 8,915 rows/s at 10M, 8,909 and 8,895 at 50M) and was the most consistent filesystem across runs, so I can't call it slow. What I can say is that I didn't find a tuning that makes it a safe default on this box, and I'd rather list what is still open than declare a verdict:

- **Recordsize.** The default `recordsize=128K` filled the disk after four hours. `8k` fixes that, but I never ran 100M with it, so the write lead is measured at 10M and 50M only.
- **Index builds.** 1.7–3.5× ext4 at 128K, still 1.3–3.5× at 8k. I didn't test whether another recordsize or `logbias` closes that.
- **Cold reads.** `drop_caches` doesn't clear ARC, so the same query measured 69 ms and 743 ms. I haven't found a way to force a true cold read on ZFS in this harness.
- **ARC on 8 GB.** I never measured how much RAM ARC takes from `shared_buffers` and the workload, so the memory trade-off is unknown.
- **Warm p95.** 30.9 ms against 9.3 ms on ext4, and I don't know whether that is ARC eviction or something tunable.

So the conclusion is "I couldn't get ZFS into a state I'd hand to a student project", not "ZFS is bad". If your team already runs it and has tuned it, fine; for a first production box, ext4 or XFS lands in the same write range with fewer unknowns.

### When to shard, and when not to

This post measures table sharding **inside one instance** (M2). Sharding **across instances** is a different thing, and I didn't test it. That distinction decides most of the advice:

- **Inside one instance**, splitting tables buys nothing for writes: same WAL, same commit flush, one `WALWriteLock` per instance. ±13%, both directions.
- **Across instances**, each one gets its own fsync lane, so write capacity does scale — at the cost of cross-shard joins, transactions, global uniqueness and rebalancing. Out of scope here.

So the rule is not "never shard". It is this:

| Situation | Shard? |
|---|---|
| You want more write throughput | Not by splitting tables in one instance — add an instance |
| The working set still fits in RAM and p95 is fine | No: the view costs under 0.1 ms at p50 |
| Data outgrew RAM (50M+ on this box) and cold p50 matters | Yes — split, then route by the key you query |
| One table is too big to build indexes, vacuum, back up or `ALTER` in a reasonable window | Yes, hash or time-based — an operational reason, not latency |
| Queries carry no shard key (global search, cross-shard joins) | No, or accept the view — only while the data stays cached |
| "One pool per tenant/schema" (M1) | No: 5–26% less throughput, p95 up to 60× worse |

Routing has a precondition: **the application must know the shard before it sends the query.** `by_id` satisfies that by construction. A login by email does not, so you need the lookup table or a shard key that matches the query. The routed number in this post is only reachable when that precondition holds.

Order of work, from biggest effect to smallest:

1. Fix the query and the index (up to 1,800×).
2. Size the pool to the knee (~16 on 4 vCPU).
3. Add RAM while that still works; a read replica is the usual next step, though this post doesn't measure one.
4. Shard, with routing, once one table is an operational problem *and* you need the tail latency.

A quick map from symptom to first thing to check:

| Symptom | First thing to check |
|---|---|
| Point lookups slow as data grows | Is the query routed, or scanning a `UNION` view? Run `EXPLAIN (ANALYZE, BUFFERS)`. |
| Writes slow under load, spiky latency | Connection count against the fsync lane. Look at p95, not the average. |
| One dashboard query slow | Index type against query shape (range → BRIN, kNN → GiST, equality → hash). |
| Disk growing faster than rows | Index footprint: 56 KB vs 511 MB for the same table. |
| ZFS fills the disk after a few hours | Is `recordsize` still 128K? |

### Two other measurement traps

Two more cost me time, and both are easy to repeat:

- **A `citext` cast that never matched the index.** `citext → text` is an implicit cast, but `text → citext` is only an assignment cast, so `email = <text expression>` compares as text and misses the citext index. One `::citext` took a query from 4.6 s to 0.2 ms.
- **A scan benchmark that scanned nothing.** `now() - interval '1 day'` matched 13k of 10M rows, and `count(*)` was answered by an index-only scan. The fix (365 days, `sum(user_id)`) produced a real scan, and an inconclusive result on parallel workers, which I reported as inconclusive.

## Limitations

- **ext4 ran natively** while the others ran on a loop device. It has no cold reads, no `pg_test_fsync` and no `pg_stat_io` rows, and it can't be compared directly with the other three.
- **Noise.** 4 vCPU, 8 GB, one container per run, a fresh runner each time. Absolute throughput moves ±40%, and the scale sweep moved up to 1.95× between rounds, so no filesystem ranking can be derived from this data.
- **Scale.** 10M rows for the index lab, 50M and 100M for the layout sweep. A 200M run was dropped because it needed about 80 GB, more than the runner disk. ZFS was measured at 50M only, with `recordsize=8k`.
- **Coverage.** Indexes and layouts were never crossed (see the table of what was measured), and the read connection sweep ran on ext4 only.
- **One instance only.** Everything here runs on a single 4 vCPU instance. Sharding across instances wasn't measured, so the write conclusions do not extend to it: a second instance brings a second fsync lane.
- **f2fs is unmeasured.** The harness can format it, but this study covered only ext4, XFS, btrfs and ZFS.
- **Inserts only.** All write numbers are small-transaction inserts with `synchronous_commit` on. Updates, bulk inserts and long-running vacuum behaviour aren't covered.
- **This post does not benchmark the metrics workload.** The jsonb/norm layouts and `temp_sort` belong to the previous metrics benchmark, which used 1,600 series and 288,000 samples per backend — same harness, different workload. Mixing the two datasets would produce a table that cannot be compared.
- **Btree height** (3 levels at 10M, 4 at 100M) is computed from page fan-out, not measured with `pageinspect`.
- **The prefix and substring shapes carry `LIMIT 100`** on an id-ordered table, which lets a sequential scan exit early; see the caveats in Part 1.
- **The parallel-worker experiment** is inconclusive at this size and CPU count.

The harness, scenarios and raw artifacts are in `benchctl/` at [github.com/hungpham10/my-benchmarks](https://github.com/hungpham10/my-benchmarks). The consolidated numbers are in `benchctl/reports/2026-10-21-pg-index-shard-read.md` and `benchctl/reports/2026-10-21-pg-user-model-fs.md`.

Earlier I used the same harness to test [PostgreSQL as a metrics store](/blog/prometheus-vs-postgres-metrics); that workload is different, and this post doesn't re-measure it. Bản tiếng Việt của bài này ở [bản dịch tiếng Việt](/blog/postgres-sharding-filesystem-vi).
