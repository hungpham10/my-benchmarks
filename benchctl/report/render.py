"""report/render.py — render results/report.md from phase outputs."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
import queries as Q  # noqa: E402  (LABELS map for display names)


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
        out.append(f"| {d['module']} | {d['samples']:,} | {fmt_bytes(d['bytes_on_disk'])} | {d['bytes_per_sample']:.2f} |")
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


def render_queries(query_files):
    """Render per-query latency table across all systems.

    Each queries-<slug>.json holds {query_name: {intent, winner_expected,
    promql, n, errors, p50_ms, p95_ms, max_ms, error}}.
    """
    out = ["## Query Latency (q1–q5, measured)"]
    if not query_files:
        out.append("No query latency data available.")
        return "\n".join(out)

    all_results = {}
    for f in sorted(query_files):
        d = json.load(open(os.path.join(RESULTS, f)))
        module = d.get("module", "unknown")
        all_results[module] = d

    if not all_results:
        out.append("No query data.")
        return "\n".join(out)

    # Collect query names from the first result (skip module/run_id keys).
    first = next(iter(all_results.values()))
    query_names = [k for k in first.keys() if k not in ("module", "run_id")]

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
        module = d.get("module", "unknown")
        count = d.get("samples", 0)
        counts[module] = count
        out.append(f"- {module}: retained {count:,} samples.")

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
                broke.append(d["module"])
        if broke:
            out.append(f"- Breaking point reached: {', '.join(broke)} (k6 exit 99).")
        else:
            out.append("- Breaking point: not reached (no k6 threshold broke).")

    out.append("- Disk bytes include WAL; Postgres figure is real COPY bytes, not a hardcoded constant.")
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

    query_files = [f for f in os.listdir(RESULTS) if f.startswith("queries-")]
    if query_files:
        lines.append(render_queries(query_files))

    stats_files = [f for f in os.listdir(RESULTS) if f.startswith("stats-")]
    if stats_files:
        lines.append(render_caveats(stats_files, disk_files))

    with open(OUT, "w") as f:
        f.write("\n".join(lines))

    print(f"[report] wrote {OUT}")

    # Also print to stdout for CI summary
    print("\n".join(lines))


if __name__ == "__main__":
    main()
