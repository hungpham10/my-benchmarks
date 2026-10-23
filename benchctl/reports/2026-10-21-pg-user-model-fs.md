# User-schema sharding models × filesystems — first full matrix

2026-10-21 · runs 39543885473 (mode/index) · 39543892826 (ext4) ·
39543901032 (xfs) · 39543908599 (btrfs) · 39543916702 (zfs) ·
branch `feat/fs-matrix`, driver code `f1cc829`.

> **Revised 2026-10-22.** Two things in the first version did not
> survive re-measurement: (a) the read columns were taken with a
> non-indexable query shape and no `ANALYZE`, so they measured
> sequential scans; (b) the filesystem ranking is not identifiable
> from one run per cell, because the same FS moves by up to 1.95×
> between batches. Both are corrected in place below; the write and
> footprint numbers stand.

## The setup (per leg)

Plain user-management schema: `users(user_id bigint PK, email citext
UNIQUE, name, created_at[, payload jsonb])`, 3 small indexes (+ GIN
jsonb_path_ops on the `gin` mode). **10M rows seeded server-side**
(single backend, `synchronous_commit=off` for the seed — per-commit
fsync is the k6 phase's question). Then the k6 leg:

- **Models matrix** (`pg-shard-models-<fs>.yaml`) runs a **closed loop**
  (`constant-vus`, 90s): VUs push registrations back to back. The VU
  budget IS the app's pool sizing:
  - **M0** baseline — 1 table, 1 pool, 16 conns → 16 VUs
  - **M1** schema-sharding — P schemas × ONE users table; `user_id % P`
    picks the pool; pool == schema. P8 → 128 VUs, P32 → 500 VUs
  - **M2** table-suffix — 32 tables `users_XX`, one pool → 16 VUs
- **Index-modes leg** (`pg-shard-user.yaml`, arrival-rate) separates
  `none / jsonb / gin` on ext4.

Server: postgres:latest, shared_buffers 1GB, max_connections 600,
wal on same FS (single mount, as every leg of the framework).

## Matrix headline — k6 closed-loop INSERT (90s)

throughput (its/s) — latency med/p95 ms in parentheses.

| Model (VUs)          | ext4              | xfs               | btrfs             | zfs               |
|----------------------|-------------------|-------------------|-------------------|-------------------|
| M0 — 1 table (16)    | 6 157 (1 / 3)     | 4 793 (2 / 5)     | 4 484 (3 / 6)     | 8 862 (1 / 3)     |
| M1-P8 — pools (128)  | 5 028 (12 / 82)   | 4 243 (18 / 75)   | 4 239 (19 / 72)   | 7 230 (10 / 60)   |
| M1-P32 — pools (500) | 5 481 (83 / 189)  | 3 975 (103 / 227) | 4 007 (104 / 232) | 6 577 (72 / 160)  |
| M2 — 32 tables (16)  | **6 757** (1 / 4) | 4 571 (2 / 6)     | 4 283 (3 / 6)     | **8 915** (1 / 3) |

## Bulk seed (10M rows, single backend) — init_s / rows/s

| Layout             | ext4         | xfs          | btrfs        | zfs          |
|--------------------|--------------|--------------|--------------|--------------|
| M0/A 1 table       | 147.6 / 67.7k| 196.5 / 50.9k| 198.3 / 50.4k| 132.1 / 75.7k|
| M1-P8 (8 schemas)  | 138.8 / 72.0k| 180.6 / 55.4k| 185.5 / 53.9k| 127.6 / 78.4k|
| M1-P32 (32 schemas)| 151.2 / 66.2k| 195.0 / 51.3k| 204.2 / 49.0k| 143.5 / 69.7k|
| M2/B 32 tables     | 156.3 / 64.0k| 194.1 / 51.5k| 202.6 / 49.3k| 144.2 / 69.3k|

## Index modes (ext4, 10M-row seed) — the real GIN picture

| Mode        | seed rows/s | bytes/row |
|-------------|-------------|-----------|
| none        | 56–63k      | 403–405   |
| jsonb       | 36.5k (−38%)| 1292 (+3.2×) |
| gin(path_ops)| 34.9k (−44%)| **1348** (+3.3×) |

- Layout costs are tiny (E ≈ +3% rows, M1/M2 ≈ ±0–7% vs single table)
  for the seed — the layout question is a WRITE-path question, only
  the k6 phase distinguishes them.
- jsonb index (GIN) costs −44% write throughput AND +3.3× footprint.
- **The read columns that used to be in this table are removed.** They
  were measured before the read-shape fixes (`random()` in `WHERE`,
  no `ANALYZE`; see `2026-10-21-pg-index-shard-read.md` §7), so the
  "seconds per point lookup" was a sequential scan, not a property of
  jsonb/GIN. Corrected reads are in that report; they have not been
  re-measured per index mode.

## Findings (answerable, not speculative)

1. **zfs led this matrix — but that is one run per cell on four
   separate runners, not a ranking.** It led the seed and every model
   at 10M; a later re-run of the same scale scenario on the same FS
   moved by up to 1.95× (btrfs 4 448 → 8 656 it/s), which is wider than
   the gap between filesystems. Treat the *ordering* here as
   descriptive of this batch only. The 10M matrix ran at the stock
   **recordsize 128K, compression off** (`fs-zfs.json`); the harness
   now sets `recordsize=8k` for zfs, which is what let the 50M run fit
   on disk. See `2026-10-21-pg-index-shard-read.md` §1 and §3.5.
2. **M2 (table-suffix) ≥ M0 (single table)** with the SAME 16-conn
   pool budget — 6 757 vs 6 157 on ext4 (+10%), 8 915 vs 8 862 on zfs.
   Page contention across 32 heaps beats one hot heap. Cheap win,
   application-side only.
3. **M1 pool-per-schema LOSES to M0 with 8× the VU budget** — 5 028
   (128 VUs) vs 6 157 (16). More pools = more connections fighting for
   the same WAL fsync lane; p50 latency climbs 1→12ms and p95 3→82ms.
   Schema isolation buys ordering, not throughput.
4. **M1-P32 non-monotonic** — 5 481 > M1-P8's 5 028 on ext4 (and
   latency worse: p50 83ms): more pools spread contention, but 500
   conns overwhelm WAL commit rate; the pool benefit saturates far
   below the naive "more pools = more throughput" curve.
