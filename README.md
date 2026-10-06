# my-benchmarks

Measurement harnesses. Each subdirectory is one benchmark: a question, a
reproducible way to answer it, and the raw numbers from a real run.

These back blog posts. The rule is that every figure published has to be
traceable to a file in this repo, and every harness has to be runnable by a
reader who distrusts the number.

## Benchmarks

| Benchmark | Question | Write-up |
| --- | --- | --- |
| [prom-vs-postgres](prom-vs-postgres/) | Should metrics live in Prometheus or PostgreSQL? | [post](https://hungpham10.wordpress.com) |

`prom-vs-postgres` also ships an optional monitoring stack: cAdvisor, a host
exporter and both databases' own metrics feed a Grafana dashboard, so a run can
be judged on what it cost the machine as well as how fast it was. See
[Watching a run](prom-vs-postgres/README.md#watching-a-run).

## What every benchmark here has in common

- **Equal resource envelopes.** Whatever is under test gets the same CPU and
  memory budget. Otherwise "Prometheus used less memory" can mean "Prometheus
  was allowed more memory".
- **Parity before performance.** Both sides hold the same data, verified by
  querying them and comparing, before any timing runs.
- **Raw results committed.** `results/*.json` and the rendered report are in the
  repo, not just a summary in a blog post.
- **Caveats recorded, not softened.** What the run could not measure is listed
  next to what it did.
- **`:latest` is recorded by digest.** `results/environment.json` pins what the
  tag resolved to on the day, or the numbers are not reproducible later.
