# Can we use Postgres for storing and querying metrics instead of Prometheus?

I recently sat in a technical interview and got a deceptively simple question:

> *Why Prometheus or InfluxDB for metrics, and not PostgreSQL?*

"Of course you can," I said. But that answer is where the interesting part
starts. Postgres can absolutely store metrics. The real question is what it
gives up to do that, and what Prometheus gave up to avoid doing it.

I ended up answering it backwards. I built a benchmark and measured first, then
went back to the TSDB source to explain what I was seeing. The results were
surprising enough that I did not trust them until I could say why.

Here is what I expected, what actually happened, and then why.

## What I expected

Postgres is a database that has been optimised for thirty years. Real indexes,
real partitioning, a query planner that has seen harder problems than "sum these
numbers by job". If I put metrics in a table with sensible partitioning, I
expected queries to be fine. Two or three times slower than Prometheus, maybe.
Slow enough to notice, not slow enough to matter.

On disk I expected Prometheus to win, but not by much. Compression is a trick,
and I assumed the gap would be somewhere around 10x.

I was wrong about both.

## The benchmark

One deterministic dataset, loaded into Prometheus and into two Postgres schemas.

- Prometheus 3.15.0 and PostgreSQL 18.6
- 1.5 CPU and 4 GB each. Identical, on purpose
- 20,000 series, 180 samples each, 3.6M samples, 45 minutes at 15s
- Targets loaded one at a time, never together, so they never compete

Two Postgres schemas, because testing only one would not be fair:

- **`metrics_jsonb`** — labels in a jsonb document, GIN index. What most people
  write first.
- **`metrics_norm_brin`** — identity in its own table, BRIN on time. The best
  Postgres can do natively.

Both time-partitioned.

One thing worth knowing before you run this. Prometheus will not accept a
backfill. Remote-write rejects samples more than 10 minutes ahead of wall clock
(`maxAheadTime`), and the Head rejects anything older than about an hour. The
usable range is roughly `[now - 50m, now + 10m]`.

A 24-hour dataset cannot be loaded through the real write path at all. If you
have read a "Prometheus vs Postgres at scale" comparison, this is probably why
it is not reproducible.

Parity first, because none of the rest matters otherwise: 1,800,000 samples,
identical count on all three targets, identical sum `183888348.7429`. Not close
to identical. Exact.

## Results

### Disk

WAL excluded, since WAL is temporary and not a property of the stored data.

| Target | On disk | Per sample |
| --- | --- | --- |
| Prometheus | 16.0 MiB | **4.67 B** |
| PostgreSQL | 960.3 MiB | **279.72 B** |

**59.9x.** I guessed 10x.

| Relation | Heap | Indexes | Index/heap |
| --- | --- | --- | --- |
| `metrics_jsonb` | 589.4 MiB | 48.5 MiB | 0.08 |
| `metrics_norm_brin` | 179.7 MiB | 108.8 MiB | 0.61 |
| **total** | **769.1 MiB** | **157.2 MiB** | **0.20** |

Look at that index ratio. The normalised schema spends 61% of its heap on
indexes. BRIN is cheap per page, but it degrades here because samples are
written per series, not in time order, so min/max per range stays wide. That is
the write pattern, not a broken index.

### Ingest

| Target | Wall time | CPU (avg / peak) | Peak memory |
| --- | --- | --- | --- |
| Prometheus | 14.4 s | 9.0% / 16.0% | 217.3 MiB |
| PostgreSQL | 40.3 s | 62.9% / 83.7% | 780.0 MiB |

### Latency

Warm, serial, one connection. 100 iterations per cell, Prometheus stepped at
300s so both return a comparable number of rows.

| Shape | Prometheus | PG jsonb | PG norm+BRIN |
| --- | --- | --- | --- |
| q1 `sum(rate(http_requests_total[5m]))` | **132 / 160 ms** | 16095 / 16179 ms | 1246 / 1262 ms |
| q2 `sum by (job) (queue_depth)` | **91 / 117 ms** | 1103 / 1139 ms | 573 / 606 ms |
| q3 `quantile_over_time(0.9, queue_depth{job="job-00"}[1h])` | 42 / 86 ms | 243 / 247 ms | **35 / 36 ms** |
| q4 `sum(queue_depth{job="job-00"})` | **5.0 / 8.1 ms** | 217 / 220 ms | 17.0 / 17.7 ms |
| q5 single series, 15 min | 0.75 / 0.86 ms | 62.8 / 65.3 ms | **0.49 / 0.53 ms** |

p50 / p95.

