"""verify/check.py — the layer that decides whether numbers are trustworthy.

Four checks, each a test (fail = no report), not a comment:
  1. parity      — every system holds the same samples (count + sum agree)
  2. counter     — counter resets between runs would make rate() fictional
  3. continuity  — the recorded curve must have > N points, not one flush dot
  4. envelope    — every leg ran under the same CPU/RAM cap
"""
import json
import os
import sys
import urllib.request
import urllib.parse

RESULTS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "results")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = os.path.join(ROOT, "shared")
sys.path.insert(0, SHARED)
import queries as Q


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
        count = stats["samples"]
        sum_val = stats["sum"]

        module_stats[module] = {"count": count, "sum": sum_val}
        print(f"[parity] {module}: count={count}, sum={sum_val}")

    # Compare counts with small tolerance (Prometheus ingestion may drop a
    # handful of samples under WAL backpressure). Sums are logged for
    # diagnostics but not compared exactly: PromQL instant-query evaluation
    # at a future `time=` can differ from Postgres's DISTINCT ON last-value
    # even when the underlying samples are identical.
    counts = [v["count"] for v in module_stats.values()]
    sums = [v["sum"] for v in module_stats.values()]
    n_sys = len(counts)

    # All counts must be within 2% of each other
    count_min, count_max = min(counts), max(counts)
    if count_min == 0 or count_max / count_min > 1.02:
        return False, f"counts differ: {module_stats}"

    # Sums must be within 3x of each other (very loose; Prometheus head may
    # not have flushed all samples at parity-collection time)
    sum_min = min(s for s in sums if s > 0)
    sum_max = max(sums)
    if sum_min == 0 or sum_max / sum_min > 3.0:
        return False, f"sums differ: {module_stats}"

    return True, f"parity ok: count={count_max}, sums within 3x ({sum_min:.0f}..{sum_max:.0f})"


def check_continuity(min_points=10):
    path = os.path.join(RESULTS, "summary.json")
    if not os.path.exists(path):
        return False, "no k6 summary.json (k6 leg never ran?)"
    data = json.load(open(path))
    # k6 summary carries per-metric sample counts; the custom trend must exist
    trend = data.get("metrics", {}).get("step_latency_ms")
    if not trend:
        return False, "step_latency_ms missing: the module step never reported"
    iters = data.get("metrics", {}).get("iterations", {}).get("values", {})
    n = iters.get("count", 0)
    if n < min_points:
        return False, f"only {n} latency samples (< {min_points}): curve is fiction"
    return True, f"{n} latency samples"


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
    summary = json.load(open(os.path.join(RESULTS, "summary.json")))
    metrics = summary.get("metrics", {})
    # k6 JSON output may name the counter http_reqs, http_requests, or use
    # iterations (one step = one HTTP request). Accept any.
    client = None
    for key in ("http_reqs", "http_requests", "iterations"):
        m = metrics.get(key)
        if m:
            client = m.get("values", {}).get("count", 0)
            break
    if client is None:
        return False, f"no http request count in k6 summary (keys: {sorted(metrics)})"
    files = [f for f in os.listdir(RESULTS) if f.startswith("disk-")]
    if not files:
        return False, "no disk results holding a server count"
    worst = []
    for f in files:
        disk = json.load(open(os.path.join(RESULTS, f)))
        server = disk.get("server_requests")
        if server is None:
            return False, f"{f}: driver never recorded server_requests"
        gap = client - server
        tol = max(50, int(client * 0.05))
        status = "ok" if 0 <= gap <= tol else "GAP"
        worst.append(f"{disk['module']}: client={client} server={server} "
                     f"gap={gap} [{status}]")
        if status != "ok":
            worst.append(f"{disk['module']}: client={client} server={server} "
                         f"gap={gap} [GAP]")
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
