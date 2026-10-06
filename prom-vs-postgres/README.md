# Prometheus vs PostgreSQL for metrics: measurement harness

One benchmark in [my-benchmarks](https://github.com/hungpham10/my-benchmarks), a collection of measurement
harnesses. Written to back a blog post comparing the two as metric
storage; see `blog/post.md`.

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

## Watching a run

`results/*.json` answers "which database was faster". It does not answer "what
did that run cost the machine", because a phase records only its own average.
A container that peaked at 2 GB and never said so looks identical to one that
sat at 100 MB the whole time.

```sh
make monitor       # Grafana on http://localhost:3000  (admin/admin)
make monitor-check # confirm every scrape target is actually delivering
```

Start it before the run, not after. `make bench` recreates the targets, and
the window you care about is ingest and query, not the idle state.

Five sources feed the dashboard:

| Source | Gives you |
| --- | --- |
| cAdvisor | CPU and memory per container, including both targets |
| `prometheus.exporter.unix` | host CPU, memory, filesystem, disk, network |
| Prometheus under test | head series, samples appended, WAL fsync, blocks loaded |
| `postgres-exporter` | database size, backends, `pg_stat_statements` |
| `results/*.json` | the latency and footprint numbers, already measured |

The Prometheus under test and the monitoring Prometheus are separate on purpose,
with separate volumes. The measured TSDB is the artifact at 4.67 B/sample
against Postgres at 279.76; one scraped target landing in it would move that
headline. `make monitor-check` asserts it is still clean.

### Container CPU is not trustworthy under Colima

`make monitor-check` passes and the container CPU panel still shows nonsense:
cAdvisor reported 8,000–10,000% for PostgreSQL while `docker stats` reported
100%, on a 4-core VM. Container **memory** from the same source is accurate to
within a MiB. Host CPU from `node_cpu_seconds_total` is accurate.

So under Colima, read memory and host CPU off the dashboard, and take
per-target CPU from `results/resource_*.json`, which samples `docker stats`
directly and is what the numbers in the blog come from. On a plain Linux
runner cAdvisor's CPU accounting is fine — this is a cgroup accounting problem
in the Colima VM, not in the collection setup.

### Keeping results off your laptop

`results/` is a git commit away, which is enough to compare runs, but it is not
a dashboard. Grafana Cloud gives the same panels a history that survives the
machine, the way k6 Cloud keeps results server-side:

```sh
cp monitoring/.env.monitoring.example monitoring/.env.monitoring
$EDITOR monitoring/.env.monitoring     # URL, instance ID, access policy token
make monitor-cloud
```

Alloy then pushes to the local stack and Grafana Cloud at once. The token is
read with `sys.env()` and never appears in a committed file;
`monitoring/.env.monitoring` is gitignored.

`monitoring/alloy/config.cloud.alloy` is generated from `config.alloy`, because
Alloy has no conditionals and the two would otherwise be hand-maintained copies
that drift. After editing the local config:

```sh
python3 monitoring/sync_cloud_config.py --write
```

`make monitor-check` fails if you forget. Both configs are validated with
`docker run --rm -v "$PWD/monitoring/alloy:/etc/alloy:ro" grafana/alloy:latest
validate /etc/alloy/config.alloy` — but note that `validate` does not catch a
`labeldrop` rule that also sets `source_labels`, which fails only at runtime as
`failed to evaluate config`. Check the Alloy log after changing the config.

### CI pushes too, on nightly and manual runs

The `schedule` and `workflow_dispatch` jobs start the same stack and push the
same series. `pull_request` runs do not: a PR run is a two-minute smoke test at
ten series, which would bury the real runs, and a PR from a fork is not given
repository secrets at all.

Set three repository secrets once:

```sh
gh secret set GRAFANA_CLOUD_URL
gh secret set GRAFANA_CLOUD_ID
gh secret set GRAFANA_CLOUD_TOKEN
```

Without them the job skips the push, says so in the step log, and still runs
the benchmark. The verification step runs `make monitor-check`, so a push that
stops being accepted fails the job instead of leaving the benchmark to produce
perfect results whose operational half nobody can see.

CI passes `--no-deps` when starting the stack. `postgres-exporter` depends on
postgres, so without it the monitoring stack would be what brings the system
under test up, minutes before `make bench` would have — changing the state the
benchmark starts from. The exporter retries DNS on its own and connects
whenever postgres appears.

### Telling runs apart

Every sample carries `run_id` and `commit`, stamped by a relabel rule:

```
run_id   gh-<run number>.<attempt>   in CI
         20261006T140233Z            locally, a UTC timestamp
commit   the short SHA
```

Without them the history is unusable. cAdvisor's series have constant labels
(`container="bench-postgres"`, `instance="cadvisor:8080"`), so every run writes
to the *same* series and a month of nightly runs is one flat line with no way
to tell which commit produced a spike. The host exporter goes the other way: its
instance is the Alloy container ID, which changes every run, so each one leaves
behind ~70 series that only expire with retention.

In Grafana Cloud, pick runs from the `run_id` variable:

```promql
node_memory_MemAvailable_bytes{run_id=~"$run_id"}
```

To see all runs together instead, match on nothing:

```promql
node_memory_MemAvailable_bytes{run_id=""}
```

`make monitor-check` reports the runs currently in the TSDB, and fails if
`BENCH_RUN_ID` is set but no series carries it. That case is otherwise
invisible: Alloy accepts a rule that has no effect, evaluates it without error,
and simply adds no label, which looks identical to a scrape that never started.

## The recorded run in `results/`

`results/` is a **real run, kept in the repo so the numbers in the write-up can
be checked against the raw JSON without re-running anything.** It was produced
on a GitHub Actions runner (4 CPU / 16 GB) against Prometheus 3.15.0 and
PostgreSQL 18.6, at the full `.env` scale. The exact image digests are in
`results/environment.json`.

The query phase used **300 iterations per cell with 20 warmup**, which is what
`.env` asks for. That phase alone takes about two hours, which is most of the
run. `results/query.json` carries the authoritative `iterations` and `warmup`
and `report.md` renders from there.

What the recorded run shows:

| | Prometheus | PostgreSQL |
| --- | --- | --- |
| Bytes per sample | 4.67 B | 279.76 B (59.9x) |
| Ingest wall time | 16.1 s | 43.8 s |
| Ingest peak memory | 286 MiB | 793 MiB |
| Query peak memory | 293 MiB | 1.21 GiB |
| Query CPU (avg) | 2.2% | 105.7% |

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
monitoring/
  alloy/config.alloy        scrapes, relabel rules, local remote_write
  alloy/config.cloud.alloy  generated: the above plus a Grafana Cloud endpoint
  sync_cloud_config.py      regenerates it, or fails on drift
  check.py                  make monitor-check
  grafana/                  datasource and dashboard, provisioned from disk
  prometheus/               config for the monitoring Prometheus
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
| Grafana dashboard is empty | Run `make monitor-check`. An empty `container` label means cAdvisor attribution is broken again; an empty `pg_*` means the exporter lost its DNS entry after Postgres was recreated. |
| Alloy restart-loops on `expected ], got EOF` | A parse error anywhere in the file reports at EOF. Look for a missing closing quote, not a missing bracket: the error line is the end of the file, not the mistake. |
| Alloy: `remote write receiver needs to be enabled` | `monitoring-prometheus` is missing `--web.enable-remote-write-receiver`. |
| Alloy: `failed to evaluate config` | A rule Alloy accepted at validate time is invalid at runtime. `labeldrop` takes only `regex`, never `source_labels`. |
| Grafana Cloud has no new series | Check `gh secret list`. With no secrets the job skips the push and says so. Otherwise look for `samples_failed_total` in `docker logs bench-alloy`. |
| `monitor-check` says `MISS ... run_id` | Alloy is running an older config or was not restarted after the relabel rules were added. |
| Container CPU shows thousands of percent | cAdvisor CPU accounting under Colima. Use host CPU and `results/resource_*.json`. |
| `postgres-exporter` logs `no such host` | Postgres is not on the compose network. It failed to attach because its host port was taken; point `POSTGRES_PORT` at a free port. |
| Prometheus restart-loops: `open /etc/prometheus/prometheus.yml: no such file or directory` | The config bind mount resolved to nothing. Colima and Docker Desktop share only your home directory into the VM, so a checkout under `/tmp` mounts an empty directory. Keep the repo under `/Users` or `~`. |
