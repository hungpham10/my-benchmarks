"""verify/summarise.py — one-line-per-fact summary for the CI step summary.

Kept as a file (not inline in the workflow) because inline python with
colons breaks YAML parsing — which is exactly the kind of silent breakage
this framework exists to avoid.

Prints one line per system from summary-<slug>.json files.
"""
import json
import os

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "results")


def load_all_summaries():
    out = {}
    if not os.path.isdir(RESULTS):
        return out
    for f in sorted(os.listdir(RESULTS)):
        if f.startswith("summary-") and f.endswith(".json"):
            key = f[len("summary-"):-5]
            out[key] = json.load(open(os.path.join(RESULTS, f)))
    return out


summaries = load_all_summaries()
if not summaries:
    print("no k6 summary-*.json files")
    raise SystemExit(0)

for slug in sorted(summaries):
    d = summaries[slug].get("metrics", {})
    iters = d.get("iterations", {}).get("values", {}).get("count", 0)
    dur = d.get("iteration_duration") or d.get("http_req_duration", {})
    dur_vals = dur.get("values", {})
    p50 = round(dur_vals.get("med", 0), 2)
    p95 = round(dur_vals.get("p(95)", 0), 2)
    dropped = d.get("dropped_iterations", {}).get("values", {}).get("count", 0)
    print(f"{slug}: iterations={iters:,} med={p50}ms p95={p95}ms dropped={dropped:,}")
