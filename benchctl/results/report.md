# Benchctl Benchmark Report

## Manifest
- Scenario: pg-config-sweep.yaml
- Run ID: gh-314.1-pg-config-sweep
- Total samples: 3600000
- Interval: 15s
- Window: 45 min

## Disk Footprint
| System | Samples | Bytes | B/sample |
|--------|---------|-------|----------|
| postgres/userdb (arg_wb64MB-blk4-conns16-layoutA-modenone-n_users2000000) | 2,000,000 | 1.1 GB | 572.39 |
| postgres/userdb (blk1-conns16-layoutA-modenone-n_users2000000) | 2,000,000 | 1.1 GB | 572.39 |
| postgres/userdb (blk2-conns16-layoutA-modenone-n_users2000000-syncoff) | 2,000,000 | 1.1 GB | 572.39 |
| postgres/userdb (blk3-conns16-layoutA-modenone-n_users2000000) | 2,000,000 | 1.1 GB | 572.37 |
| postgres/userdb (blk5-conns16-layoutA-modenone-n_users2000000) | 2,000,000 | 1.1 GB | 572.39 |
| postgres/userdb (blk6-conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000) | 2,000,000 | 1.1 GB | 572.37 |
| postgres/userdb (blk7-conns16-layoutA-modenone-n_users2000000) | 2,000,000 | 1.1 GB | 572.37 |
| postgres/userdb (blk8-ckpt30min-conns16-layoutA-modenone-n_users2000000) | 2,000,000 | 1.1 GB | 572.39 |
| postgres/userdb (blk9-conns16-layoutA-mix_read_pct50-modenone-n_users2000000) | 2,000,000 | 1.1 GB | 572.37 |

## k6 Load Test
| System | Iterations | p50 ms | p95 ms | Dropped |
|--------|------------|--------|--------|---------|
| postgres-userdb-arg_wb64MB-blk4-conns16-layoutA-modenone-n_users2000000 | 588,808 | 0.9 ms | 2.4 ms | 0 |
| postgres-userdb-blk1-conns16-layoutA-modenone-n_users2000000 | 597,492 | 0.9 ms | 2.4 ms | 0 |
| postgres-userdb-blk2-conns16-layoutA-modenone-n_users2000000-syncoff | 702,346 | 0.6 ms | 2.1 ms | 0 |
| postgres-userdb-blk3-conns16-layoutA-modenone-n_users2000000 | 601,563 | 0.9 ms | 2.3 ms | 0 |
| postgres-userdb-blk5-conns16-layoutA-modenone-n_users2000000 | 582,340 | 0.9 ms | 2.4 ms | 0 |
| postgres-userdb-blk6-conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000 | 577,129 | 0.9 ms | 2.4 ms | 0 |
| postgres-userdb-blk7-conns16-layoutA-modenone-n_users2000000 | 591,089 | 0.9 ms | 2.4 ms | 0 |
| postgres-userdb-blk8-ckpt30min-conns16-layoutA-modenone-n_users2000000 | 593,463 | 0.9 ms | 2.4 ms | 0 |
| postgres-userdb-blk9-conns16-layoutA-mix_read_pct50-modenone-n_users2000000 | 443,585 | 1.1 ms | 3.2 ms | 0 |

## Resource Usage (during k6 leg)
| System | Leg (s) | CPU core avg | Mem max MB |
|--------|---------|--------------|------------|
| postgres-userdb-arg_wb64MB-blk4-conns16-layoutA-modenone-n_users2000000 | 64.4 | None | None |
| postgres-userdb-blk1-conns16-layoutA-modenone-n_users2000000 | 64.3 | None | None |
| postgres-userdb-blk2-conns16-layoutA-modenone-n_users2000000-syncoff | 63.7 | None | None |
| postgres-userdb-blk3-conns16-layoutA-modenone-n_users2000000 | 64.2 | None | None |
| postgres-userdb-blk5-conns16-layoutA-modenone-n_users2000000 | 64.3 | None | None |
| postgres-userdb-blk6-conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000 | 63.5 | None | None |
| postgres-userdb-blk7-conns16-layoutA-modenone-n_users2000000 | 64.4 | None | None |
| postgres-userdb-blk8-ckpt30min-conns16-layoutA-modenone-n_users2000000 | 64.4 | None | None |
| postgres-userdb-blk9-conns16-layoutA-mix_read_pct50-modenone-n_users2000000 | 64.2 | None | None |

