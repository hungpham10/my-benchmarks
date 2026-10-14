"""report/render.py — render results/report.md from phase outputs."""

import json
import os
import sys

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
OUT = os.path.join(RESULTS, "report.md")


def load(name):
    path = os.path.join(RESULTS, name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def fmt_bytes(value):
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def fmt_ms(value):
    return f"{value:.1f} ms"


def add(line=""):
    return line


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


def render_verify(verify):
    out = ["## Verification"]
    out.append(f"- Failed checks: {verify.get('failed', '?')}")
    out.append("")
    return "\n".join(out)


def render_k6(summary):
    out = ["## k6 Load Test"]
    if not summary:
        out.append("No k6 summary available.")
        return "\n".join(out)

    m = summary.get("metrics", {})
    out.append("| Metric | Value |")
    out.append("|--------|-------|")

    if "http_reqs" in m:
        out.append(f"| Total requests | {m['http_reqs']['values']['count']:,} |")
    if "http_req_duration" in m:
        v = m["http_req_duration"]["values"]
        out.append(f"| HTTP p50 | {fmt_ms(v.get('med', 0))} |")
        out.append(f"| HTTP p95 | {fmt_ms(v.get('p(95)', 0))} |")
    if "iterations" in m:
        out.append(f"| Iterations | {m['iterations']['values']['count']:,} |")
    if "dropped_iterations" in m:
        out.append(f"| Dropped iterations | {m['dropped_iterations']['values']['count']:,} |")

    out.append("")
    return "\n".join(out)


def render_caveats():
    out = ["## Caveats"]
    out.append("- Prometheus and PostgreSQL data may differ due to:")
    out.append("  - PostgreSQL partitioned tables may have slight timestamp precision differences")
    out.append("  - Prometheus remote-write may buffer samples")
    out.append("- k6 thresholds may cause early exit (exit 99 = threshold broke)")
    out.append("- Alloy scrape interval (10s) may cause server_requests gap")
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

    summary = load("summary.json")
    if summary:
        lines.append(render_k6(summary))

    lines.append(render_caveats())

    with open(OUT, "w") as f:
        f.write("\n".join(lines))

    print(f"[report] wrote {OUT}")

    # Also print to stdout for CI summary
    print("\n".join(lines))


if __name__ == "__main__":
    main()