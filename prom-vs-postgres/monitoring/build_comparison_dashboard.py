#!/usr/bin/env python3
"""Build the comparison dashboard.

    python3 monitoring/build_comparison_dashboard.py [--write]

Kept as a generator rather than a hand-edited JSON because the panels are
mechanical -- same shape, four metrics, two systems -- and a hand-maintained
file that must be regenerated for Grafana Cloud anyway has no advantage over
one that is produced from a description of what it should contain.

The metric names below were verified against a real run rather than assumed.
That matters more than usual here: k6 renames its metrics on the way out
(prom_requests reaches Prometheus as k6_prom_requests_total), and a panel
pointing at the wrong name is an empty panel, not an error.
"""

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
DASHBOARDS = HERE / "grafana" / "dashboards"
LOCAL = DASHBOARDS / "benchmark-comparison.json"
CLOUD = DASHBOARDS / "benchmark-comparison.cloud.json"

DS_LOCAL = {"type": "prometheus", "uid": "bench-monitoring"}
INPUT_NAME = "DS_PROMETHEUS"
INPUT_REF = "${%s}" % INPUT_NAME

TITLE = "Prometheus vs PostgreSQL — comparison"


def panel(title, description, targets, x, y, w=12, h=8, unit="short",
          stack=False, overrides=None):
    return {
        "datasource": DS_LOCAL,
        "title": title,
        "description": description,
        "type": "timeseries",
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2 if stack else 1,
                    "fillOpacity": 12 if stack else 8,
                    "stacking": {"mode": "normal" if stack else "none"},
                    "showPoints": "never",
                },
            },
            "overrides": overrides or [],
        },
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom",
                       "calcs": ["mean", "max", "lastNotNull"]},
            "tooltip": {"mode": "multi"},
        },
        "targets": [{"expr": e, "legendFormat": l, "refId": chr(65 + i)}
                    for i, (e, l) in enumerate(targets)],
    }


def row(title, y, x=0, w=24):
    return {"collapsed": False, "gridPos": {"h": 1, "w": w, "x": x, "y": y},
            "panels": [], "title": title, "type": "row"}


# The envelope, restated on the dashboard. Both systems get the same CPU and
# memory cap, which is the only reason a side-by-side comparison means
# anything, so it belongs where the curves are read rather than only in a
# README.
ENVELOPE = (
    "Both systems are capped identically (TARGET_CPUS / TARGET_MEMORY in "
    "compose), so a gap here is a difference in the storage format rather than "
    "in how much machine each one was given."
)


