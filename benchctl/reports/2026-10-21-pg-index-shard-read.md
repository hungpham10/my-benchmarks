# Index types, sharding and read parallelism — measured

2026-10-21 · branch `feat/fs-matrix` · driver `8edde46`

Three questions, one harness:

1. **Does sharding help indexed reads?** — routed point reads vs the
   `users_all` UNION view, at 10M / 50M / 100M rows.
2. **What does each index type cost to build, keep and read?** — eight
   `IX` legs on one 10M-row table (`none`, `btree_name`, `brin_time`,
   `hash_rating`, `gist_point`, `spgist_point`, `spgist_name`,
   `gin_trgm`), four access methods plus a baseline.
3. **Does adding connections help reads the way it hurts writes?** —
   closed-loop point reads at 1/4/16/64/256 connections, plus a
   `parallel_workers` 0-vs-4 comparison on a heap scan.

Every leg seeds server-side and then runs a 90s closed loop (60s for
the read sweep). Only within-run ratios are trustworthy; the runner
moves ±40% between runs, and two runs of the same leg are noted where
they disagree.

---

## 1. Write path at scale (50M / 100M)

`layoutA` = 1 table, `layoutB` = 32 tables `users_XX`, both 16
connections. Throughput in iters/s, p95 in ms. Each cell is one 90s
leg.

| FS | 50M A | 50M B (32 tables) | 100M A | 100M B | p95 |
|---|---|---|---|---|---|
| ext4 | 7 168 | 6 659 | 6 854 | 6 612 | 3–4 |
| xfs | 4 632 | 4 519 | 4 523 | 4 568 | 6 |
| btrfs | 4 448 | 4 328 | 4 978 | 5 106 | 5–6 |
| **zfs (tuned)** | **8 909** | **8 895** | not measured | not measured | 3 |

- **Sharding does not move the write needle.** 1 table vs 32 tables is
  flat at every size and on every finished FS: the WAL fsync lane
  (`XLogFlush`, one `WALWriteLock` per instance) is not partitioned by
  table names.
- **zfs is the fastest writer in this matrix.** +24% over ext4 at 50M,
  +44% at 10M — but those are single runs on a runner with ±40%
  between-run noise, so treat "zfs ≥ ext4 ≥ xfs ≈ btrfs" as the
  ordering and the exact gap as provisional. The 50M leg needed
  `recordsize=8k` to fit (§7.5); that change did not create the lead
  (zfs was already fastest at 10M on 128K), it only stopped the disk
  from filling.
- Disk cost per row falls with size: 242 B/row at 50M, 221 B/row at
  100M (same 4 indexes), i.e. the fixed per-relation overhead amortises.
  With `recordsize=8k`, zfs lands on the same 241.5/242.3 B/row as the
  other three.
- The 10M matrix (`2026-10-21-pg-user-model-fs.md`) still holds:
  M0/M2 at 16 connections beat M1 pool-per-schema at 128/500
  connections by 13–26% throughput with 20–60× worse p95. More
  connections, same fsync lane: a longer queue, not more capacity.

## 2. Sharded reads: routed vs view — corrected

**The four-order-of-magnitude gap reported in the first version of this
report was an artifact, not a sharding cost.** Those numbers were taken
while the read shapes still put `random()` in the `WHERE` clause (§7.3):
a volatile qual is not a pseudo-constant, so the planner cannot use an
index and every "point lookup" through the view degenerated into 32
sequential scans with `LIMIT 1`. `ANALYZE` was missing too (§7.1), but
the volatile qual alone is enough to force the scan. The 50M zfs leg —
the first scale leg to run with both fixes — tells a different story
(p50 ms, same 16 connections):

| Read, 50M rows, zfs tuned | view (`users_all`) | routed to one shard |
|---|---|---|
| login, warm | 0.09 (A) / 0.11 (B) | 0.07 |
| by_id, warm | 0.12 (A) / 0.10 (B) | 0.07 |
| login, cold | 26.1 (A) / 69.0 (B) | 1.77 |
| by_id, cold | 3.3 (A) / 3.7 (B) | 1.21 |

- **Warm, routing buys nothing measurable**: 0.07 ms routed vs
  0.09–0.11 ms through the 32-branch view. The Append stops at the
  first branch that matches, and with statistics the planner gives each
  branch an index scan. Cost is one extra plan node, not 32 scans.