## Query Latency (q1–q5, measured)
| Query | postgres/userdb (arg_wb64MB-blk4-conns16-layoutA-modenone-n_users2000000) p50 ms | postgres/userdb (blk1-conns16-layoutA-modenone-n_users2000000) p50 ms | postgres/userdb (blk2-conns16-layoutA-modenone-n_users2000000-syncoff) p50 ms | postgres/userdb (blk3-conns16-layoutA-modenone-n_users2000000) p50 ms | postgres/userdb (blk5-conns16-layoutA-modenone-n_users2000000) p50 ms | postgres/userdb (blk6-conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000) p50 ms | postgres/userdb (blk7-conns16-layoutA-modenone-n_users2000000) p50 ms | postgres/userdb (blk8-ckpt30min-conns16-layoutA-modenone-n_users2000000) p50 ms | postgres/userdb (blk9-conns16-layoutA-mix_read_pct50-modenone-n_users2000000) p50 ms |
|-------|----|----|----|----|----|----|----|----|----|
| login | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 |
| by_id | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 |
| recent | 65.7 | 69.0 | 63.1 | 63.3 | 68.4 | 67.8 | 74.5 | 68.7 | 68.8 |

| Query | postgres/userdb (arg_wb64MB-blk4-conns16-layoutA-modenone-n_users2000000) p95 ms | postgres/userdb (blk1-conns16-layoutA-modenone-n_users2000000) p95 ms | postgres/userdb (blk2-conns16-layoutA-modenone-n_users2000000-syncoff) p95 ms | postgres/userdb (blk3-conns16-layoutA-modenone-n_users2000000) p95 ms | postgres/userdb (blk5-conns16-layoutA-modenone-n_users2000000) p95 ms | postgres/userdb (blk6-conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000) p95 ms | postgres/userdb (blk7-conns16-layoutA-modenone-n_users2000000) p95 ms | postgres/userdb (blk8-ckpt30min-conns16-layoutA-modenone-n_users2000000) p95 ms | postgres/userdb (blk9-conns16-layoutA-mix_read_pct50-modenone-n_users2000000) p95 ms |
|-------|----|----|----|----|----|----|----|----|----|
| login | 1.4 | 1.3 | 1.3 | 1.2 | 1.5 | 1.3 | 1.4 | 1.4 | 1.2 |
| by_id | 0.4 | 0.4 | 0.4 | 0.3 | 0.4 | 0.4 | 0.5 | 0.3 | 0.3 |
| recent | 80.4 | 79.0 | 79.2 | 78.8 | 80.3 | 79.6 | 90.6 | 81.4 | 80.9 |

| Query | Expected winner | Measured fastest | Notes |
|-------|-----------------|------------------|-------|
| login | — | postgres/userdb (arg_wb64MB-blk4-conns16-layoutA-modenone-n_users2000000) | ok |
| by_id | — | postgres/userdb (arg_wb64MB-blk4-conns16-layoutA-modenone-n_users2000000) | ok |
| recent | — | postgres/userdb (blk2-conns16-layoutA-modenone-n_users2000000-syncoff) | ok |

## Caveats
- postgres/userdb (arg_wb64MB-blk4-conns16-layoutA-modenone-n_users2000000): retained 2,000,000 samples.
- postgres/userdb (blk1-conns16-layoutA-modenone-n_users2000000): retained 2,000,000 samples.
- postgres/userdb (blk2-conns16-layoutA-modenone-n_users2000000-syncoff): retained 2,000,000 samples.
- postgres/userdb (blk3-conns16-layoutA-modenone-n_users2000000): retained 2,000,000 samples.
- postgres/userdb (blk5-conns16-layoutA-modenone-n_users2000000): retained 2,000,000 samples.
- postgres/userdb (blk6-conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000): retained 2,000,000 samples.
- postgres/userdb (blk7-conns16-layoutA-modenone-n_users2000000): retained 2,000,000 samples.
- postgres/userdb (blk8-ckpt30min-conns16-layoutA-modenone-n_users2000000): retained 2,000,000 samples.
- postgres/userdb (blk9-conns16-layoutA-mix_read_pct50-modenone-n_users2000000): retained 2,000,000 samples.
- k6 thresholds: exit 99 means threshold broke (breaking point), not an error.
- Breaking point: not reached (no k6 threshold broke).
- Disk bytes include WAL; Postgres figure is real COPY bytes, not a hardcoded constant.
