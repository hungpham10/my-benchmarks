"""modules/prometheus/requests.py — server_requests(): what did the SERVER handle?

Parses the /metrics exposition text directly, NOT the query API. The SUT
does not scrape itself (scrape_configs empty by design), so its own query
API cannot see prometheus_http_requests_total at all — asking it returns 0
for every handler. The exposition endpoint is live instrumentation and has
the truth. Prints {"server_requests": N} as JSON, last line.
"""
import json
import os
import re
import urllib.request

SUT = os.environ.get("SUT_URL", "http://localhost:19090")
WANT = ('prometheus_http_requests_total{handler="/api/v1/query",code="200"}',)


def server_requests():
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


if __name__ == "__main__":
    print(json.dumps(server_requests()))
