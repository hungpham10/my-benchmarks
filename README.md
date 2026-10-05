# my-benchmarks

Measurement harnesses. One directory per benchmark: a question, a reproducible
way to answer it, and the raw numbers from a real run.

These back blog posts. Two rules hold across everything here — every figure
published must be traceable to a file in this repo, and every harness must be
runnable by a reader who distrusts the number.

## Benchmarks

| Benchmark | Question | Headline |
| --- | --- | --- |
| [prom-vs-postgres](prom-vs-postgres/) | Should metrics live in Prometheus or PostgreSQL? | Prometheus uses **59.9x** less disk; wins 9 of 11 dimensions |

## Running a benchmark

Each one is self-contained and starts the same way:

```sh
cd prom-vs-postgres
make build
make bench
```

`make bench` runs every phase in order and writes `results/report.md`. Phases
are separately runnable (`make ingest`, `make query`) because they are
separately diagnosable.

There is also a workflow: <kbd>Actions</kbd> → *benchmark* → *Run workflow*.
`scale=smoke` finishes in about two minutes and checks that the harness still
runs. `scale=full` takes roughly forty and regenerates `results/`.

## What every benchmark here has in common

- **Equal resource envelopes.** Whatever is under test gets the same CPU and
  memory budget. Otherwise "Prometheus used less memory" can quietly mean
  "Prometheus was allowed more memory".
- **Parity before performance.** Both sides hold the same data, verified by
  querying them and comparing, before any timing runs. A fast answer to the
  wrong question is worth nothing.
- **Raw results committed.** `results/*.json` and the rendered report are in the
  repo, not just a summary in a blog post.
- **Caveats recorded, not softened.** What a run could not measure is listed
  next to what it did.
- **`:latest` pinned by digest.** `results/environment.json` records what the tag
  resolved to on the day. Without it the numbers expire silently.
- **Results land as a PR, never a push to main.** A run that changes published
  figures should be a diff somebody can read.

## Why the harness is in the repo at all

Because numbers in blog posts rot. Every claim here is a file, and every file is
regenerable. If a number in a post stops matching `results/report.md`, one of
them is wrong and you can find out which.