- **q1, counter rate.** About 100x on the normalised schema, 120x on jsonb.
- **q2, grouped sum.** The closest to a fair fight. Prometheus still wins 10x.
- **q3, percentile.** Postgres wins.
- **q4, one job, whole window.** Prometheus wins 43x. The label index working as
  designed.
- **q5, one series, 15 min.** Postgres wins.

Also worth noting: normalising the schema beat jsonb on all five shapes, 13x on
q1 alone. If you have to use Postgres, normalise.

### Resources during the query phase

| Target | CPU (avg / peak) | Peak memory |
| --- | --- | --- |
| Prometheus | 1.6% / 101.7% | 236.8 MiB |
| PostgreSQL | 104.4% / 156.4% | **1.2 GiB** |

Prometheus averaged 1.6% of one core across the whole suite. Postgres was
saturated the entire time.

## What surprised me

**The gap was two orders of magnitude, not one.** I expected 3x on queries. The
counter rate query took 16 seconds on jsonb and 132 ms on Prometheus. That is not
"Prometheus is better engineered". That is a different category of operation.

**The jsonb schema was not merely slower, it was unusable.** 16 seconds for a
query that Prometheus answers in 160ms. My expectation was that jsonb would be
maybe 1.5x behind the normalised schema. It was 13x behind on q1. Normalising
turned out to matter far more than any Postgres tuning knob.

**Postgres used 5x the memory and never stopped working.** 1.2 GiB peak and
pinned at 104% CPU for the entire 36-minute suite, while Prometheus idled at
1.6%. I had not expected the *resource* difference to be this lopsided. It
matters more than latency in practice, because it decides how many series you
can afford to keep.

**The two shapes where Postgres won were completely predictable.** Percentiles
and single-series lookups. Once I read the query engine I could point at the
exact reason for both, and it was not a close call in either direction.

At this point I had numbers I did not understand. So I read the code.

## Why: metrics are not rows

Four things about metrics data make it different from normal relational data.
These decide everything above.

**Samples only go forward.** They arrive at roughly increasing timestamps and
never get updated in place. This is what makes heavy compression possible.

**A row has no ID of its own.** In Prometheus a series *is* its label set. There
is no primary key. High cardinality is built into the model, not a mistake
anyone made.

**Queries start from labels and end at time.** Prometheus keeps two separate
structures for this: an index keyed by label, and time-ordered chunks. A
relational database picks one indexing strategy per set of columns.

**Deletion is rare.** Prometheus writes a tombstone and applies it later, at
compaction. `Block.Delete` removes zero bytes.

That last one alone should be a warning sign. In any store where you cannot
cheaply delete a row, you will grow forever.

## Why: the one assumption

Read `tsdb/chunkenc/xor.go` and the whole disk result falls out. The encoder
remembers the last timestamp and the last gap. For each new sample:

```
tDelta = t - a.t
dod    = tDelta - a.tDelta
```

Scrapes on a fixed schedule give `dod == 0` after the first two samples. Zero
costs one bit. A 120-sample chunk spends about 15 bytes on timestamps.

Then:

- A constant offset is free. Shift every timestamp by the same amount, `dod`
  stays 0. This is why `scrape_offset` works so well.
- Constant clock drift is free too. A clock running slightly fast still gives a
  constant `dod`. Only *fluctuation* costs bits.
- One bad sample does not stay bad. It perturbs `dod` once, the next sample
  cancels it, and packing goes back to 1 bit.

This is the whole 60x. Prometheus gets to spend one bit on a regular interval
because it is allowed to assume the data arrives in time order. Postgres is not
allowed to make that assumption, so it stores 279 bytes where Prometheus stores
4.67. That number is not a tuning gap and it will not close as your dataset
grows.

Here is the part I did not expect. Go timer jitter in the scrape loop perturbs
the scrape interval, which pushes every sample off the 1-bit path. At scale that
is hundreds of GB per year, caused by a scheduler.

Prometheus cannot fix this in the encoder. The encoder is not allowed to throw
information away, so there is no "close enough" rounding. Instead there is a
flag called `scrape.adjust-timestamps` that snaps timestamps back to the
intended schedule.

The storage format is shaped by the runtime sitting above it.

## Why: compaction is not the compression story

I got this wrong before I read the code, and I have seen it claimed in blog
posts, so: compaction does **not** improve your compression ratio.

`storage/merge.go` only re-encodes chunks that overlap in time. Head chunks
landing in a block do not overlap, so they pass through byte for byte. There is
no step that merges four 120-sample chunks into one big one. Even when chunks
do overlap, the output is cut at 120 samples again, so chunk size never grows.

Bytes per sample stays flat.