5. **No leg flips the ordering inside this batch**: zfs > ext4 > xfs ≈
   btrfs, at every model and at mode=none. The fsync-interaction
   hypothesis did not hold for the small-commit workload — fsync
   overhead is averaged out by group commit. But the between-batch
   swing (see finding 1) is larger than the between-FS spread, so this
   is "all four are within ~2×", not a ranking you can act on.

## Harness caveats (recorded honestly)

- Read shapes go through the `users_all` UNION view (framework's
  parity mechanism). **Correction:** the view was *not* inherently a
  full scan per shard — that was the volatile-`random()` shape bug.
  With statistics and an indexable predicate, warm p50 through the
  32-branch view is 0.04–0.08 ms from a routed read. See
  `2026-10-21-pg-index-shard-read.md` §2. The read numbers originally
  in this report were measured before that fix and are not usable.
- Arrival-mode runs (pg-shard-user) all capped at ~158 it/s by the
  ramping-arrival-rate executor standing up VUs slowly; that scenario
  is retained for the SEED/index comparison, not the latency axis.
  The closed-loop matrix above is the throughput/latency authority.
- pg_test_fsync on zfs: O_DIRECT refusal (framework avoid): fio
  column + pg_stat_io cover it; fsync-*.json lacks zfs by design.

## Where the numbers came from

- `pg-shard-models-{ext4,xfs,btrfs,zfs}` scenarios (one FS per job,
  4 legs each), driver `d3b8e53..f1cc829`.
- Artifact files: `disk-*.json` (seed), `summary-*.json` (k6),
  `queries-*.json` (qbench), `fs-*.json` (fio), `resource-*.json`.
