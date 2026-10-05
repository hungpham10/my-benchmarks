# Prometheus vs PostgreSQL for metrics: measurement harness

Loads one deterministic 3.6M-sample dataset (20k series x 45 min @ 15s) into
Prometheus and into two PostgreSQL schemas, then measures three things:
**disk footprint, query latency percentiles, and resource consumption.**
Everything goes through the real write and query paths — nothing is pre-built
with `promtool tsdb create-blocks-from`.

`results/report.md` is the output.

## What it measures, and why each part exists

| Measurement | Why it is the interesting one |
| --- | --- |
| Bytes per sample | The only disk figure that survives a change of scale. Raw byte totals mean nothing without the sample count next to them. |
| p50 / p75 / p95 / p99 per query | Percentiles separate "usually fast" from "occasionally unusable". A mean hides the tail, and the tail is what an alerting system feels. |
| CPU and memory inside a fixed, identical envelope | The cost of the storage format — and whether it fits in the budget a real deployment would give it. |

Five query shapes, each chosen to probe one claim rather than to pad the
table. `bench/queries.py` carries the intent and the expected winner inline;
the report repeats both, so a reader can see when a result contradicts the
prediction instead of quietly picking the favourable ones.

## The resource envelope

Prometheus and PostgreSQL get an **identical** budget: `TARGET_CPUS=1.5` and
`TARGET_MEMORY=4G` each, out of the 4 vCPU / 10 GB the VM provides. The
leftover core and ~2 GB go to the bench runner and the OS.

This is what makes the comparison a comparison. Uncapped, Prometheus could
spend three times the RAM PostgreSQL uses and still be declared the winner, and
"peak memory" would measure the ceiling of the machine rather than of either
design. A query that OOMs or starves on CPU inside the envelope is a real
property of "what fits here", not a measurement artefact — that is the number a
reader actually wants.

PostgreSQL's settings are sized to its half of the envelope rather than to the
whole VM (`shared_buffers=1GB`, `work_mem=64MB`, `max_wal_size=2GB`), which is
what you would actually configure for a 4 GB container.

Both images under test run on `:latest` so the comparison is against current
software, which also means **a rerun six months from now is not the same
benchmark** — record the resolved digests alongside any published numbers.

## Prerequisites

- Colima or Docker Desktop **must be running**, sized for those limits:

  ```sh
  colima start --cpu 4 --memory 10 --disk 80
  ```

- ~12 GB free disk.
- Python 3.9+ on the host for `bench/*.py`. No other host dependencies —
  numpy, psycopg and cramjam all live inside the image.
- `make build` needs network access for pip.

## Run it

```sh
make build     # once
make bench     # up -> ingest -> footprint -> query -> parity -> report
```

Phases are separately runnable (`make ingest`, `make query`, …) because they
are separately diagnosable. Results land in `results/*.json` and each phase
writes independently, so a failure in one does not discard the others.

To smoke-test before the real run, shrink `.env`:
`JOBS=2 INSTANCES_PER_JOB=5 WINDOW_MINUTES=15`.

## The recorded run in `results/`

`results/` is a **real run, kept in the repo so the numbers in the write-up can
be checked against the raw JSON without re-running anything.** It was produced
by Prometheus 3.15.0 and PostgreSQL 18.6, at the full `.env` scale.

One caveat worth stating plainly: the recorded latency run used **100
iterations with 10 warmup**, overridden on the command line to keep the run
inside a sane wall-clock budget. `.env` and `results/environment.json` still
say 300/20, so they do *not* describe the run that produced `query.json`.
`results/query.json` is authoritative and carries its own `iterations` and
`warmup`, and `report.md` renders the numbers from there.

What the recorded run shows:

| | Prometheus | PostgreSQL |
| --- | --- | --- |
| Bytes per sample | 4.67 B | 279.72 B (59.9x) |
| Ingest wall time | 14.4 s | 40.3 s |
| Ingest peak memory | 217 MiB | 780 MiB |
| Query peak memory | 237 MiB | 1.2 GiB |

