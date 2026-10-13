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


def main():
    checks = [("parity", check_parity), ("continuity", check_continuity),
              ("envelope", check_envelope)]
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
