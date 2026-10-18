"""modules/prometheus/observe.py — SUT observation (server requests + sample stats).

Combines the old requests.py (server_requests) with new sample_stats() for
parity verification. Both functions query the live SUT and return JSON.

server_requests(): counts handler=/api/v1/query requests from /metrics endpoint.
sample_stats(): queries Prometheus API for count and sum of http_requests_total
over the benchmark window.

Both print {"server_requests": N} or {"samples": N, "sum": X} as JSON, last line.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.parse

SHARED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "shared")

SUT = os.environ.get("SUT_URL", "http://localhost:19090")
QUERY_URL = SUT + "/api/v1/query"
WANT = ('prometheus_http_requests_total{handler="/api/v1/query",code="200"}',)


def server_requests():
    """Return {'server_requests': N} from Prometheus /metrics."""
    with urllib.request.urlopen(SUT + "/metrics", timeout=30) as r:
        text = r.read().decode()
    total = 0
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        name, _, rest = line.partition("{")
        if name != "prometheus_http_requests_total":
            continue
        labels, _, value = rest.partition("} ")
        if ('handler="/api/v1/query"' in labels and 'code="200"' in labels):
            total += int(float(value))
    return {"server_requests": total}


def _read_dataset():
    """Read dataset.json and return (window_minutes, t_start_ms, t_end_ms)."""
    results_dir = os.environ.get(
        "RESULTS_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "results"))
    ds_path = os.path.join(results_dir, "dataset.json")
    if os.path.exists(ds_path):
        with open(ds_path) as f:
            m = json.load(f)
            return (m.get("window_minutes", 45), m.get("t_start_ms", 0), m.get("t_end_ms", 0))
    return 45, 0, 0


def sample_stats():
    """Return {'samples': count, 'sum': value} from Prometheus query API.

    - samples: count of all samples over the window across both metric families
      (http_requests_total counter + queue_depth gauge), matching Postgres loaders.
    - sum: sum of last value per series (counter state at window end).
    """
    window_min, t_start_ms, t_end_ms = _read_dataset()
    total_count = 0
    for metric in ("http_requests_total", "queue_depth"):
        # Count of samples - use range query at t_end_ms to capture full window
        q = f"sum(count_over_time({metric}[{window_min}m]))"
        url_q = QUERY_URL + "?query=" + urllib.parse.quote(q)
        if t_end_ms:
            url_q += f"&time={t_end_ms / 1000}"
        with urllib.request.urlopen(url_q, timeout=30) as r:
            data = json.load(r)
        res = data["data"]["result"]
        count = int(float(res[0]["value"][1])) if res else 0
        total_count += count

    # Sum of last value per series (counter end state) - evaluate at t_end_ms
    q = f"http_requests_total"
    url_q = QUERY_URL + "?query=" + urllib.parse.quote(q)
    if t_end_ms:
        url_q += f"&time={t_end_ms / 1000}"
    with urllib.request.urlopen(url_q, timeout=30) as r:
        data = json.load(r)
    res = data["data"]["result"]
    sum_val = 0.0
    for sample in res:
        sum_val += float(sample["value"][1])

    return {"samples": total_count, "sum": sum_val}


def qbench():
    """Measure latency for each query in shared/queries.py via Prometheus API.

    Runs every q1-q5 PromQL against /api/v1/query at t_end_ms (instant query,
    explicit time so future-dated data is visible). Returns JSON dict keyed by
    query name with p50/p95/max ms plus iteration count.
    """
    sys.path.insert(0, SHARED)
    from querybench import run_all

    _, t_start_ms, t_end_ms = _read_dataset()

    def exec_fn(name, promql):
        url = QUERY_URL + "?query=" + urllib.parse.quote(promql)
        if t_end_ms:
            url += f"&time={t_end_ms / 1000}"
        with urllib.request.urlopen(url, timeout=120) as r:
            data = json.load(r)
        res = data.get("data", {}).get("result", [])
        return len(res)

    results = run_all(exec_fn, t_start_ms, t_end_ms, "prometheus")
    print(json.dumps(results))


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(sample_stats()))
    elif len(sys.argv) > 1 and sys.argv[1] == "qbench":
        qbench()
    else:
        print(json.dumps(server_requests()))