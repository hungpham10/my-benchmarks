#!/usr/bin/env python3
"""Merge result_*.jsonl into a comparison table (stdout + CSV)."""
import csv
import glob
import json
import sys
from collections import defaultdict

data = defaultdict(dict)
meta = {}

for f in glob.glob("out/result_*.jsonl"):
    for line in open(f):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        tag = r["tag"]
        if "phase" in r and r["phase"] != "balance":
            data[tag][r["phase"]] = r
        elif "rows" in r and "workers" in r:
            meta[tag] = r  # metadata record (no "phase" key)

cols = [
    ("tag", lambda t: t),
    ("FS", lambda t: meta.get(t, {}).get("fs_label", "")),
    ("rows", lambda t: meta.get(t, {}).get("rows", "")),
    ("LOAD rows/s", lambda t: data[t].get("load", {}).get("rows_per_sec")),
    ("LOAD p95 ms", lambda t: data[t].get("load", {}).get("lat_p95")),
    ("WAL B/row", lambda t: data[t].get("load", {}).get("wal_bytes_per_row")),
    ("HYB ins/s", lambda t: round(data[t.get("tag", t)].get("hybrid", {}).get("inserts", 0)) if False else None),
    ("HYB p95 ms", lambda t: data[t].get("hybrid", {}).get("read_p95_ms")),
    ("RO reads/s", lambda t: data[t].get("readonly", {}).get("reads_per_sec")),
    ("RO p95 ms", lambda t: data[t].get("readonly", {}).get("lat_p95_ms")),
    ("cache hit", lambda t: data[t].get("cache", {}).get("hit_ratio")),
    ("shard spread%", lambda t: data[t].get("balance", {}).get("spread_pct")),
]

# simpler col access via helper
def cell(tag, phase, field):
    r = data[tag].get(phase)
    return r.get(field) if r else None

HEAD = ["tag", "load_rows_s", "load_p95_ms", "wal_b_row",
        "hyb_ins_s", "hyb_p95_ms", "ro_reads_s", "ro_p95_ms",
        "cache_hit", "spread_pct", "rows", "workers"]
rows_out = []
for tag in sorted(data):
    rows_out.append([
        tag,
        cell(tag, "load", "rows_per_sec"),
        cell(tag, "load", "lat_p95"),
        cell(tag, "load", "wal_bytes_per_row"),
        cell(tag, "hybrid", "inserts"),
        cell(tag, "hybrid", "read_p95_ms"),
        cell(tag, "readonly", "reads_per_sec"),
        cell(tag, "readonly", "lat_p95_ms"),
        cell(tag, "cache", "hit_ratio"),
        cell(tag, "balance", "spread_pct"),
        meta.get(tag, {}).get("rows"),
        meta.get(tag, {}).get("workers"),
    ])

w = csv.writer(sys.stdout)
w.writerow(HEAD)
w.writerows(rows_out)
with open("out/summary.csv", "w", newline="") as fh:
    cw = csv.writer(fh)
    cw.writerow(HEAD)
    cw.writerows(rows_out)
print("wrote out/summary.csv", file=sys.stderr)
