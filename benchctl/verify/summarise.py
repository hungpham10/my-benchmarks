"""verify/summarise.py — one-line-per-fact summary for the CI step summary.

Kept as a file (not inline in the workflow) because inline python with
colons breaks YAML parsing — which is exactly the kind of silent breakage
this framework exists to avoid.
"""
import json
import os

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "results")

d = json.load(open(os.path.join(RESULTS, "summary.json")))["metrics"]
print("iterations:", d["iterations"]["values"]["count"])
p50 = round(d["http_req_duration"]["values"]["med"], 2)
p95 = round(d["http_req_duration"]["values"]["p(95)"], 2)
print(f"http med/p95: {p50} / {p95} ms")
print("dropped_iterations:",
      d.get("dropped_iterations", {}).get("values", {}).get("count", 0))
