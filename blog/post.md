# Can we use Postgres for storing and querying metrics instead of Prometheus?

I recently sat in a technical interview and got a deceptively simple question:

> *Why Prometheus or InfluxDB for metrics, and not PostgreSQL?*

"Of course you can," I said. But that is where it gets interesting — Postgres
can absolutely store metrics. The real question is what it gives up in order to
do that, and what Prometheus gave up in order to avoid doing it.

So I did two things. First I read the TSDB source to find out what Prometheus
actually builds. Then I built a harness and measured the difference.

There are two answers here, at two different levels.

**The architectural answer** is that Prometheus is small and fast because of one
assumption it is allowed to make and Postgres is not: that samples arrive in
time order. That single assumption is worth roughly 60x on disk, and it shows up
all over the codebase — the chunk encoder, the label index, the compaction path.

**The measured answer** comes from a harness that loaded one deterministic
3.6M-sample dataset into Prometheus and into two carefully designed PostgreSQL
schemas, then measured disk, latency and resource use under an identical
envelope. Prometheus won 9 of the 11 dimensions I compared. PostgreSQL won the
two it should — percentiles over many series, and single-series short reads.

Neither half is much use alone. The architecture explains *why* the numbers come
out where they do, including the two shapes where Postgres still wins. Everything
below comes from one real run; the harness, the raw JSON and the CI workflow are
public, so every number is checkable.

## Why metric data is not relational data

Before the measurements, the design constraints — because they explain all of
the numbers.

**1. Metrics are append-mostly and time-ordered.** Samples arrive at roughly
increasing timestamps and are never updated in place. This is the property that
makes aggressive compression possible at all.

**2. A row has no identity of its own.** In Prometheus, a series *is* its label
set. There is no primary key, no row ID. High cardinality is intrinsic to the
data model, not a modelling mistake.

**3. Two opposite access patterns coexist.** Queries start from labels and then
want a time range. Prometheus therefore keeps two separate structures: a
symbol/postings index keyed by label, and time-ordered compressed chunks. A
relational database has to pick one indexing strategy per column set.

**4. Deletion is rare and coarse.** Prometheus appends *tombstones* and applies
them at the next compaction. `Block.Delete` removes zero bytes.

Point 2 is worth pausing on, because it is the one that makes PostgreSQL feel
like the wrong tool. `labels @> '{"job":"job-00"}'` matches rows. Prometheus's
`Postings` index returns `[]uint64` series references — not rows, not samples.
Everything else follows from that difference.

## What the storage format does with that

### Delta-of-delta: 1 bit for a regular interval

The encoder in `tsdb/chunkenc/xor.go` keeps two fields of state: the previous
timestamp and the previous gap. For each sample:

```
tDelta = t - a.t               // ①
dod    = tDelta - a.tDelta     // ② the delta of the delta
```

If scrapes land on a fixed interval, `dod == 0` after the first two samples, and
zero costs one bit. A 120-sample chunk spends roughly 15 bytes on its
timestamps.

The consequences are more interesting than the headline:

- **A constant offset is free.** Shifting every timestamp by a constant leaves
  `tDelta` untouched, so `dod` stays 0.
- **Constant clock drift is also free.** A clock running slightly fast yields a
  constant `tDelta`. Only *fluctuation* costs bits.
- **One spike self-corrects.** A single jittered sample perturbs `tDelta`; the
  next sample's `dod` cancels it and packing returns to 1 bit. The cost of an
  incident is bounded, not cumulative.
- **There is no "close enough" rounding.** `dod == 1` costs 16 bits, the same as
  `dod == 8191`. The encoder is not permitted to discard information.

That last point has a consequence that surprised me: **a Go runtime detail
becomes a storage cost.** Timer jitter in the scrape loop perturbs the scrape
interval, which pushes every sample off the 1-bit path. At scale that is
hundreds of GB per year. It is why `scrape.adjust-timestamps` exists — it snaps
timestamps back to the intended schedule within a 2 ms tolerance. The fix
cannot live in the encoder, because the encoder is not allowed to lose
information.

### Compaction does not make compression better

