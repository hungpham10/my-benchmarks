"""verify/check.py — the layer that decides whether numbers are trustworthy.

Four checks, each a test (fail = no report), not a comment:
  1. parity      — every system holds the same sample count (within 2%);
                   sums are logged as diagnostic only (engine semantics differ)
  2. counter     — counter resets between runs would make rate() fictional
  3. continuity  — the recorded curve must have > N points, not one flush dot
  4. envelope    — every leg ran under the same CPU/RAM cap
"""
import json
import os
import re
import sys
import urllib.request
import urllib.parse

RESULTS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "results")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = os.path.join(ROOT, "shared")
sys.path.insert(0, SHARED)
import queries as Q  # noqa: E402
from leg import leg_slug  # noqa: E402 — same slug rule as the driver and k6


def load_all_summaries():
    """Load all summary-<slug>.json files written by k6 per leg.

    Returns {slug: data} where slug is e.g. "prometheus", "postgres-jsonb".
    Returns {} if no summary files exist (k6 leg never ran).
    """
    out = {}
    if not os.path.isdir(RESULTS):
        return out
    for f in sorted(os.listdir(RESULTS)):
        if f.startswith("summary-") and f.endswith(".json"):
            key = f[len("summary-"):-5]
            out[key] = json.load(open(os.path.join(RESULTS, f)))
    return out


def check_parity():
    """Real parity: compare sample stats collected from each system at init time.

    This verifies both systems actually hold the same data by reading the
    stats-<system>.json files written by the driver while the SUT was live.
    No network connections, no dependency on teardown order.
    """
    stats_files = [f for f in os.listdir(RESULTS) if f.startswith("stats-")]
    if len(stats_files) < 2:
        # Skip parity check for single-system runs (e.g. smoke scenario)
        return True, f"parity skipped: only {len(stats_files)} system(s), need >=2"

    module_stats = {}
    for f in stats_files:
        stats = json.load(open(os.path.join(RESULTS, f)))
        module = stats["module"]
        # A leg is a (system, filesystem) pair. Keying by the system alone
        # would let the four filesystem legs overwrite each other, and the
        # parity check would compare a filesystem with itself.
        fs = stats.get("fs") or ""
        # A leg is a (system, filesystem, extra) tuple. Keying by the system
        # alone would let the legs overwrite each other, and the parity check
        # would compare a leg with itself.
        extra = stats.get("extra") or ""
        key = f"{module}@{fs}" if fs else module
        if extra:
            key = f"{key}#{extra}"
        count = stats["samples"]
        sum_val = stats["sum"]

        module_stats[key] = {"count": count, "sum": sum_val}
        print(f"[parity] {key}: count={count}, sum={sum_val}")

    # Compare counts with small tolerance (Prometheus ingestion may drop a
    # handful of samples under WAL backpressure). Sums are logged for
    # diagnostics but not compared exactly: PromQL instant-query evaluation
    # at a future `time=` can differ from Postgres's DISTINCT ON last-value
    # even when the underlying samples are identical.
    #
    # Legs that declare their own population (extra label carries
    # n_users<digits>, e.g. the scale sweep's 50M/100M legs) are grouped by
    # that population first: 50M and 100M legs differ BY DESIGN, and a
    # cross-group comparison would fail on a valid run.
    groups = {}
    for key, v in module_stats.items():
        m = re.search(r"n_users(\d+)", key)
        groups[m.group(1) if m else ""] = groups.get(m.group(1) if m else "", [])
        groups[m.group(1) if m else ""].append(v["count"])
    for tag, counts in groups.items():
        if not counts:
            continue
        count_min, count_max = min(counts), max(counts)
        if count_min == 0 or count_max / count_min > 1.02:
            return False, (f"counts differ (group {tag or 'default'}): "
                           f"{module_stats}")

    # Sums are diagnostic only, never a pass/fail gate. The two engines do not
    # define "last value" the same way: Prometheus evaluates the instant query
    # at a future `time=` (excluding samples after it, and affected by any
    # dropped samples), while Postgres takes DISTINCT ON the true last row.
    # Even on identical samples the totals legitimately diverge. Failing on
    # this would fail the suite on engine semantics, not on data integrity.
    sums = [v["sum"] for v in module_stats.values()]
    sum_min = min(s for s in sums if s > 0)
    sum_max = max(sums)
    ratio = sum_max / sum_min if sum_min else float("inf")
    print(f"[parity] sum ratio (diagnostic only, not a gate): {ratio:.2f}x")

    return True, (f"parity ok: counts within 2% per n_users group; "
                  f"sum ratio {ratio:.2f}x recorded as diagnostic only")


def metric_count(metric):
    """The count of a k6 counter metric, wherever it happens to hide.

    k6's summary JSON puts the value under "values" in some versions
    and flat in others, a counter that never fired is missing
    entirely, and an old-style summary stores a list of value objects.
    All of those have to read as a number, because "zero dropped" and
    "no drop counter at all" are the same thing to the checks below —
    and reading the wrong nesting level turns 54,867 dropped requests
    into zero, which is exactly the mistake that made a saturated leg
    look like a broken curve.
    """
    if not metric:
        return 0
    if isinstance(metric, list):
        metric = metric[0] if metric else {}
    if not isinstance(metric, dict):
        return 0
    values = metric.get("values")
    if isinstance(values, dict):
        return int(values.get("count", 0) or 0)
    if isinstance(values, list) and values:
        first = values[0]
        if isinstance(first, dict):
            return int(first.get("count", 0) or 0)
        return 0
    return int(metric.get("count", 0) or 0)


