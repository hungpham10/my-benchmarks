"""Render results/report.md from the phase outputs.

The report is organised by the three claims being tested, in the order a
reader needs them: are the two datasets the same, how big is each one, how
fast is each query, and what did that cost in CPU and memory.

Anything a phase did not produce is printed as a gap rather than silently
omitted. A benchmark that quietly drops the run it could not complete is
indistinguishable from one that passed.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import queries as Q  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")
OUT = os.path.join(RESULTS, "report.md")


def load(name):
    path = os.path.join(RESULTS, name)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def fmt_bytes(value):
    if value is None:
        return "n/a"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    number = float(value)
    for unit in units:
        if abs(number) < 1024 or unit == units[-1]:
            return "%.1f %s" % (number, unit) if unit != "B" else "%d B" % number
        number /= 1024


def fmt_ms(value):
    if value is None:
        return "n/a"
    if value >= 1000:
        return "%.2f s" % (value / 1000.0)
    return "%.1f ms" % value


def add(line=""):
    lines.append(line)


def render_manifest(manifest, environment):
    add("## Run parameters")
    add()
    if environment:
        envelope = environment.get("envelope") or {}
        add("**Resource envelope, identical for both systems: %s CPU, %s RAM "
            "each.**" % (envelope.get("cpus_per_target", "?"),
                         envelope.get("memory_per_target", "?")))
        add()
        images = environment.get("images") or {}
        rows = [(name, info.get("id") or info.get("tag"))
                for name, info in images.items() if info]
        if rows:
            add("Images under test (`:latest`, so the digest is what makes this "
                "run reproducible):")
            add()
            add("| System | Image ID (pinned by this run) |")
            add("| --- | --- |")
            for name, digest in rows:
                add("| %s | `%s` |" % (name, digest))
            add()
    add("| Parameter | Value |")
    add("| --- | --- |")
    if manifest:
        add("| Series | %s |" % "{:,}".format(manifest.get("total_series", 0)))
        add("| Samples per series | %s |" % "{:,}".format(manifest.get("samples", 0)))
        add("| Total samples | %s |" % "{:,}".format(manifest.get("total_samples", 0)))
        add("| Window (ms) | %s .. %s |" % (manifest.get("t_start_ms"),
                                            manifest.get("t_end_ms")))
    else:
        add("| _dataset.json missing_ | run `make ingest` |")
    add()


def render_parity(parity):
    add("## 0. Parity")
    add()
    if not parity:
        add("_Not run._ No latency or footprint number below is meaningful "
            "until this passes.")
        add()
        return
    verdict = "**PASS**" if parity["passed"] else "**FAIL**"
    add("%s on `%s`." % (verdict, parity["metric"]))
    add()
    add("| Target | Samples | Sum | Min | Max |")
    add("| --- | --- | --- | --- | --- |")
    add("| Prometheus | %s | %.4f | %.4f | %.4f |"
        % ("{:,}".format(int(parity["prometheus"]["count"])),
           parity["prometheus"]["sum"], parity["prometheus"]["min"],
           parity["prometheus"]["max"]))
    for name, got in parity["postgres"].items():
        add("| %s | %s | %.4f | %.4f | %.4f |"
            % (Q.LABELS.get(name, name), "{:,}".format(got["count"]),
               got["sum"], got["min"], got["max"]))
    if parity["failures"]:
        add()
        for failure in parity["failures"]:
            add("- MISMATCH: %s" % failure)
    add()


def render_footprint(footprint):
    add("## 1. Disk footprint")
    add()
    if not footprint:
        add("_Not run._")
        add()
        return
    prom = footprint["prometheus"]
    pg = footprint["postgres"]
    samples = footprint["total_samples"]

    add("WAL excluded. The comparable quantity is **bytes per sample**: raw "
        "byte totals only mean something next to the sample count.")
    add()
    blocks = footprint.get("prometheus_blocks")
    if blocks is not None:
        if blocks == 0:
            state = ("Prometheus currently holds **0 compacted blocks**. The "
                     "window never reached the 3h `compactable()` threshold, so "
                     "everything is still head chunks and the figure below is "
                     "the head figure, not a settled long-run one.")
        else:
            state = ("Prometheus currently holds **%d compacted block(s)**; the "
                     "remainder is head chunks." % blocks)
        add(state)
        add()

    add("| Target | On disk | Per sample |")
    add("| --- | --- | --- |")
    add("| Prometheus | %s | **%.2f B** |"
        % (fmt_bytes(prom.get("bytes")), prom.get("bytes_per_sample", 0)))
    add("| PostgreSQL (both schemas) | %s | **%.2f B** |"
        % (fmt_bytes(pg.get("bytes")), pg.get("bytes_per_sample", 0)))
    add()
    ratio = footprint.get("ratio_prometheus_smaller")
    if ratio:
        add("PostgreSQL occupies **%.1fx** more disk for the same %s samples."
            % (ratio, "{:,}".format(samples)))
    add()

    tables = footprint.get("tables") or {}
    if tables:
        add("PostgreSQL, per relation (heap excludes indexes):")
        add()
        add("| Relation | Heap | Indexes | Index/heap |")
        add("| --- | --- | --- | --- |")
        total_heap = total_index = 0
        for name, sizes in tables.items():
            heap, index = sizes["heap_bytes"], sizes["index_bytes"]
            total_heap += heap
            total_index += index
            add("| `%s` | %s | %s | %.2f |"
                % (name, fmt_bytes(heap), fmt_bytes(index),
                   index / heap if heap else 0))
        if total_heap:
            add("| **total** | **%s** | **%s** | **%.2f** |"
                % (fmt_bytes(total_heap), fmt_bytes(total_index),
                   total_index / total_heap))
        add()
    add("WAL, excluded above because it is transient rather than a property of "
        "the stored data: Prometheus %s, PostgreSQL %s."
        % (fmt_bytes(prom.get("wal_bytes")), fmt_bytes(pg.get("wal_bytes"))))
    add()


def render_query(query):
    add("## 2. Query latency")
    add()
    if not query or not query.get("results"):
        add("_Not run._")
        add()
        return
    add("Warm, serial, single connection. %d iterations per cell after %d warmup "
        "executions, Prometheus stepped at %ds so both sides return comparable "
        "row counts." % (query["iterations"], query["warmup"], query["step_seconds"]))
    add()
    for shape in Q.QUERIES:
        rows = [r for r in query["results"] if r["query"] == shape["name"]]
        if not rows:
            continue
        by_backend = {r["backend"]: r for r in rows}
        add("### %s" % shape["name"])
        add()
        add("_%s_ — expected: **%s**." % (shape["intent"], shape["winner"]))
        add()
        add("| Target | p50 | p75 | p95 | p99 | max | rows |")
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for backend in Q.BACKENDS:
            got = by_backend.get(backend)
            if not got:
                continue
            add("| %s | %s | %s | %s | %s | %s | %s |"
                % (Q.LABELS[backend], fmt_ms(got["p50_ms"]), fmt_ms(got["p75_ms"]),
                   fmt_ms(got["p95_ms"]), fmt_ms(got["p99_ms"]),
                   fmt_ms(got["max_ms"]), "{:,}".format(got["rows"])))
        winner = min(rows, key=lambda r: r["p95_ms"])
        add()
        add("Fastest p95: **%s** at %s."
            % (Q.LABELS.get(winner["backend"], winner["backend"]),
               fmt_ms(winner["p95_ms"])))
        add()


def render_resource(ingest, query_res):
    add("## 3. Resource use")
    add()
    if not ingest and not query_res:
        add("_Not run._")
        add()
        return
    if ingest:
        add("**During ingest** — targets loaded sequentially, so these are not "
            "contending for the same cores:")
        add()
        add("| Target | Wall time | CPU (avg / peak) | Peak memory |")
        add("| --- | --- | --- | --- |")
        for target, got in ingest.items():
            # The window is named for the target being loaded, but it holds a
            # sample of *both* systems. Report the one under test; the other
            # was idle and its number would be misleading here.
            own = got.get(target) or {}
            add("| %s | %.1f s | %s%% / %s%% | %s |"
                % (Q.LABELS.get(target, target), got.get("seconds", 0),
                   own.get("cpu_pct_avg", "n/a"), own.get("cpu_pct_peak", "n/a"),
                   fmt_bytes(own.get("mem_bytes_peak"))))
        add()
    if query_res:
        suite = load("query.json") or {}
        add("**During the query phase** — the full %d-iteration suite, "
            "%.1f s wall:" % (suite.get("iterations", 0), query_res.get("seconds", 0)))
        add()
        peak = (query_res.get("peak") or {})
        if peak:
            add("| Target | CPU (avg / peak) | Peak memory |")
            add("| --- | --- | --- |")
            for backend, got in peak.items():
                add("| %s | %s%% / %s%% | %s |"
                    % (Q.LABELS.get(backend, backend), got.get("cpu_pct_avg", "n/a"),
                       got.get("cpu_pct_peak", "n/a"),
                       fmt_bytes(got.get("mem_bytes_peak"))))
        add()


def render_caveats():
    add("## Caveats to carry into the write-up")
    add()
    add("- **Scale.** This run is at a deliberately small scale so it completes "
        "in minutes on a 4-core VM. At that size PostgreSQL's fixed costs "
        "(catalog, WAL, autovacuum) are a larger share of its total than they "
        "would be in production, which flatters it on absolute footprint. "
        "State the measured scale next to every number, and label any "
        "extrapolation as computed rather than measured.")
    add("- **Single connection, single node.** No parallelism on either side. "
        "PostgreSQL parallelises a scan across workers; this does not test that.")
    add("- **Both systems run inside the same CPU and memory envelope.** A "
        "query that was OOM-killed or CPU-starved did not finish slowly, it "
        "did not finish at all. Report those as failures, not as slow numbers.")
    add("- **Images are `:latest`.** The figures describe whatever that tag "
        "resolved to on the day. Keep the digest printed above; without it the "
        "numbers cannot be reproduced and silently expire.")
    add("- **Percentiles are warm.** Cold-cache behaviour is not measured.")
    add("- **`docker stats` CPU is utilisation, not cumulative time.** Pair it "
        "with the wall time above to reason about CPU-seconds.")
    add("- **The dataset is future-dated** because Prometheus will not accept "
        "samples older than `MaxTime - chunkRange/2`. This is why every query "
        "passes an explicit range and why the retention window is short.")
    add()


def main():
    global lines
    lines = []
    manifest = load("dataset.json")
    environment = load("environment.json")
    parity = load("parity.json")
    footprint = load("footprint.json")
    query = load("query.json")
    ingest_res = load("resource_ingest.json")
    query_res = load("resource_query.json")

    add("# Prometheus vs PostgreSQL for metrics")
    add()
    add("Generated by `bench/report.py`. Do not edit by hand; rerun the phases.")
    add()
    render_manifest(manifest, environment)
    render_parity(parity)
    render_footprint(footprint)
    render_query(query)
    render_resource(ingest_res, query_res)
    render_caveats()

    os.makedirs(RESULTS, exist_ok=True)
    with open(OUT, "w") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    print("[report] wrote %s" % os.path.relpath(OUT, ROOT))
    missing = [n for n, v in (("dataset", manifest), ("parity", parity),
                              ("footprint", footprint), ("query", query))
               if v is None]
    if missing:
        print("[report] missing phases: %s" % ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
