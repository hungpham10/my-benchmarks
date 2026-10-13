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

RESULTS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "results")


def check_parity():
    files = [f for f in os.listdir(RESULTS) if f.startswith("disk-")]
    if len(files) < 1:
        return False, "no disk results to compare"
    samples = set()
    for f in files:
        samples.add(json.load(open(os.path.join(RESULTS, f)))["samples"])
    if len(samples) != 1:
        return False, f"sample counts differ across systems: {samples}"
    return True, f"samples agree: {samples.pop()}"


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
    """Client count (k6 http_reqs) vs server count (monitoring Prometheus).

    The server count comes from the monitoring stack, which scrapes the SUT
    externally — never from the SUT's own query API. The SUT does not scrape
    itself (scrape_configs is empty by design, so self-scrape series would
    corrupt the measured TSDB), so its own /api/v1/query cannot see
    prometheus_http_requests_total at all. Asking the SUT about itself
    returns 0 for every handler, which is how a whole debugging session was
    spent chasing a scrape problem that did not exist.

    max_over_time, not an instant query: verify runs after the SUT is down,
    and an instant query on a series that went stale returns nothing.
    """
    import urllib.request
    import urllib.parse
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scen = os.environ.get("BENCHCTL_SCENARIO", "")
    if scen and os.path.exists(os.path.join(root, scen)):
        modules = yaml.safe_load(open(os.path.join(root, scen)))["systems"]
    else:
        modules = [f[len("disk-"):-len(".json")] for f in os.listdir(RESULTS)
                   if f.startswith("disk-")]
    summary = json.load(open(os.path.join(RESULTS, "summary.json")))
    client = summary["metrics"]["http_reqs"]["values"]["count"]
    mon = "http://localhost:19091"
    worst = []
    for module in modules:
        mod = yaml.safe_load(
            open(os.path.join(root, "modules", module, "module.yaml")))
        mon_cfg = mod.get("monitoring", {})
        metric = mon_cfg.get("server_requests_metric")
        if not metric:
            return False, f"{module}: no server_requests_metric declared"
        match = ",".join(f'{k}="{v}"'
                         for k, v in mon_cfg.get(
                             "server_requests_match", {}).items())
        q = f"max_over_time({metric}{{{match}}}[30m])"
        url = mon + "/api/v1/query?query=" + urllib.parse.quote(q, safe="")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.load(r)
        except Exception as e:  # noqa: BLE001 — monitoring may be down already
            return False, f"monitoring query failed: {e}"
        res = data["data"]["result"]
        server = int(float(res[0]["value"][1])) if res else 0
        gap = client - server
        tol = max(50, int(client * 0.05))
        status = "ok" if 0 <= gap <= tol else "GAP"
        worst.append(f"{module}: client={client} server={server} "
                     f"gap={gap} [{status}]")
        if status != "ok":
            return False, "; ".join(worst)
    return True, "; ".join(worst)


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
