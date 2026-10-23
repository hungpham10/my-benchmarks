# Benchctl Benchmark Report

## Manifest
- Scenario: pg-config-sweep.yaml
- Run ID: gh-313.1-pg-config-sweep
- Total samples: 3600000
- Interval: 15s
- Window: 45 min

## Disk Footprint
| System | Samples | Bytes | B/sample |
|--------|---------|-------|----------|
| postgres/userdb (arg_wb64MB-conns16-layoutA-modenone-n_users2000000) | 2,000,000 | 1.1 GB | 572.39 |
| postgres/userdb (ckpt30min-conns16-layoutA-modenone-n_users2000000) | 2,000,000 | 1.1 GB | 572.39 |
| postgres/userdb (conns16-layoutA-mix_read_pct50-modenone-n_users2000000) | 2,000,000 | 1.1 GB | 572.39 |
| postgres/userdb (conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000) | 2,000,000 | 1.1 GB | 572.39 |
| postgres/userdb (conns16-layoutA-modenone-n_users2000000-syncoff) | 2,000,000 | 1.1 GB | 572.39 |
| postgres/userdb (conns16-layoutA-modenone-n_users2000000) | 2,000,000 | 1.1 GB | 572.39 |

## k6 Load Test
| System | Iterations | p50 ms | p95 ms | Dropped |
|--------|------------|--------|--------|---------|
| postgres-userdb-arg_wb64MB-conns16-layoutA-modenone-n_users2000000 | 281,014 | 1.7 ms | 5.5 ms | 0 |
| postgres-userdb-ckpt30min-conns16-layoutA-modenone-n_users2000000 | 282,498 | 1.7 ms | 5.5 ms | 0 |
| postgres-userdb-conns16-layoutA-mix_read_pct50-modenone-n_users2000000 | 218,700 | 2.2 ms | 8.4 ms | 0 |
| postgres-userdb-conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000 | 285,956 | 1.7 ms | 5.3 ms | 0 |
| postgres-userdb-conns16-layoutA-modenone-n_users2000000 | 306,493 | 1.6 ms | 5.0 ms | 0 |
| postgres-userdb-conns16-layoutA-modenone-n_users2000000-syncoff | 327,947 | 1.2 ms | 4.4 ms | 0 |

## Resource Usage (during k6 leg)
| System | Leg (s) | CPU core avg | Mem max MB |
|--------|---------|--------------|------------|
| postgres-userdb-arg_wb64MB-conns16-layoutA-modenone-n_users2000000 | 62.0 | None | None |
| postgres-userdb-ckpt30min-conns16-layoutA-modenone-n_users2000000 | 62.0 | None | None |
| postgres-userdb-conns16-layoutA-mix_read_pct50-modenone-n_users2000000 | 62.1 | None | None |
| postgres-userdb-conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000 | 61.9 | None | None |
| postgres-userdb-conns16-layoutA-modenone-n_users2000000 | 62.0 | None | None |
| postgres-userdb-conns16-layoutA-modenone-n_users2000000-syncoff | 62.0 | None | None |

## Query Latency (q1–q5, measured)
| Query | postgres/userdb (arg_wb64MB-conns16-layoutA-modenone-n_users2000000) p50 ms | postgres/userdb (ckpt30min-conns16-layoutA-modenone-n_users2000000) p50 ms | postgres/userdb (conns16-layoutA-mix_read_pct50-modenone-n_users2000000) p50 ms | postgres/userdb (conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000) p50 ms | postgres/userdb (conns16-layoutA-modenone-n_users2000000) p50 ms | postgres/userdb (conns16-layoutA-modenone-n_users2000000-syncoff) p50 ms |
|-------|----|----|----|----|----|----|
| login | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 |
| by_id | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 |
| recent | 104.8 | 102.4 | 101.3 | 103.7 | 101.5 | 100.8 |

| Query | postgres/userdb (arg_wb64MB-conns16-layoutA-modenone-n_users2000000) p95 ms | postgres/userdb (ckpt30min-conns16-layoutA-modenone-n_users2000000) p95 ms | postgres/userdb (conns16-layoutA-mix_read_pct50-modenone-n_users2000000) p95 ms | postgres/userdb (conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000) p95 ms | postgres/userdb (conns16-layoutA-modenone-n_users2000000) p95 ms | postgres/userdb (conns16-layoutA-modenone-n_users2000000-syncoff) p95 ms |
|-------|----|----|----|----|----|----|
| login | 1.8 | 1.7 | 1.7 | 1.9 | 1.6 | 1.6 |
| by_id | 0.6 | 0.6 | 0.5 | 0.6 | 0.5 | 0.5 |
| recent | 120.4 | 118.9 | 118.5 | 123.1 | 117.7 | 137.9 |

| Query | Expected winner | Measured fastest | Notes |
|-------|-----------------|------------------|-------|
| login | — | postgres/userdb (conns16-layoutA-modenone-n_users2000000) | ok |
| by_id | — | postgres/userdb (arg_wb64MB-conns16-layoutA-modenone-n_users2000000) | ok |
| recent | — | postgres/userdb (conns16-layoutA-modenone-n_users2000000-syncoff) | ok |

## Caveats
- postgres/userdb (arg_wb64MB-conns16-layoutA-modenone-n_users2000000): retained 2,000,000 samples.
- postgres/userdb (ckpt30min-conns16-layoutA-modenone-n_users2000000): retained 2,000,000 samples.
- postgres/userdb (conns16-layoutA-mix_read_pct50-modenone-n_users2000000): retained 2,000,000 samples.
- postgres/userdb (conns16-layoutA-modenone-mws8GB-mws_min1GB-n_users2000000): retained 2,000,000 samples.
- postgres/userdb (conns16-layoutA-modenone-n_users2000000-syncoff): retained 2,000,000 samples.
- postgres/userdb (conns16-layoutA-modenone-n_users2000000): retained 2,000,000 samples.
- k6 thresholds: exit 99 means threshold broke (breaking point), not an error.
- Breaking point: not reached (no k6 threshold broke).
- Disk bytes include WAL; Postgres figure is real COPY bytes, not a hardcoded constant.