What compaction actually does:

| What | How much |
| --- | --- |
| Drop duplicate chunks from replication | 100% |
| Merge chunks that overlap in time | can improve, can get worse |
| Apply tombstones | 100% of the deleted part |
| Drop series left with no chunks | 100% |

So compaction is not there to compress harder. It is there to reduce the number
of blocks a query has to open, and to clean up deletions. The cost is write
amplification: every level rewrites everything under it.

If you want faster queries, block size is the lever. Not compression.

## Why: which PromQL functions are actually native

This is where the two Postgres wins come from, and neither is a coin flip.

`rate()` is a real Prometheus win. It computes `last - first + sum of resets`,
walking a slice that is already sorted because the data was appended in time
order. In Postgres the same thing is `lag(value) OVER (...)`, a window function
that forces a sort of the partition. That is q1, and it is a 100x gap with a
structural cause.

`histogram_quantile()` on a native histogram is the strongest case Prometheus
has. It works on one sample's buckets, so it is O(buckets) rather than
O(samples).

`quantile_over_time()` is not a win, and people assume it is. It copies every
float into a heap and does a full sort in RAM, under a global sample cap. The
Postgres equivalent, `percentile_cont WITHIN GROUP (ORDER BY value)`, sorts in
the database and has no such cap. That is q3.

q5 is the simplest case in the whole suite: one series, one block, 15 minutes.
There is almost nothing for a specialised engine to exploit there, and the
planner wins.

One more thing worth knowing: PromQL is not streaming. `rangeEval`
materialises the entire result before calling any function, capped by
`--query.max-samples` at 50,000,000.

## Where Postgres wins

Not hedging on these.

**Selective delete.** Postgres drops a partition in O(1). Prometheus writes a
tombstone and pays at the next compaction. A block with more than 5% tombstoned
series gets rewritten regardless of age. Deleting one popular metric costs a
rewrite of every block it touches.

**Retention.** Dropping old time ranges is exactly what `DROP PARTITION` does.

**Percentiles over many series.** q3 above, twice.

**One database instead of several.** Joining metrics to business data, or
running an ad-hoc query, is a SQL problem Prometheus cannot answer at all.

And the honest framing: Postgres wins here by being general, not by being worse
at time series. It is not losing on speed. It is losing on **disk**, and that
number does not get better with scale.

## Caveats

Read these before quoting any number above.

- **Nothing compacted.** 45 minutes never reaches the 3h threshold, so 0 blocks
  were written. The Prometheus disk number is the head number, not settled
  steady state. Compaction costs that this run never paid.
- **The short window flatters Postgres on latency.** 45 minutes is the planner's
  best case. Postgres gets worse as the range grows. Prometheus gets worse more
  slowly.
- **The short window also hurts Prometheus on bytes/sample.** At 180 samples per
  series, per-series overhead has very little to amortise over.
- **Scale is small on purpose**, so Postgres fixed costs (catalog, WAL,
  autovacuum) are a bigger share of its total than they would be in production.
- **Warm, serial, one connection.** No parallelism on either side. Postgres
  parallelises scans across workers; this does not test that.
- **P99 over 100 iterations is basically the maximum.** Do not quote it as tail
  latency.
- **Write amplification and delete churn are argued from source, not measured.**
  I did not run long enough to compact anything.
- **Images are `:latest`.** Keep the digests from `results/environment.json` or
  these numbers cannot be reproduced.

## Conclusion

Postgres is not a bad metrics store. It is a good general store paying for
generality in a currency that matters here: disk.

4.67 bytes per sample versus 279.72 is not a tuning gap. It comes down to one
thing. Prometheus can assume samples arrive in time order and spend one bit on a
regular interval. Postgres cannot assume that, so it pays.

Prometheus does compress well, but not *because of* compaction. It compresses
well because it only ever appends in time order. Those are different claims and
the second one is the true one.

What Prometheus buys with those saved bytes is real but narrower than the pitch
suggests. Faster on rate, grouping and selective reads. Not faster on
percentiles. Deletion and joins are strictly harder.

So use Prometheus when metrics are most of what you store and you query them the
way monitoring queries them. Use Postgres when metrics are one table among
several, when you need joins, or when 60x disk does not bother you.

Both are right answers to different questions. Knowing which question you are
asking is the useful part.

---

## Reproduce it

```sh
make build
make bench
```

Harness, raw JSON and CI: <https://github.com/hungpham10/my-benchmarks/prom-vs-postgres>

The recorded run used 100 iterations per cell rather than the 300 in `.env`, to
keep it under a sane wall-clock budget. `results/query.json` has the real count.