- **Cold, routing buys 15–40×** (1.2–1.8 ms vs 26–69 ms): the view
  touches 32 index roots instead of one, and those misses hit the
  device. Still two orders, not four.
- The old seconds-scale numbers remain useful for one thing only: they
  are what a **non-indexable** read shape (volatile `random()`) does to
  a 32-branch view. That is a real failure mode — and it is what the
  first read sweep measured — but it is fixed by materialising the
  literal client-side, not by sharding. The old `routed_*` shapes never
  had the problem: they used a literal id, so they compared an index
  lookup against 32 seq scans, not routed against view.
- The earlier ext4/xfs/btrfs scale legs ran before both fixes, so their
  view/routed read numbers are **not comparable** to the table above.
  Their write numbers are unaffected. A rerun of those three legs with
  the fixed init is required before quoting a cross-FS read comparison.

## 3. Index lab — footprint, build, read, write tax

10M-row `docs` table, heap 1.38 GiB (148 B/row). Each leg builds ONE
extra index after the seed, then pays its per-statement maintenance
during the 90s insert loop. `ANALYZE docs` runs before any read (see
§7 — without it every read is a seq scan).

### 3.1 Footprint and build

`index_bytes` is identical on all four filesystems (data, not layout);
build seconds differ per FS. **The zfs column is 128K recordsize
(untuned, §7.5)** — read it as "zfs out of the box", not as zfs.

| IX | index size | build ext4 | build xfs | build btrfs | build zfs |
|---|---|---|---|---|---|
| brin_time | **56 KB** | 5.6 s | 5.5 s | 5.6 s | 16.7 s |
| gin_trgm | 178.6 MB | 15.6 s | 15.8 s | 17.5 s | 33.2 s |
| btree_name | 300.8 MB | 24.6 s | 25.8 s | 25.7 s | 42.2 s |
| hash_rating | 306.6 MB | 14.9 s | 15.2 s | 17.3 s | 29.9 s |
| spgist_name | 337.5 MB | 19.8 s | 19.9 s | 21.6 s | 26.7 s |
| spgist_point | 429.3 MB | 43.7 s | 44.9 s | 45.6 s | 46.3 s |
| gist_point | 510.8 MB | 10.2 s | 10.5 s | 12.1 s | 35.9 s |

- BRIN is 5 400× smaller than a GiST index on the same column and
  builds 8–9× faster. That is the whole BRIN trade: block-range
  summaries instead of per-row entries.
- Build time is an FS question too: untuned zfs pays 1.3–3.4× the ext4
  build time for the same bytes (metadata-heavy index build plus the
  16× record amplification of §7.5), except for SP-GiST point, where
  the sort dominates and the FS barely matters. With `recordsize=8k`
  that column would look different; it has not been re-measured.

### 3.2 Read shapes — each AM wins only its own case

p50 / p95 in ms, xfs (other FS within ±40%; untuned zfs consistently
~10–15% lower, btrfs ~10% higher).

| Shape | Winner | p50 / p95 | Nearest alternative | Baseline (`none`) |
|---|---|---|---|---|
| `range_time` (7-day range) | brin_time | **1.7 / 4.7** | 462 / 511 (hash) | 3 086 / 3 130 |
| `eq_rating` (~100 rows) | hash_rating | **0.23 / 1.0** | 484 / 515 (GIN) | 472 / 519 |
| `knn_point` (ORDER BY `<->`) | gist_point | **0.41 / 1.24** | 3.5 / 5.4 (SP-GiST) | 1 117 / 1 172 |
| `prefix_name` (`LIKE 'User 1234%'`) | spgist_name | **0.22 / 0.83** | 0.28 / 663 (btree!) | 0.30 / 638 |
| `trgm_like` (`LIKE '%12345%'`) | gin_trgm (stable) | 4.2 / 4.7 | 0.22 / 96 (seq) | 0.24 / 96 |

Two of these need a caveat rather than a headline:

- **`prefix_name` and the btree.** `btree_name` is `CREATE INDEX ON
  docs (name)`. Under a non-C collation a plain btree cannot serve a
  prefix `LIKE`, so the btree leg fells back to a scan at p95 (663 ms)
  while SP-GiST stays sub-millisecond. A `text_pattern_ops` index
  would fix it — that is a definition detail, not an access-method
  verdict.
