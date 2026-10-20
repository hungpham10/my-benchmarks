"""report/render.py — render results/report.md from phase outputs."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
import queries as Q  # noqa: E402  (LABELS map for display names)
from leg import leg_slug  # noqa: E402  (one slug rule, shared with the driver)


RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
OUT = os.path.join(RESULTS, "report.md")


def load(name):
    path = os.path.join(RESULTS, name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def load_all(prefix):
    """Load all JSON files matching prefix-*.json in RESULTS.

    Returns {slug: data} where slug is the middle token, e.g. summary-*.json
    for "postgres/jsonb" becomes key "postgres-jsonb".
    """
    out = {}
    if not os.path.isdir(RESULTS):
        return out
    for f in sorted(os.listdir(RESULTS)):
        if f.startswith(prefix + "-") and f.endswith(".json"):
            key = f[len(prefix) + 1:-5]  # strip "prefix-" and ".json"
            out[key] = load(f)
    return out


def fmt_bytes(value):
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def fmt_ms(value):
    return f"{value:.1f} ms"


def render_manifest(manifest):
    out = []
    out.append("## Manifest")
    out.append(f"- Scenario: {manifest.get('scenario', 'unknown')}")
    out.append(f"- Run ID: {manifest.get('run_id', 'unknown')}")
    out.append(f"- Total samples: {manifest.get('total_samples', 'N/A')}")
    out.append(f"- Interval: {manifest.get('interval_seconds', 'N/A')}s")
    out.append(f"- Window: {manifest.get('window_minutes', 'N/A')} min")
    out.append("")
    return "\n".join(out)


def render_disk(disk_files):
    out = ["## Disk Footprint"]
    out.append("| System | Samples | Bytes | B/sample |")
    out.append("|--------|---------|-------|----------|")
    for f in sorted(disk_files):
        d = json.load(open(os.path.join(RESULTS, f)))
        out.append(f"| {leg_label(d)} | {d['samples']:,} | {fmt_bytes(d['bytes_on_disk'])} | {d['bytes_per_sample']:.2f} |")
    out.append("")
    return "\n".join(out)


def render_k6(summaries):
    """Render k6 load-test table across all systems."""
    out = ["## k6 Load Test"]
    if not summaries:
        out.append("No k6 summary available.")
        return "\n".join(out)

    out.append("| System | Iterations | p50 ms | p95 ms | Dropped |")
    out.append("|--------|------------|--------|--------|---------|")
    for system in sorted(summaries):
        s = summaries[system]
        m = s.get("metrics", {})
        iters = m.get("iterations", {}).get("values", {}).get("count", 0)
        # k6 may name the duration trend iteration_duration (xk6-sql) or
        # http_req_duration (plain http). Accept either.
        dur = m.get("iteration_duration") or m.get("http_req_duration", {})
        dur_vals = dur.get("values", {})
        p50 = dur_vals.get("med", 0)
        p95 = dur_vals.get("p(95)", 0)
        dropped = m.get("dropped_iterations", {}).get("values", {}).get("count", 0)
        out.append(f"| {system} | {iters:,} | {fmt_ms(p50)} | {fmt_ms(p95)} | {dropped:,} |")
    out.append("")
    return "\n".join(out)


def render_verify(verify):
    out = ["## Verification"]
    out.append(f"- Failed checks: {verify.get('failed', '?')}")
    out.append("")
    return "\n".join(out)


def render_resource(resources):
    """Render per-system CPU/memory sampled from cAdvisor during the k6 leg."""
    out = ["## Resource Usage (during k6 leg)"]
    if not resources:
        out.append("No resource data available.")
        return "\n".join(out)

    out.append("| System | Leg (s) | CPU core avg | Mem max MB |")
    out.append("|--------|---------|--------------|------------|")
    for system in sorted(resources):
        d = resources[system]
        out.append(f"| {system} | {d.get('leg_seconds', 'n/a')} | "
                   f"{d.get('cpu_core_avg', 'n/a')} | {d.get('mem_max_mb', 'n/a')} |")
    out.append("")
    return "\n".join(out)


def leg_label(d):
    """'postgres/jsonb' + 'xfs' -> 'postgres/jsonb on xfs'."""
    fs = d.get("fs")
    return f"{d.get('module', 'unknown')} on {fs}" if fs else d.get("module", "unknown")


def render_queries(query_files):
    """Render per-query latency table across all systems.

    Each queries-<slug>.json holds {query_name: {intent, winner_expected,
    promql, n, errors, p50_ms, p95_ms, max_ms, error}}, plus module/fs/run_id.
    """
    out = ["## Query Latency (q1–q5, measured)"]
    if not query_files:
        out.append("No query latency data available.")
        return "\n".join(out)

    all_results = {}
    for f in sorted(query_files):
        d = json.load(open(os.path.join(RESULTS, f)))
        # Keyed by leg, not by module: the filesystem matrix runs the same
        # module on several filesystems, and keying by module would let each
        # leg overwrite the previous one.
        all_results[leg_label(d)] = d

    if not all_results:
        out.append("No query data.")
        return "\n".join(out)

    # Collect query names from the first result (skip the metadata keys).
    first = next(iter(all_results.values()))
    meta_keys = ("module", "fs", "run_id")
    query_names = [k for k in first.keys() if k not in meta_keys]

    # Build module list from actual result keys (not hardcoded wrong keys),
    # keyed to Q.LABELS display names. Unknown modules fall back to raw key.
    mods = sorted(all_results.keys())
    def mod_label(m):
        return Q.LABELS.get(m, m)

    def cell(d, qname, field):
        v = d.get(qname, {}).get(field)
        if v is None:
            return "—"
        if field in ("p50_ms", "p95_ms", "max_ms"):
            return f"{v:.1f}"
        return str(v)

    def q_empty(qname, mod):
        return all_results.get(mod, {}).get(qname, {}).get("empty", False)

    def q_p50(qname, mod):
        return all_results.get(mod, {}).get(qname, {}).get("p50_ms")

    # p50 table
    out.append("| Query | " + " | ".join(f"{mod_label(m)} p50 ms" for m in mods) + " |")
    out.append("|-------|" + "|".join("----" for _ in mods) + "|")
    for qname in query_names:
        row = [qname]
        for mod in mods:
            row.append(cell(all_results.get(mod, {}), qname, "p50_ms"))
        out.append("| " + " | ".join(row) + " |")
    out.append("")

    # p95 table
    out.append("| Query | " + " | ".join(f"{mod_label(m)} p95 ms" for m in mods) + " |")
    out.append("|-------|" + "|".join("----" for _ in mods) + "|")
    for qname in query_names:
        row = [qname]
        for mod in mods:
            row.append(cell(all_results.get(mod, {}), qname, "p95_ms"))
        out.append("| " + " | ".join(row) + " |")
    out.append("")

    # Expected winner vs measured fastest (skip empty queries — latency on an
    # empty result set is meaningless overhead, not a real measurement).
    out.append("| Query | Expected winner | Measured fastest | Notes |")
    out.append("|-------|-----------------|------------------|-------|")
    for qname in query_names:
        expected = first[qname].get("winner_expected", "—")
        best = None
        best_ms = float("inf")
        for mod in mods:
            d = all_results.get(mod, {})
            if d.get(qname, {}).get("empty"):
                continue
            ms = d.get(qname, {}).get("p50_ms")
            if ms is not None and ms < best_ms:
                best_ms = ms
                best = mod
        best_label = mod_label(best) if best else "— (all empty)"
        row = [qname, str(expected), best_label]
        notes = []
        for mod in mods:
            d = all_results.get(mod, {}).get(qname, {})
            if d.get("empty"):
                notes.append(f"{mod_label(mod)}: empty result (0 rows)")
            elif d.get("error"):
                notes.append(f"{mod_label(mod)}: {d['error']}")
            elif d.get("errors", 0) > 0:
                notes.append(f"{mod_label(mod)}: {d['errors']} failed iters")
        out.append("| " + " | ".join(row + ["; ".join(notes) if notes else "ok"]) + " |")
    out.append("")
    return "\n".join(out)


def render_caveats(stats_files, disk_files=None):
    """Data-driven caveats: what was actually measured, not boilerplate."""
    out = ["## Caveats"]
    if not stats_files:
        out.append("- No sample stats available.")
        out.append("")
        return "\n".join(out)

    counts = {}
    for f in sorted(stats_files):
        d = json.load(open(os.path.join(RESULTS, f)))
        label = leg_label(d)
        count = d.get("samples", 0)
        counts[label] = count
        out.append(f"- {label}: retained {count:,} samples.")

    # Parity note: compare counts only; sum is diagnostic.
    prom_count = counts.get("prometheus")
    pg_counts = {m: c for m, c in counts.items() if m.startswith("postgres")}
    if prom_count is not None and pg_counts:
        pg_total = sum(pg_counts.values())
        n_pg = len(pg_counts)
        avg_pg = pg_total / n_pg if n_pg else 0
        delta = prom_count - avg_pg
        pct = (delta / prom_count * 100) if prom_count else 0
        out.append(f"- Parity: Prometheus {prom_count:,} vs Postgres avg {avg_pg:,.0f} "
                   f"(delta {delta:+,}, {pct:+.1f}%). Count tolerance is 2%; sum is "
                   "recorded as diagnostic only, not verified.")

    out.append("- k6 thresholds: exit 99 means threshold broke (""breaking point""), not an error.")
    if disk_files:
        broke = []
        for f in sorted(disk_files):
            d = json.load(open(os.path.join(RESULTS, f)))
            code = d.get("k6_exit_code")
            if code == 99:
                broke.append(leg_label(d))
        if broke:
            out.append(f"- Breaking point reached: {', '.join(broke)} (k6 exit 99).")
        else:
            out.append("- Breaking point: not reached (no k6 threshold broke).")

    out.append("- Disk bytes include WAL; Postgres figure is real COPY bytes, not a hardcoded constant.")
    out.append("")
    return "\n".join(out)


def _fs_legs(disk_files, prefix):
    """{fs_type: data} for one kind of per-leg result file.

    Keyed by filesystem rather than by slug, because the filesystem is the
    axis the reader is comparing along.
    """
    out = {}
    for f in sorted(disk_files):
        d = json.load(open(os.path.join(RESULTS, f)))
        fs = d.get("fs")
        if not fs:
            continue
        slug = leg_slug(d["module"], fs)
        path = os.path.join(RESULTS, f"{prefix}-{slug}.json")
        if os.path.exists(path):
            out[fs] = json.load(open(path))
    return out


def render_fs_environment(fs_probes):
    """What each filesystem was, as it described itself."""
    out = ["## Filesystem Environment"]
    out.append("| Filesystem | Device | Mount options | Block size | Notes |")
    out.append("|------------|--------|---------------|------------|-------|")
    for fs in sorted(fs_probes):
        p = fs_probes[fs]
        notes = []
        if p.get("zfs_properties"):
            props = p["zfs_properties"]
            notes.append(f"recordsize {props.get('recordsize', '?')}, "
                         f"compression {props.get('compression', '?')}")
        if p.get("backend"):
            notes.append(f"host backend: {p['backend']}")
        block = p.get("block_size")
        out.append(f"| {fs} | `{p.get('device', '?')}` | "
                   f"`{p.get('mount_options', '?')}` | "
                   f"{block if block else '?'} | {', '.join(notes)} |")
    out.append("")
    return "\n".join(out)


def render_fs_write(disk_files, fsync_files, io_files):
    """The write path: where the filesystem actually shows up."""
    fsyncs = _fs_legs(disk_files, "fsync")
    ios = _fs_legs(disk_files, "io")
    out = ["## Filesystem Matrix — Write Path"]
    out.append("| Filesystem | fdatasync µs | fsync µs | fio p50 µs | fio p99 µs | COPY s | samples/s | on disk |")
    out.append("|------------|---------------|----------|-----------|-----------|--------|-----------|---------|")
    for f in sorted(disk_files):
        d = json.load(open(os.path.join(RESULTS, f)))
        fs = d.get("fs")
        if not fs:
            continue
        fsb = fsyncs.get(fs, {})
        fio = fsb.get("fio", {})
        copy_s = d.get("seconds")
        sps = d.get("samples_per_second")
        out.append(
            f"| {fs} | {fsb.get('fdatasync_us', 'n/a')} | "
            f"{fsb.get('fsync_us', 'n/a')} | "
            f"{fio.get('fdatasync_us_p50', 'n/a')} | "
            f"{fio.get('fdatasync_us_p99', 'n/a')} | "
            f"{copy_s if copy_s is not None else 'n/a'} | "
            f"{f'{sps:,.0f}' if sps else 'n/a'} | "
            f"{fmt_bytes(d.get('bytes_on_disk', 0))} |")
    out.append("")
    out.append("fdatasync µs / fsync µs: pg_test_fsync on the data directory "
               "itself; fdatasync is what PostgreSQL's WAL pays on Linux. "
               "fio p50/p99 µs: the same filesystem measured with fio — 4 KiB "
               "buffered writes, one fdatasync each, which is PostgreSQL's "
               "default WAL path. pg_test_fsync uses O_DIRECT and cannot run "
               "on ZFS, which refuses direct writes smaller than its 128K "
               "record. COPY s: the bulk load, one transaction.")
    out.append("")
    return "\n".join(out)


def render_fs_syscalls(disk_files):
    """What PostgreSQL itself measured at the syscall boundary."""
    ios = _fs_legs(disk_files, "io")
    out = ["## Filesystem Matrix — PostgreSQL's Own I/O Accounting (pg_stat_io)"]
    out.append("| Filesystem | write µs/op | writeback µs/op | fsync µs/op | fsyncs | read µs/op | writeback |")
    out.append("|------------|-------------|-----------------|-------------|--------|-------------|-----------|")
    for f in sorted(disk_files):
        d = json.load(open(os.path.join(RESULTS, f)))
        fs = d.get("fs")
        if not fs:
            continue
        io = ios.get(fs, {})
        w = io.get("after_write", {})
        r = io.get("after_read", {})
        out.append(
            f"| {fs} | {w.get('write_us_avg', 'n/a')} | "
            f"{w.get('writeback_us_avg', 'n/a')} | "
            f"{w.get('fsync_us_avg', 'n/a')} | "
            f"{w.get('fsyncs', 'n/a')} | "
            f"{r.get('read_us_avg', 'n/a')} | "
            f"{w.get('writebacks', 'n/a')} |")
    out.append("")
    out.append("Average latency per syscall, as PostgreSQL measured it. "
               "writeback is the checkpointer flushing dirty pages; fsync is "
               "what a commit costs.")
    out.append("")
    return "\n".join(out)


def render_fs_read(disk_files, io_files, query_files, summaries):
    """The read path: warm (cache) and cold (disk). The difference is the point."""
    ios = _fs_legs(disk_files, "io")
    queries = _fs_legs(disk_files, "queries")
    colds = _fs_legs(disk_files, "queries-cold")
    out = ["## Filesystem Matrix — Read Path"]
    out.append("| Filesystem | q1 warm p50 ms | q1 cold ms | q5 warm p50 ms | "
               "k6 p95 ms | PG read µs/op |")
    out.append("|------------|-----------------|------------|----------------|"
               "------------|----------------|")
    for f in sorted(disk_files):
        d = json.load(open(os.path.join(RESULTS, f)))
        fs = d.get("fs")
        if not fs:
            continue
        q = queries.get(fs, {})
        q1 = q.get("q1_counter_rate", {})
        q5 = q.get("q5_short_range", {})
        cq = colds.get(fs, {}).get("q1_counter_rate", {})
        io = ios.get(fs, {}).get("after_read", {})
        slug = leg_slug(d["module"], fs)
        k6 = summaries.get(slug, {})
        m = k6.get("metrics", {})
        dur = m.get("iteration_duration") or m.get("http_req_duration", {})
        vals = dur.get("values", {})
        out.append(
            f"| {fs} | {q1.get('p50_ms', 'n/a')} | {cq.get('p50_ms', 'n/a')} | "
            f"{q5.get('p50_ms', 'n/a')} | "
            f"{fmt_ms(vals.get('p(95)', 0)) if vals else 'n/a'} | "
            f"{io.get('read_us_avg', 'n/a')} |")
    out.append("")
    out.append("warm p50: the median of the measured iterations with the "
               "cache warm. cold: one execution right after the container "
               "restarted and the host page cache was dropped, so the "
               "read had to reach the filesystem.")
    out.append("")
    return "\n".join(out)


def render_fs_readshapes(disk_files):
    """The read shapes: what the filesystem does when the query is allowed
    to touch the disk."""
    warm = _fs_legs(disk_files, "readshapes")
    cold = _fs_legs(disk_files, "readshapes-cold")
    if not warm:
        return ""
    shapes = list(next(iter(warm.values())).keys())
    out = ["## Filesystem Matrix — Read Shapes"]
    out.append("| Shape | " + " | ".join(
        f"{fs} warm / cold ms" for fs in sorted(warm)) + " |")
    out.append("|-------|" + "|".join(["---------"] * len(warm)) + "|")
    for shape in shapes:
        cells = []
        for fs in sorted(warm):
            w = warm.get(fs, {}).get(shape, {})
            c = cold.get(fs, {}).get(shape, {})
            cells.append(f"{w.get('p50_ms', 'n/a')} / {c.get('p50_ms', 'n/a')}")
        out.append(f"| {shape} | " + " | ".join(cells) + " |")
    out.append("")
    out.append("seq_scan reads the whole table; one_metric goes through the "
               "label index for a single metric; one_series is one series, "
               "one index lookup plus a heap fetch. warm / cold is the "
               "median with the cache warm / the first execution after the "
               "cache was dropped.")
    out.append("")
    return "\n".join(out)


def render_fs_caveats(fs_probes, availability=None):
    """The things a reader must know before quoting these numbers."""
    out = ["## Filesystem Caveats"]
    for fs in sorted(fs_probes):
        out.append(f"- **{fs}**: block size "
                   f"{fs_probes[fs].get('block_size', '?')} B, mounted "
                   f"`{fs_probes[fs].get('mount_options', '?')}`.")
    out.append("- Every filesystem sits on a loop device backed by a file on "
               "the runner's own disk. The comparison between filesystems is "
               "meaningful; the absolute numbers describe that disk, not a "
               "bare device.")
    out.append("- The loop device runs with --direct-io=on, so the host page "
               "cache cannot absorb writes before they reach the disk. "
               "Without it, this would measure the host's cache.")
    out.append("- The dataset is smaller than RAM. With a warm cache the read "
               "path barely moves between filesystems — that is a result, "
               "not a missing measurement. The cold column is the same "
               "query after the container restarted and the host page cache "
               "was dropped, which is where the filesystem is actually on "
               "the critical path.")
    if "zfs" in fs_probes:
        out.append("- ZFS keeps its own cache (ARC) in host memory, capped at "
                   "the envelope size. It also writes in 128K records rather "
                   "than 4K blocks, which is a real difference and not a "
                   "tuning choice.")
    out.append("- pg_test_fsync uses O_DIRECT, and ZFS refuses direct writes "
               "smaller than its 128K record size, so it cannot run there. "
               "The fio column covers ZFS, and PostgreSQL's own fsync "
               "accounting covers all four.")
    if availability:
        for fs, info in sorted(availability.items()):
            if not info.get("available"):
                out.append(f"- **{fs} was not measured**: {info.get('reason')}. "
                           "It is a gap in the matrix, not a zero.")
    out.append("")
    return "\n".join(out)


def main():
    lines = ["# Benchctl Benchmark Report", ""]

    manifest = load("manifest.json")
    if manifest:
        lines.append(render_manifest(manifest))

    disk_files = [f for f in os.listdir(RESULTS) if f.startswith("disk-")]
    if disk_files:
        lines.append(render_disk(disk_files))

    verify = load("verify.json")
    if verify:
        lines.append(render_verify(verify))

    summaries = load_all("summary")
    if summaries:
        lines.append(render_k6(summaries))

    resources = load_all("resource")
    if resources:
        lines.append(render_resource(resources))

    # queries-cold-<slug>.json is the same suite after the cache was
    # dropped; it has its own table in the filesystem section and must not
    # be mixed into the warm one.
    query_files = [f for f in os.listdir(RESULTS)
                   if f.startswith("queries-") and not f.startswith("queries-cold-")]
    if query_files:
        lines.append(render_queries(query_files))

    stats_files = [f for f in os.listdir(RESULTS) if f.startswith("stats-")]
    if stats_files:
        lines.append(render_caveats(stats_files, disk_files))

    # Filesystem matrix, when the scenario has one.
    fs_probes = {}
    for f in os.listdir(RESULTS):
        if f.startswith("fs-") and f.endswith(".json") and f != "fs-availability.json":
            fs_probes[f[3:-5]] = load(f)
    if fs_probes:
        lines.append(render_fs_environment(fs_probes))
        lines.append(render_fs_write(disk_files, None, None))
        lines.append(render_fs_syscalls(disk_files))
        lines.append(render_fs_read(disk_files, None, None, summaries))
        lines.append(render_fs_readshapes(disk_files))
        lines.append(render_fs_caveats(fs_probes, load("fs-availability.json") or None))

    with open(OUT, "w") as f:
        f.write("\n".join(lines))

    print(f"[report] wrote {OUT}")

    # Also print to stdout for CI summary
    print("\n".join(lines))


if __name__ == "__main__":
    main()