This is the most misread part of the TSDB, and it matters for any comparison.

`compactChunkIterator.Next` in `storage/merge.go` only re-encodes chunks that
overlap **in time**. Head chunks landing in a block do not overlap, so they pass
through byte-for-byte. There is no "merge four 120-sample chunks into one
480-sample chunk" step. Even on the overlap path, output is still cut at 120
samples, so chunk size never grows.

**Bytes per sample is essentially unchanged by compaction.** What compaction
actually buys:

| Gain | How much |
| --- | --- |
| Drop byte-identical duplicate chunks | 100%, only from replicated blocks |
| Merge time-overlapping chunks | Ratio can improve *or degrade* |
| Apply tombstones | 100% of the deleted portion |
| Drop series with no chunks left | 100% |

So query speed is not improved by decompressing less — the same samples are
decoded either way. It improves because there are **fewer blocks to fan out
over**. That is why `--storage.tsdb.block-range` moves query latency far more
than it moves compression.

Compaction also costs write amplification: each level rewrites every sample, so a
sample is written to disk roughly `log₃(32h/2h)` times over its life. That is
the price of opening one file instead of thirty.

### Where the query functions are genuinely native

PromQL is not streaming. `rangeEval` materialises the entire input before
calling any function, guarded by `--query.max-samples` (default 50,000,000).

Where it is genuinely time-series-native:

- **`rate`** — `last - first + sum(previous value at each reset)`, then
  extrapolation. The loop walks a slice that is *already sorted*, because the
  data was appended in time order. In PostgreSQL the equivalent is
  `lag(value) OVER (...)`, a window function that forces a sort of the
  partition. This is a real, defensible advantage.
- **`irate` / `idelta`** — read only the last two samples. O(1) on identical
  storage.
- **`histogram_quantile` on a native histogram** — operates on one sample's
  buckets, so O(buckets) rather than O(samples). This is the strongest native
  claim available, and it is conditional on using native histograms.

Where it is **not** a strength: **`quantile_over_time`**. It copies every float
into a heap and calls a full `sort.Sort`. PostgreSQL's `percentile_cont`
*WITHIN GROUP (ORDER BY value)* sorts in the database and is not subject to
`--query.max-samples`. Both are O(N log N), and PostgreSQL has the better
memory story.

And extrapolation semantics — `durationToStart`, `extrapolationThreshold` — are
PromQL semantics, not storage semantics. Anyone storing metrics in PostgreSQL
has to reimplement them to match `rate()`.

## The benchmark

- **Prometheus 3.15.0** and **PostgreSQL 18.6**, both `:latest`.
- **Identical envelope: 1.5 CPU and 4 GB each.** This is what makes the
  comparison a comparison. Uncapped, Prometheus could spend three times the RAM
  PostgreSQL uses and still win, and "peak memory" would measure the machine
  rather than either design.
- **20,000 series × 180 samples = 3.6M samples**, 45 minutes at 15s.
- Both targets loaded **sequentially**, never together.

Two PostgreSQL schemas, because one would not be a fair fight:

- **`metrics_jsonb`** — labels in a `jsonb` document, GIN with `jsonb_path_ops`
  for containment. What most people write first. GIN can tell you *which rows
  match* but never *what they contain*, so every match needs a heap fetch.
- **`metrics_norm_brin`** — identity in its own `series_dim` table, BRIN on
  time, exactly as Prometheus keeps identity separate from samples. The best
  PostgreSQL can do natively.

Both are time-partitioned, so partitioning is a constant rather than a variable.

### The 45-minute ceiling is not laziness

Prometheus will not accept a backfill. Remote-write rejects samples more than
10 minutes ahead of wall clock (`maxAheadTime`), and the Head rejects anything
older than `MaxTime - chunkRange/2` — about one hour at the default 2h range.
The usable envelope is roughly `[now - 50m, now + 10m]`.

**A 24-hour dataset cannot be loaded through the real write path at all.** Volume
has to come from cardinality instead. That is why this benchmark is wide and
short, and it is the single biggest reason to distrust any "Prometheus vs
Postgres at scale" comparison you have read.

### Parity first