- **`trgm_like` does not prove GIN wins.** The query has `LIMIT 100`
  and the table is ordered by `id`, while `'%12345%'` first matches
  around id 12345 — so the no-index scan exits after ~13k rows
  (0.2 ms p50, 96 ms p95). GIN is the only leg with a stable ~4 ms,
  which is a consistency win, not a speed win. This shape needs
  `LIMIT 1000` or a pattern that matches late rows before it can
  support a claim.
- `knn_point` is the cleanest result: GiST is 2 700× faster than the
  sort-based baseline, and 8× faster than SP-GiST's kNN.

### 3.3 Read path by filesystem: warm and cold

Warm p50 / p95 (ms) for each shape's winning leg. The zfs column is
untuned 128K recordsize (§7.5):

| Shape (leg) | ext4 | xfs | btrfs | zfs |
|---|---|---|---|---|
| range_time (brin) | 1.71 / 6.55 | 1.70 / 4.66 | 2.50 / 4.51 | **1.36 / 5.60** |
| eq_rating (hash) | 0.25 / 1.00 | 0.23 / 1.01 | 0.24 / 1.00 | **0.16 / 1.45** |
| knn_point (gist) | 0.42 / 1.30 | 0.41 / 1.24 | 0.59 / 1.23 | **0.31 / 1.78** |
| prefix_name (spgist) | 0.25 / 0.77 | 0.22 / 0.83 | 0.22 / 0.72 | **0.14 / 1.02** |

Cold reads (container restarted before the read, so the page cache is
gone; xfs/btrfs/zfs only):

| Shape | xfs | btrfs | zfs |
|---|---|---|---|
| BRIN range (index + one week of heap) | 73.7 ms | 99.3 ms | **71.4 ms** |
| seq scan, 7-day range (no index) | **3 103 ms** | 3 164 ms | 4 062 ms |
| kNN with GiST | 1 315 ms | 1 337 ms | **1 007 ms** |

So the zfs read story is two-sided and does not match the write story:

- **Warm, it is the fastest on every shape** (10–40% below xfs/ext4 p50),
  but its p95 is *worse* on three of four shapes: ARC gives a lower
  median and a fatter tail.
- **Cold, it is on par with xfs for indexed reads** (71 ms vs 74 ms on
  the BRIN range) and *slower* for a full sequential scan (4 062 ms vs
  3 103 ms). At the measured 128K recordsize (§7.5) a 1.4 GB heap scan
  reads in larger units and pays more per page; at 8k this may change,
  which is one more reason to rerun the zfs index lab.
- Absolute differences here are milliseconds; the warm view-vs-routed
  difference in §2 is tenths of a millisecond. Do not pick a filesystem
  for indexed point reads.

### 3.4 Write tax per index

Insert throughput of the 90s loop, relative to the `none` leg of the
same FS (so FS speed cancels out).

| IX | ext4 | xfs | btrfs | zfs |
|---|---|---|---|---|
| brin_time | −0.6% | −1.1% | +2.1% | −4.4% |
| btree_name | −1.3% | −3.4% | +1.5% | +6.1% |
| gin_trgm | −1.5% | −4.7% | −0.5% | −9.1% |
| hash_rating | −0.8% | −3.7% | +0.5% | +4.1% |
| spgist_name | −1.6% | −4.5% | −0.7% | −8.2% |
| gist_point | −3.5% | −6.2% | −2.9% | −13.3% |
| spgist_point | −3.8% | −11.2% | −0.7% | −20.1% |

- On ext4/btrfs even the heaviest AM costs ≤4%; on xfs 11%; on zfs 20%.
  The index is not the bottleneck — the commit path is — and the FS
  determines how much of the index maintenance lands in the measured
  window.
- zfs's per-leg numbers here are noisy in absolute terms (this run's
  `none` leg was 4 195 it/s against 8 862 in the 10M matrix); the
  within-run ratio is the only number used.
- v1 of this lab (before `ANALYZE`) reported "GIN −24%". That was the
  planner scanning a stats-less table; v2 with stats is the number
  above.

## 4. Read parallelism — where the knee is