Prometheus wins 9 of the 11 compared dimensions; PostgreSQL wins two query
shapes (single-series percentile, single-series short window). Read
`results/report.md` for the per-shape percentiles and
[the caveats](#things-that-will-bite-you) before quoting any of it.

## Running it on GitHub Actions

`.github/workflows/benchmark.yml` runs the whole harness on `ubuntu-latest`
(4 CPU / 16 GB) and uploads `results/` as an artifact. It is triggered
manually and nightly — deliberately **not** on every push, because the query
phase alone is ~36 minutes at full scale.

```sh
# full scale, ~40 min
gh workflow run benchmark.yml

# fast check, ~2 min
gh workflow run benchmark.yml -f scale=smoke
```

The runner needs the same shape as the local VM: the envelope in `.env` gives
each target 1.5 CPU / 4 GB, so 8 GB of the 16 GB goes to the systems under
test and the rest to the runner and the OS. Free-tier runners (2 CPU / 7 GB)
are too small and will produce numbers that measure the runner.

## Layout

```
Dockerfile            one image, used for both ingest and the query suite
docker-compose.yml    prometheus, postgres, bench
.env                  scale and benchmark knobs — the single source of truth
generator/
  dataset.py          series model, deterministic from SEED
  values.py           AR(1) gauge and cumsum counter generation
  protobuf.py         hand-rolled remote-write encoder
  copyfmt.py          PostgreSQL binary COPY framing
  generate.py         drives the load into both targets
postgres/init/        two schemas: labels-as-jsonb, and identity+BRIN
prometheus/           server config
bench/
  queries.py          the five query shapes, PromQL and SQL side by side
  bench.py            latency loop and percentiles
  parity.py           proves both targets hold the same data
  run.py              phase orchestration, footprint and resource sampling
  report.py           renders results/report.md
```

## The two PostgreSQL schemas

Not decoration — they bracket the design space, and the pair is chosen to
test the two structural findings in the write-up:

- **`metrics_jsonb`** — labels in a `jsonb` document, GIN for containment.
  What most people write first. GIN can tell you *which rows match* but never
  *what they contain*: it sets `amcanreturn = NULL`, so every match needs a
  heap fetch.
- **`metrics_norm_brin`** — identity in its own table, BRIN on time. The best
  Postgres can do natively, and it degrades here precisely because samples
  are written per series rather than in time order.

## Things that will bite you

- **Every query passes an explicit time range, and the window is capped at
  about an hour.** The dataset is timestamped in the *future* because
  Prometheus refuses samples older than `MaxTime - chunkRange/2` (one hour at
  the default 2h block range) — and remote-write refuses anything more than
  10 minutes ahead of wall clock (`maxAheadTime`,
  `storage/remote/write_handler.go:53`). The usable envelope is roughly
  `[now - 50m, now + 10m]`. A 24h dataset cannot be loaded through the real
  write path at all; volume comes from cardinality instead. `time=now()`
  returns nothing, correctly.
- **Nothing gets compacted.** 45 minutes is well under the 3h
  `compactable()` threshold, so Prometheus keeps everything in the head. The
  disk figure is head chunks, not settled blocks.
- **Targets are loaded sequentially, never together.** A shared 4-core VM
  would make the two ingest numbers a measurement of the scheduler.
- **Percentiles need many iterations.** 300 is the floor. At 30 samples a P95
  is indistinguishable from the maximum.
- **Scale is small on purpose.** At this size PostgreSQL's fixed costs
  (catalog, WAL, autovacuum) are a larger share of its total than they would
  be in production, which flatters it. Label the measured run as such, and
  label any extrapolation as computed rather than measured.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Prometheus: `unknown long flag '--storage.tsdb.block-range'` | The flag was removed upstream; defaults already give the 2h head chunk range. |
| Prometheus: `field retention_time not found` | Retention lives in `prometheus.yml` under `storage: tsdb: retention: time:`. |
| Postgres: `unused mount/volume`, refuses to start | PostgreSQL 18 moved the data directory; mount the volume at `/var/lib/postgresql`. |
| remote-write `400 s2: corrupt input` | `cramjam.snappy.compress` writes the framed stream; the receiver wants raw block. Use `compress_raw`. |
| remote-write `400 timestamp is too far in the future` | `maxAheadTime = 10m`. Shorten `WINDOW_MINUTES` or lower `AHEAD_MINUTES`. |
| remote-write returns 2xx but Prometheus is empty | Check the log for `duplicated_label`: each `TimeSeries` needs its own field-1. |
| `the query has 0 placeholders but 2 parameters` | psycopg3 wants `%s`, not `$1`. |
| PromQL returns nothing | Query has no explicit time range. The data is future-dated. |
| `parity` fails on count only | The head has not been replayed, or ingest is still running. |
| `footprint` reports 0 bytes | The containers are down, or `du` failed; check `results/footprint.json` for `null`. |
| `build` fails on pip | No network. The image build needs PyPI. |
| Ports already bound | Change `PROMETHEUS_PORT` / `POSTGRES_PORT` in `.env`. |
| Prometheus restart-loops: `open /etc/prometheus/prometheus.yml: no such file or directory` | The config bind mount resolved to nothing. Colima and Docker Desktop share only your home directory into the VM, so a checkout under `/tmp` mounts an empty directory. Keep the repo under `/Users` or `~`. |
