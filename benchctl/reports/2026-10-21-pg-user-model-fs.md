# User-schema sharding models × filesystems — first full matrix

2026-10-21 · runs 39543885473 (mode/index) · 39543892826 (ext4) ·
39543901032 (xfs) · 39543908599 (btrfs) · 39543916702 (zfs) ·
branch `feat/fs-matrix`, driver code `f1cc829`.

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

| Mode        | seed rows/s | bytes/row | read: by_id p50/p95 | login p50/p95 |
|-------------|-------------|-----------|---------------------|---------------|
| none        | 56–63k      | 403–405   | 0.5–0.6s / 0.66s    | 1.05–1.2s / 1.5s |
| jsonb       | 36.5k (−38%)| 1292 (+3.2×) | 10.4s / 15s      | 6.8s / 15s    |
| gin(path_ops)| 34.9k (−44%)| **1348** (+3.3×) | 1.4s / 7.4s  | 3.1s / 12s    |

- Layout costs are tiny (E ≈ +3% rows, M1/M2 ≈ ±0–7% vs single table)
  for the seed — the layout question is a WRITE-path question, only
  the k6 phase distinguishes them.
- jsonb index (GIN) costs −44% write throughput AND +3.3× footprint.
  Its containment read still costs seconds (view-wide scan).

## Findings (answerable, not speculative)

1. **zfs fastest everywhere** — against the raw fsync expectation.
   The measured ZFS properties are the stock ones: **recordsize 128K,
   compression off** (`fs-zfs.json`; the harness sets no recordsize).
   An earlier draft of this report claimed recordsize=8k — that was
   wrong. Whatever the cause (ARC absorbing metadata, copy-on-write
   batching), the ranking holds for the seed AND every model at 10M.
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
5. **FS ranking is MODEL-INDEPENDENT** (so far, at mode=none): zfs >
   ext4 > xfs ≈ btrfs across every leg — no leg flips the ordering,
   the fsync-interaction hypothesis did NOT hold for the small-commit
   workload; fsync overhead is averaged out by group commit.

## Harness caveats (recorded honestly)

- Read shapes go through the `users_all` UNION view (framework's
  parity mechanism) — point lookups cost a full scan per shard; a real
  app would hit the routed table directly. The absolute read numbers
  above OVERESTIMATE the app-side read cost; relative M0 vs M1/M2
  ordering still meaningful via the UNION width.
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