Closed loop, `user_login` (unique e-mail index, literal per iteration),
10M rows, ext4, 60s per leg, VUs = connections.

| connections | throughput | p50 | p95 |
|---|---|---|---|
| 1 | 5 165 it/s | 0 ms | 1 ms |
| 4 | 9 304 it/s | 0 ms | 1 ms |
| **16** | **10 060 it/s** | 1 ms | **3 ms** |
| 64 | 9 484 it/s | 4 ms | 35 ms |
| 256 | 7 439 it/s | 28 ms | 78 ms |

- Point reads **do** scale with connections, unlike writes: 2× from 1
  to 16 connections, with p95 still at 3 ms. There is no fsync lane to
  queue on, so the ceiling is CPU, not a lock.
- The knee is at **16 connections = 4 vCPU × 4**, matching the
  `cores × 2–4` rule of thumb. Past it throughput *falls* 26% and p95
  grows 26× — the same shape as the write side, just at a higher
  connection count and with much smaller absolute latency.
- qbench point reads are flat across all five legs (0.17–0.24 ms), so
  the curve is a scheduling effect, not a plan change.

## 5. Parallel workers on a scan — inconclusive

`sum(user_id)` over a 365-day range (~5M of 10M rows, forces heap
access), ext4:

| leg | k6 p50 | qbench p50 |
|---|---|---|
| workers 0, 1 conn | 509 ms | 948 ms |
| workers 4, 1 conn | 703 ms | 750 ms |
| workers 4, 16 conns | 14 383 ms | 747 ms |

The two measurements disagree in sign, and each leg re-seeds its own
10M rows, so the between-leg variance is larger than the effect. Two
earlier attempts measured the wrong thing entirely (`now() - 1 day`
≈ 13k rows; then `count(*)`, which is served by an index-only scan on
`created_at` and barely parallelises).

**Conclusion: at 4 vCPU and this data size, no reliable worker gain was
measured.** Do not claim one. Testing it properly needs several
repetitions per leg on the same seed, and a scan big enough that
parallel setup cost is amortised.

## 6. Findings

1. **Routing a shard saves cold cache misses, not query plans.** Warm,
   a routed point read is 0.07 ms against 0.09–0.11 ms through the
   32-branch view — no measurable difference. Cold, routing is worth
   15–40× (1.2–1.8 ms vs 26–69 ms) because the view touches 32 index
   roots. The four-order gap in the first version of this report was a
   read-shape bug (§7.2, §7.3), not a property of sharding. Index
   height changes by one level between 10M and 100M (computed from the
   btree fan-out, not measured with `pageinspect`).
2. **Sharding does not buy writes.** 1 table vs 32 tables is flat at
   50M and 100M. The commit path is per instance, not per relation.
3. **zfs, configured for Postgres, is the fastest writer here.** 8 909
   (A) / 8 895 (B) it/s at 50M against ext4 7 168/6 659, xfs
   4 632/4 519 and btrfs 4 448/4 328 at the same 16 connections, with
   the same 241.5 B/row. Those are single runs with ±40% between-run
   noise, so the gap is provisional; but the earlier "zfs writes
   slowly" reading came from a 128K recordsize (§7.5) and must not be
   reused.
4. **The per-index write bill is FS-dependent**: ≤4% on ext4/btrfs,
   11% on xfs, 20% on zfs for the worst AM (SP-GiST point). BRIN is
   free within noise and 5 400× smaller.
5. **Each access method wins exactly its own shape** — BRIN range,
   hash equality, GiST kNN, SP-GiST prefix. Picking "an index" without
   the query shape is how a 1.7 ms range becomes a 3 s scan.
6. **Read parallelism has a knee at ~4× cores** (16 connections on
   4 vCPU): 2× throughput up to it, −26% and 26× p95 past it. Write
   parallelism turns over earlier and more sharply because of the
   single WAL fsync lane.
7. **Btree is still the default** — it serves equality and range well,
   and the one place it loses here (`LIKE 'prefix%'`) is a missing
   `text_pattern_ops`, not a limit of the AM.

## 7. What was measured wrong (and fixed mid-flight)

Recorded because each of these produced a plausible-looking, wrong
number:

1. **No `ANALYZE` after the userdb seed.** A read-only leg never
   triggers autoanalyze (write legs did, by accident, after ~50
   inserts), so the planner believed the table was empty and chose seq
   scans. Fixed with an explicit `ANALYZE` per seeded table, reported
   as `analyze_seconds`.
2. **`random()` in the WHERE clause.** `is_pseudo_constant_clause`
   (`clauses.c:2113`) rejects volatile expressions, so
   `email = 'user' || random() || …` can never use an index. Every
   "point lookup" was a seq scan with `LIMIT 1`: 5.97 s per login, 12
   iterations in 60 s. Fixed by substituting the id client-side in
   `step.js` and keeping the SQL a literal.
3. **`citext` cast direction.** `citext → text` is IMPLICIT
   (`contrib/citext/citext--1.4.sql:83`) while `text → citext` is only
   ASSIGNMENT, so an uncast text expression compares as `text = text`
   and misses the citext index. Fixed with `::citext`.
4. **A scan shape that measured nothing.** `now() - 1 day` is ~13k of
   10M rows; `count(*)` is answered by an index-only scan. Fixed by
   widening to 365 days and projecting `sum(user_id)`.
5. **zfs dataset at the stock 128K recordsize.** Postgres writes 8 KB
   pages; a 128K record turns every dirty page into a read-modify-write
   of the whole record — up to 16× write amplification once the working
   set stops fitting in ARC. Consequence: the 50M/100M zfs attempt
   (run 298) died after 3h58m with the runner unable to write
   `Worker_*.log` ("No space left on device"). Fixed in `fssetup.py`
   with `recordsize=8k`; the 50M zfs leg (run 306) is the first zfs
   data measured with it. **Every zfs number before run 306 is
   untuned-128K and is labelled as such**, including the 10M index-lab
   build times and the warm/cold read tables. Note that the tuning did
   not create zfs's write lead: zfs was already fastest at 10M on 128K
   (8 862 it/s), where the working set fits in ARC. 8k is what makes
   the 50M data fit on the runner at all.

## 8. Caveats

- 10M rows for the index lab; scale legs at 50M/100M. The 200M point
  was dropped: ~80 GB exceeds the runner disk.
- zfs at 50M only, and tuned (`recordsize=8k`). 100M zfs is unmeasured:
  the 50M+100M attempt hit the disk ceiling before the recordsize fix.
- The 50M zfs leg is the only scale leg whose reads ran with fixed
  shapes; ext4/xfs/btrfs 50M reads predate both fixes and need a rerun
  before any cross-FS read claim at scale.
- 4 vCPU, 8 GB, one container per leg, WAL on the FS under test.
  Absolute throughput moves ±40% between runs; ratios within a leg are
  the evidence.
- `pg_test_fsync` refuses O_DIRECT on zfs, so the raw fsync column is
  missing there by design; fio + `pg_stat_io` cover the gap.
- `prefix_name`/`trgm_like` carry a `LIMIT 100` and an id-ordered
  table, which lets a scan exit early. Read §3.2 before quoting them.
- Parallel-workers result is inconclusive (§5).

## 9. Runs and artifacts

| What | Scenario | Run |
|---|---|---|
| idxlab 10M, ext4 | `pg-idxlab-ext4` | gh-291 |
| idxlab 10M, xfs | `pg-idxlab-xfs` | gh-292 |
| idxlab 10M, btrfs | `pg-idxlab-btrfs` | gh-293 |
| idxlab 10M, zfs | `pg-idxlab-zfs` | gh-294 |
| scale 50M/100M, ext4 | `pg-shard-scale-ext4` | gh-295 |
| scale 50M/100M, xfs | `pg-shard-scale-xfs` | gh-296 |
| scale 50M/100M, btrfs | `pg-shard-scale-btrfs` | gh-297 |
| scale 50M/100M, zfs | `pg-shard-scale-zfs` | gh-298 (failed: disk, see §7.5) |
| scale 50M tuned (8k), zfs | `pg-shard-scale-zfs-50m` | gh-306 |
| read sweep, ext4 | `pg-readpool-ext4` | gh-301/303 |
| scan + workers, ext4 | `pg-readscan-ext4` | gh-302/304 |

The first attempt at each read scenario is retained in the history as a
record of §7: their numbers must not be cited.