1,800,000 `queue_depth` samples, identical count on all three targets, identical
sum `183888348.7429`. Correctness before performance.

## Results

### Disk

WAL excluded, because WAL is transient rather than a property of the stored data.

| Target | On disk | Per sample |
| --- | --- | --- |
| Prometheus | 16.0 MiB | **4.67 B** |
| PostgreSQL (both schemas) | 960.3 MiB | **279.72 B** |

**59.9x.**

| Relation | Heap | Indexes | Index/heap |
| --- | --- | --- | --- |
| `metrics_jsonb` | 589.4 MiB | 48.5 MiB | 0.08 |
| `metrics_norm_brin` | 179.7 MiB | 108.8 MiB | 0.61 |
| **total** | **769.1 MiB** | **157.2 MiB** | **0.20** |

WAL for the same run: Prometheus 45.4 MiB, PostgreSQL 1.9 GiB.

Normalising the schema is worth 3.3x on its own — and note the index ratio.
`metrics_norm_brin` spends 61% of its heap on indexes against 8% for `jsonb`.
BRIN is cheap per page, but it degrades here because samples are written *per
series*, not in time order, so min/max per range stays wide. That is a property
of the write pattern, not a defect in the index.

### Ingest

| Target | Wall time | CPU (avg / peak) | Peak memory |
| --- | --- | --- | --- |
| Prometheus | 14.4 s | 9.0% / 16.0% | 217.3 MiB |
| PostgreSQL | 40.3 s | 62.9% / 83.7% | 780.0 MiB |

2.8x faster, 3.6x less memory. Expected: PostgreSQL is writing a heap tuple plus
index entries plus WAL per sample, where Prometheus appends to a compressed
buffer and a flat WAL record.

### Query latency

Warm, serial, single connection, 100 iterations per cell after 10 warmup,
Prometheus stepped at 300s so both sides return comparable row counts. p50 / p95:

| Shape | Prometheus | PG jsonb | PG norm+BRIN |
| --- | --- | --- | --- |
| q1 `sum(rate(http_requests_total[5m]))` | **132 / 160 ms** | 16095 / 16179 ms | 1246 / 1262 ms |
| q2 `sum by (job) (queue_depth)` | **91 / 117 ms** | 1103 / 1139 ms | 573 / 606 ms |
| q3 `quantile_over_time(0.9, queue_depth{job="job-00"}[1h])` | 42 / 86 ms | 243 / 247 ms | **35 / 36 ms** |
| q4 `sum(queue_depth{job="job-00"})` | **5.0 / 8.1 ms** | 217 / 220 ms | 17.0 / 17.7 ms |
| q5 single series, 15 min | 0.75 / 0.86 ms | 62.8 / 65.3 ms | **0.49 / 0.53 ms** |

Read the shapes, not the row:

- **q1 is the headline, 100x.** Counter rate is where time-ordered storage pays
  off completely. Prometheus walks a sorted slice; PostgreSQL sorts a window
  partition.
- **q2 is 10x** and is the closest thing to a neutral comparison — grouped
  aggregation, both engines comfortable. Prometheus still wins comfortably,
  because `sum by (job)` walks 10,000 sorted series instead of re-deriving
  grouping through a hash aggregate over a heap scan.
- **q4 is 43x** and shows the postings index working as designed: one job
  resolves to a set of series refs, and only those chunks are read.
- **q3 and q5 are PostgreSQL wins**, and they are not close calls on
  architecture. q3 is `quantile_over_time` sorting in Prometheus RAM under a
  global sample cap. q5 is a single series in a single block — there is
  essentially nothing for a specialised engine to exploit, and PostgreSQL's
  planner wins.

Normalising the schema beats `jsonb` on **all five** shapes — 13x on q1, 2x on
q2, 7x on q4. If you must use PostgreSQL, normalise.

### Resources during the query phase

Full 100-iteration suite, 2163 s wall:

| Target | CPU (avg / peak) | Peak memory |
| --- | --- | --- |
| Prometheus | 1.6% / 101.7% | 236.8 MiB |
| PostgreSQL | 104.4% / 156.4% | **1.2 GiB** |