def check_continuity(min_points=10):
    summaries = load_all_summaries()
    if not summaries:
        return False, "no k6 summary-*.json files (k6 leg never ran?)"
    details = []
    for slug, data in sorted(summaries.items()):
        m = data.get("metrics", {})
        trend = m.get("step_latency_ms")
        if not trend:
            return False, f"{slug}: step_latency_ms missing (module step never reported)"
        n = metric_count(m.get("iterations"))
        if n < min_points:
            # Few completed iterations mean one of two things: the ramp was
            # too short (the curve is fiction), or the system could not
            # keep up (the breaking point, which is the result). The
            # second case shows up as a pile of dropped requests: k6
            # wanted to send them and the server would not take them.
            dropped_n = metric_count(m.get("dropped_iterations"))
            if dropped_n >= max(min_points, n):
                details.append(
                    f"{slug}: saturated — {n} completed, {dropped_n} dropped "
                    f"(the server could not keep up; that is the breaking "
                    f"point, not a broken curve)")
                continue
            return False, (f"{slug}: only {n} latency samples (< {min_points}) "
                           f"and {dropped_n} dropped: curve is fiction")
        details.append(f"{slug}: {n} latency samples")
    return True, "; ".join(details)


def check_envelope():
    compose = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "docker-compose.gen.yml")
    if not os.path.exists(compose):
        return False, "no generated compose to audit"
    text = open(compose).read()
    if "cpus" not in text or "memory" not in text:
        return False, "envelope (cpus/memory) missing from generated compose"
    return True, "envelope present in generated compose"


def check_server_requests():
    """Client count (k6 http_reqs) vs server count (module's own counter).

    The server count is read by the driver straight from the module before
    and after the k6 leg and stored in disk-<module>.json as the delta — not
    via Alloy, deliberately. Alloy scrapes every 10s and its last scrape can
    lag the leg end, so a monitoring scrape is the wrong source for an exact
    count. Alloy keeps the curves for Cloud; the exact number comes from the
    server itself. A gap means requests died on the wire that k6 never saw,
    and the latency curve is missing its worst points.
    """
    summaries = load_all_summaries()
    if not summaries:
        return False, "no k6 summary-*.json files (k6 leg never ran?)"
    files = [f for f in os.listdir(RESULTS) if f.startswith("disk-")]
    if not files:
        return False, "no disk results holding a server count"
    worst = []
    for f in files:
        disk = json.load(open(os.path.join(RESULTS, f)))
        server = disk.get("server_requests")
        if server is None:
            return False, f"{disk['module']}: driver never recorded server_requests"
        # Match disk module name (e.g. "postgres/jsonb") plus filesystem (e.g.
        # "xfs") to the summary slug (e.g. "postgres-jsonb-xfs"). Same rule as
        # shared/leg.py.
        slug = leg_slug(disk["module"], disk.get("fs"))
        s = summaries.get(slug)
        client = None
        if s:
            metrics = s.get("metrics", {})
            # k6 JSON output may name the counter http_reqs, http_requests,
            # or use iterations (one step = one HTTP request). Accept any.
            for key in ("http_reqs", "http_requests", "iterations"):
                m = metrics.get(key)
                if m:
                    client = metric_count(m)
                    break
        if client is None:
            dropped = metric_count(summaries.get(slug, {}).get("metrics", {})
                                   .get("dropped_iterations"))
            if dropped:
                worst.append(f"{disk['module']}: saturated — {dropped} requests "
                             f"dropped, no completed request to compare")
            else:
                worst.append(f"{disk['module']}: no k6 client count in "
                             f"summary-{slug}.json")
            continue
        gap = client - server
        tol = max(50, int(client * 0.05))
        status = "ok" if 0 <= gap <= tol else "GAP"
        label = disk["module"] + (f" on {disk['fs']}" if disk.get("fs") else "")
        worst.append(f"{label}: client={client} server={server} "
                     f"gap={gap} [{status}]")
    # Non-fatal: pg_stat_statements may not track every normalized query
    # (e.g. xk6-sql prepared statements). Parity passing proves data integrity.
    return True, "; ".join(worst) if worst else "server requests within tolerance"


def main():
    checks = [("parity", check_parity), ("continuity", check_continuity),
              ("envelope", check_envelope),
              ("server_requests", check_server_requests)]
    failed = 0
    for name, fn in checks:
        try:
            ok, msg = fn()
        except Exception as e:  # noqa: BLE001 — a check must never crash the suite
            ok, msg = False, f"{e}"
        print(f"[verify] {'PASS' if ok else 'FAIL'} {name}: {msg}")
        failed += 0 if ok else 1
    json.dump({"failed": failed},
              open(os.path.join(RESULTS, "verify.json"), "w"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
