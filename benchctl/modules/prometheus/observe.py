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
import urllib.request
import urllib.parse

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


def sample_stats():
    """Return {'samples': count, 'sum': value} from Prometheus query API.

    Uses count_over_time + sum over the 45m window matching the dataset.
    """
    # count_over_time over the window used in dataset (45m)
    q = f"sum(count_over_time(http_requests_total[45m]))"
    url_q = QUERY_URL + "?query=" + urllib.parse.quote(q)
    with urllib.request.urlopen(url_q, timeout=30) as r:
        data = json.load(r)
    res = data["data"]["result"]
    count = int(float(res[0]["value"][1])) if res else 0

    q = f"sum(http_requests_total)"
    url_q = QUERY_URL + "?query=" + urllib.parse.quote(q)
    with urllib.request.urlopen(url_q, timeout=30) as r:
        data = json.load(r)
    res = data["data"]["result"]
    sum_val = float(res[0]["value"][1]) if res else 0.0

    return {"samples": count, "sum": sum_val}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(sample_stats()))
    else:
        print(json.dumps(server_requests()))