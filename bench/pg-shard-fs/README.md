# pg-shard-fs-bench

Benchmark cho plan `~/.opencode/plan/pg-mod-shard-fs-benchmark.md`.

So sánh modulo-suffix sharding (`user_id % N` → `events_XX`) vs schema sharding
vs declarative hash partition, trên nhiều FS (ext4 / xfs / btrfs / zfs loopback
trên GitHub runner), với các biến thể index (btree composite / đơn, GIN
jsonb_ops vs jsonb_path_ops, generated-column btree, BRIN).

## Layouts

| ID | Mô tả |
|----|-------|
| A | 1 bảng `events` (baseline) |
| B | N bảng `events_%02d` (chính) |
| C | 8 schemas × 4 bảng (namespace-only control) |
| D | B + `payload jsonb` |
| E | `PARTITION BY HASH (user_id)` N partitions |
| F | B + BRIN `(created_at)` |

## Index variants

| ID | Cấu hình |
|----|----------|
| I1 | btree `(user_id, created_at DESC)` |
| I2 | btree `(user_id)` |
| I3 | D + GIN `jsonb_ops` `fastupdate=on` |
| I4 | D + GIN `jsonb_path_ops` `fastupdate=off` |
| I5 | D + generated `kind` + btree `(user_id, kind)` |

## Chạy local (spike trước khi chạy GH)

```bash
# 1. setup PG 18 (đã cầm pin version trong setup-pg.sh)
bash setup-pg.sh /tmp/pgdata /tmp/pgwal 5433 sync
export DSN="postgresql://postgres@/bench?unix_socket=<SOCKET_DIR from output>&port=5433"

# 2. spike nhỏ (1M rows, mỗi phase 15s) — sanity check protocol
python3 harness.py --dsn "$DSN" --layout B --index I1 --shards 32 \
    --rows 1000000 --workers 6 --load-sec 15 --hybrid-sec 15 --read-sec 10
```

Output: `out/result_*.jsonl` (1 dòng/phase) + `out/iostat.log`.

## Chạy trên GitHub Actions

`.github/workflows/pg-shard-fs-bench.yml` — `workflow_dispatch`, mặc định
5M rows cho CI nhanh; full 50M chạy thủ công khi cần (đặt `rows=50000000`,
tăng timeout).

## Chỉ số chính

`rows_per_sec` (LOAD), `read_p95_ms` (HYBRID/READ), `wal_bytes_per_row`
(cố định theo row-length — chỉ so sánh giữa layouts, không giữa FS),
iostat await trong artifact `iostat.log`, và `pg_test_fsync` baseline của từng
job để normalize giữa runner khác nhau.

## Skepticism / limitations

- GH runner 4 vCPU — knee point N đo được là墙面 của environment đó; không
  tuyên bran "production N" mà chỉ path trend.
- ZFS loopback không phản ánh完整 ARC memory dynamics; đã clamp
  `zfs_arc_max=512MB` + `recordsize=8K` + `primarycache=metadata`.
- Resharding (thay đổi `% N`) không nằm trong scope này.