Prometheus averaged 1.6% of a core across the entire suite. PostgreSQL was
saturated the whole time.

## Where PostgreSQL wins

I want these stated as plainly as the wins above.

**Selective deletion.** PostgreSQL drops a partition in O(1). Prometheus writes a
tombstone and pays for it at the next compaction — a block with more than 5%
tombstoned series gets rewritten regardless of age. Deleting one popular metric
in Prometheus costs a rewrite of every block it touches.

**Retention management.** Time-range retention that drops whole blocks is exactly
what `DROP PARTITION` does. Prometheus's version is fine; it is not an advantage.

**Percentiles over many series.** Shown above, twice.

**One database to operate.** Joining metrics to business data, or running a
non-time query against the same box, is a SQL problem Prometheus cannot answer
at all.

**The 2 GiB WAL.** PostgreSQL's WAL is large *for this workload*, because it is
writing heap tuples. That is not a defect in its WAL design — MVCC has to record
undo — but it is not something to hand-wave either.

And the honest framing of all of it: **PostgreSQL wins by being general, not by
being worse at time series.** It is not rejected for performance. It is rejected
for **disk** — 279 B/sample against 4.67 B/sample. That ratio does not improve
with scale; it is structural.

## Caveats

I would not quote any of the above without these.

- **Nothing was compacted.** 45 minutes never reaches the 3h `compactable()`
  threshold, so **0 compacted blocks**. The Prometheus disk figure is the *head*
  figure, not settled steady-state. Compaction adds write amplification that this
  run never paid.
- **The window flatters PostgreSQL on latency.** 45 minutes is the planner's best
  case — a small, fully-cached working set. PostgreSQL's q1 cost grows with the
  range scanned; Prometheus' does not grow nearly as fast.
- **The window penalises Prometheus on bytes/sample.** At 180 samples per series
  the per-series overhead (index entries, series refs, block metadata) is
  amortised over very little. The 4.67 B/sample figure will improve somewhat at
  steady state; PostgreSQL's will not improve much.
- **Scale is small on purpose**, so PostgreSQL's fixed costs (catalog, WAL,
  autovacuum) are a larger share of its total than in production. That flatters
  it on absolute footprint.
- **Warm, single connection, serial.** No parallelism on either side.
  PostgreSQL parallelises a scan across workers; this does not test that.
- **Percentiles are warm**, and P99 over 100 iterations is close to the maximum,
  not a tail latency. Do not quote it as one.
- **Write amplification and delete churn are argued from source, not measured.**
  I did not run a long enough window to compact anything.
- **Images are `:latest`.** Record the digests — `results/environment.json` has
  them — or these numbers cannot be reproduced and silently expire.

## Conclusion

PostgreSQL is not a bad metrics store. It is a good *general* store that pays
for generality in a currency that matters here: **disk**. 279 B/sample against
4.67 B/sample is not a tuning gap. It comes from not being able to assume data
arrives in time order.

Prometheus compresses well because it may assume monotonic, near-uniform
timestamps and spend one bit on a regular interval. PostgreSQL cannot make that
assumption, so it pays for generality. That is the sharpest single point of the
comparison, and it is a *design* difference, not an implementation gap.

What Prometheus buys with those saved bytes is real, but narrower than the
marketing suggests. It is faster on rate, on grouping, and on selective reads. It
is **not** faster on percentiles, it does not compress better *because of*
compaction, and it makes deletion and joins strictly harder.

So: pick Prometheus when metrics are most of what you store and you query them
the way monitoring actually queries them. Pick PostgreSQL when metrics are one
table among several, when you need joins or ad-hoc SQL, or when 60x disk growth
does not matter to you. Both are correct answers to different questions, and the
useful thing about this comparison is knowing which question you are actually
asking.

---

## Reproduce it

```sh
make build
make bench
```

Full harness, raw JSON and a GitHub Actions workflow: REPO_URL

Every number above comes from `results/` in that repository, and
`results/environment.json` pins the image digests the run resolved to. The
recorded run used 100 iterations per cell rather than the 300 in `.env` to stay
inside a sensible wall-clock budget; `results/query.json` carries the true
count.
