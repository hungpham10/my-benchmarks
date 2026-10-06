#!/usr/bin/env python3
"""Verify the monitoring stack is actually collecting what the dashboard needs.

Three things get checked, cheapest first:

  1. config.cloud.alloy still matches config.alloy (drift check)
  2. every scrape target is delivering series
  3. the Prometheus under test has not been contaminated by monitoring

(3) is the one that matters most. A mistake in the remote_write wiring puts
operational metrics into the TSDB being measured, and every footprint number in
results/ becomes wrong without anything failing.
"""
import json, pathlib, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from sync_cloud_config import BASE, CLOUD  # noqa: E402

QUERIES = [
    ("cAdvisor container CPU",  "count(container_cpu_usage_seconds_total)"),
    ("cAdvisor container MEM",  "count(container_memory_working_set_bytes)"),
    ("host CPU",                "count(node_cpu_seconds_total)"),
    ("host memory",             "count(node_memory_MemAvailable_bytes)"),
    ("host filesystem",         "count(node_filesystem_avail_bytes)"),
    ("Prometheus under test",   "count(prometheus_tsdb_head_series)"),
    ("Postgres up",             "count(pg_up)"),
    ("Postgres database size",  "count(pg_database_size_bytes)"),
    ("Postgres statements",     "count(pg_stat_statements_calls_total)"),
    ("Postgres backends",       "count(pg_stat_database_numbackends)"),
]


def query(q):
    out = subprocess.run(
        ["docker", "exec", "bench-monitoring-prometheus",
         "wget", "-qO-", "http://localhost:9090/api/v1/query", "--post-data=" + f"query={q}"],
        capture_output=True, text=True, check=True).stdout
    result = json.loads(out)["data"]["result"]
    return int(result[0]["value"][1]) if result else 0


def config_drift() -> list[str]:
    """config.cloud.alloy is generated; if it drifted it measures something else."""
    import sync_cloud_config
    expected = sync_cloud_config.render()
    if not CLOUD.exists():
        return ["config.cloud.alloy missing (run sync_cloud_config.py --write)"]
    if CLOUD.read_text() != expected:
        return ["config.cloud.alloy is stale (run sync_cloud_config.py --write)"]
    return []


def under_test_is_clean() -> bool:
    """The measured TSDB must hold benchmark data only, never monitoring data."""
    probe = subprocess.run(
        ["curl", "-s", "http://localhost:9090/api/v1/query",
         "--data-urlencode", "query=count(container_cpu_usage_seconds_total)"],
        capture_output=True, text=True)
    try:
        return json.loads(probe.stdout)["data"]["result"] == []
    except Exception:                                      # noqa: BLE001
        return False


def main():
    failed = []

    drift = config_drift()
    for problem in drift:
        print(f"  DRIFT  {problem}")
        failed.append(problem)
    if not drift:
        print(f"  ok     {BASE.name} and {CLOUD.name} agree")

    if under_test_is_clean():
        print("  ok     Prometheus under test holds no monitoring series")
    else:
        print("  LEAK   monitoring series found in the Prometheus under test")
        failed.append("monitoring series in the measured TSDB")

    print(f"{'signal':<26} {'series':>8}")
    print("-" * 36)
    for label, q in QUERIES:
        try:
            n = query(q)
        except Exception as exc:                       # noqa: BLE001
            print(f"{label:<26} {'ERR':>8}  {exc}")
            failed.append(label)
            continue
        print(f"{label:<26} {n:>8}")
        if n == 0:
            failed.append(label)
    print()
    if failed:
        print("EMPTY: " + ", ".join(failed))
        return 1
    print("all targets delivering")
    return 0


if __name__ == "__main__":
    sys.exit(main())
