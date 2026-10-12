#!/usr/bin/env python3
"""Query the Grafana Cloud stack the benchmark pushes to.

Uses a read token supplied through the environment:

    GRAFANA_CLOUD_READ_TOKEN=glc_... python3 monitoring/query_cloud.py 'up'

The token is never written to disk, never committed, and never echoed. A read
scope is a separate service-account token from the push one; the access policy
token the harness pushes with cannot answer this, which is the whole reason
this script exists.
"""
import json
import os
import subprocess
import sys
import urllib.parse

ENDPOINT = os.environ.get(
    "GRAFANA_CLOUD_QUERY_URL",
    "https://prometheus-prod-18-prod-ap-southeast-0.grafana.net")


def query(expr, user="1182692"):
    token = os.environ.get("GRAFANA_CLOUD_READ_TOKEN", "")
    if not token:
        raise SystemExit("set GRAFANA_CLOUD_READ_TOKEN (a service-account token "
                         "with metrics:read; the push token cannot do this)")
    url = "%s/prometheus/api/v1/query?query=%s" % (
        ENDPOINT, urllib.parse.quote(expr))
    out = subprocess.run(
        ["curl", "-s", "-u", "%s:%s" % (user, token), url],
        capture_output=True, text=True).stdout
    payload = json.loads(out)
    if payload.get("status") != "success":
        raise SystemExit("query failed: %s" % payload.get("error"))
    return payload["data"]["result"]


if __name__ == "__main__":
    for row in query(sys.argv[1] if len(sys.argv) > 1 else "up"):
        labels = {k: v for k, v in row["metric"].items() if k != "__name__"}
        print("%-40s %s  %s" % (row["metric"].get("__name__", ""),
                                 row["value"][1], labels or ""))