def build():
    y = 0
    panels = []

    panels.append(row(TITLE, y)); y += 1
    panels.append(panel(
        "Envelope in force", ENVELOPE,
        [('count(bench_cpu_pct{system="prometheus"})', "prometheus series"),
         ('count(bench_cpu_pct{system="postgres"})', "postgres series")],
        0, y, w=24, h=4))

    y += 4
    panels.append(row("Throughput — both systems under the same load", y)); y += 1
    panels.append(panel(
        "Queries per second, both systems",
        "One k6 run, two scenarios, same VU count on both arms, so the two "
        "lines are measured over the same wall clock. A TPS number without the "
        "load that produced it is meaningless; the VU panel below is the other "
        "half of that pair.",
        [('sum(rate(k6_prom_requests_total{run_id=~"$run_id"}[1m]))', "Prometheus"),
         ('sum(rate(k6_pg_requests_total{run_id=~"$run_id"}[1m]))', "PostgreSQL")],
        0, y, stack=True))
    panels.append(panel(
        "Virtual users driving the run",
        "k6 stamps run_id onto the Trend but not onto its own built-in "
        "metrics, so this panel has no run filter — filtering k6_vus by run_id "
        "matches nothing.",
        [('k6_vus', "VUs")],
        12, y))
    y += 8
    panels.append(panel(
        "Latency under that load (p99)",
        "From the same concurrent run. A flat latency line at falling "
        "throughput is saturation, not a fast system.",
        [('k6_prom_query_duration_ms_p99{run_id=~"$run_id"}', "Prometheus p99"),
         ('k6_pg_query_duration_ms_p99{run_id=~"$run_id"}', "PostgreSQL p99")],
        0, y))
    panels.append(panel(
        "Failed queries",
        "Counted rather than logged. Under concurrency a query can fail and "
        "still contribute a latency figure, so a run of errors looks exactly "
        "like a fast system on the throughput panel above.",
        [('sum(rate(k6_prom_failed_total{run_id=~"$run_id"}[1m]))',
          "Prometheus failures/s"),
         ('sum(rate(k6_pg_failed_total{run_id=~"$run_id"}[1m]))',
          "PostgreSQL failures/s")],
        12, y))
    y += 8

    panels.append(row("Resources — the cost of serving that load", y)); y += 1
    panels.append(panel(
        "CPU",
        "docker stats, sampled every 2s by the harness. Not cAdvisor: under "
        "Colima its per-container CPU is documented as untrustworthy, and this "
        "is the same source as the headline figures in results/report.md.",
        [('bench_cpu_pct{system="prometheus"}', "Prometheus"),
         ('bench_cpu_pct{system="postgres"}', "PostgreSQL")],
        0, y, unit="percent"))
    panels.append(panel(
        "Memory",
        "Working set, same source as CPU above.",
        [('bench_mem_bytes{system="prometheus"}', "Prometheus"),
         ('bench_mem_bytes{system="postgres"}', "PostgreSQL")],
        12, y, unit="bytes"))
    y += 8
    panels.append(panel(
        "On disk",
        "du of each system's own data directory, sampled through the run. This "
        "is the number the bytes-per-sample figure is computed from. Not "
        "cAdvisor's container_fs_usage_bytes: under Colima it carries no "
        "container label and reports the host filesystem for every series, so a "
        "panel over it draws one 25GB line for both systems and looks entirely "
        "plausible.",
        [('bench_disk_bytes{system="prometheus"}', "Prometheus"),
         ('bench_disk_bytes{system="postgres"}', "PostgreSQL")],
        0, y, w=24, unit="bytes"))

    return {
        "__inputs": [],
        "annotations": {"list": []},
        "editable": True,
        "title": TITLE,
        "description": "Throughput, CPU, memory and disk for the two systems, "
                       "measured while both are under load.",
        "tags": ["benchmark", "prometheus", "postgresql"],
        "timezone": "utc",
        "refresh": "10s",
        "schemaVersion": 39,
        "version": 1,
        "panels": panels,
        "time": {"from": "now-1h", "to": "now"},
        "templating": {"list": [{
            "name": "run_id", "label": "Run", "type": "query",
            "datasource": DS_LOCAL,
            "query": 'label_values(k6_prom_requests_total, run_id)',
            "includeAll": True, "multi": True, "refresh": 2, "sort": 2,
        }]},
    }


def to_cloud(dashboard):
    """Swap the local datasource uid for Grafana's import-time input."""
    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str) and node == "bench-monitoring":
            return INPUT_REF
        return node

    out = walk(dashboard)
    out["__inputs"] = [{
        "name": INPUT_NAME,
        "label": "Prometheus (Grafana Cloud)",
        "description": "The Cloud stack that receives this benchmark's metrics.",
        "type": "datasource",
        "pluginId": "prometheus",
        "pluginName": "Prometheus",
    }]
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if not args.write:
        expected_local = json.dumps(build(), indent=2) + "\n"
        if not LOCAL.exists() or LOCAL.read_text() != expected_local:
            print("%s is stale; re-run --write" % LOCAL.name)
            return 1
        print("  ok     %s is current" % LOCAL.name)
        return 0
    dashboard = build()
    LOCAL.write_text(json.dumps(dashboard, indent=2) + "\n")
    CLOUD.write_text(json.dumps(to_cloud(dashboard), indent=2) + "\n")
    print("wrote %s" % LOCAL.name)
    print("wrote %s" % CLOUD.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())