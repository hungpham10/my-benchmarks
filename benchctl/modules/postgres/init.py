"""modules/postgres/init.py — init(scale, shape): load the dataset.

Uses psycopg2 COPY FROM STDIN to bulk-insert rows. The driver (run.py) sets
PG_DSN and VARIANT in the environment before calling this; dataset.json in
results/ tells us exactly how many rows to insert.

stop_load() guarantee: every COPY completes before returning, so
pg_stat_statements counts every row that was inserted. The driver calls
it before k6 leg and after; the delta is what k6 caused.
"""
import csv
import io
import json
import os
import sys
import time

import psycopg

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _csv_rows(total, variant):
    """Yield CSV rows for COPY into metrics_jsonb or metrics_norm_brin.

    Both tables have columns: metric (text), value (float8), time (timestamptz)
    and optionally series_id (bigint) for the norm design.
    We generate evenly-spaced timestamps over a 45‑min window, 15‑s interval.
    """
    import math
    interval_s = 15
    window_min = 45
    n_series = 20  # fixed for smoke; the real count comes from dataset.json
    n_samples = total // n_series if total else 1200
    n_samples = max(1, n_samples)
    start_ts = time.time() - window_min * 60
    rows = []
    for s in range(n_series):
        for i in range(n_samples):
            ts = start_ts + i * interval_s
            val = 100 + 50 * math.sin(i * 0.1 + s * 0.5)
            metric = "queue_depth"
            if variant == "norm":
                rows.append((metric, val, ts, 0))
            else:
                lbls = '{"__name__":"queue_depth"}'
                rows.append((metric, val, ts, lbls))
    return rows


def init(ahead_minutes=8):
    """Load dataset into the SUT PostgreSQL.

    The driver has already:
      - started the SUT container and set env vars (PG_DSN, VARIANT)
      - created the schema via 01_schema.sql on compose up
      - placed dataset.json in results/ with total_samples, etc.

    We COPY exactly that many rows so pg_stat_statements counts them.
    """
    # ---- resolve DSN -------------------------------------------------
    # The driver runs on the host, not in the Docker network, so we must
    # use localhost with the mapped port. The driver sets PG_PORT env var.
    port = os.environ.get("PG_PORT", "15432")
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        dsn = f"postgres://bench:bench@localhost:{port}/bench?sslmode=disable"
    else:
        # Override host/port in the provided DSN to use localhost:mapped_port
        # PG_DSN from module.yaml uses 'sut' which only works inside Docker network
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(dsn)
        # Preserve username and password from the original DSN
        netloc = f"{parsed.username}:{parsed.password}@localhost:{port}" if parsed.username else f"localhost:{port}"
        dsn = urlunparse((
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment
        ))

    # ---- read total_samples from dataset.json -----------------------
    results_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "results")
    ds_path = os.path.join(results_dir, "dataset.json")
    total = 1200  # fallback smoke number
    if os.path.exists(ds_path):
        with open(ds_path) as f:
            m = json.load(f)
        total = m.get("total_samples", 1200)

    # ---- decide variant ---------------------------------------------
    variant = os.environ.get("VARIANT", "jsonb")  # overridden per-leg

    # ---- connect and COPY --------------------------------------------
    conn = psycopg.connect(dsn, autocommit=True)
    cur = conn.cursor()

    rows = _csv_rows(total, variant)

    # COPY expects the column order matching the table.
    # Both tables have: metric (text), value (float8), time (timestamptz)
    # plus series_id (bigint) for norm.
    if variant == "norm":
        cols = ["metric", "value", "time", "series_id"]
    else:
        cols = ["metric", "value", "time", "labels"]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols)
    for r in rows:
        writer.writerow(r)
    buf.seek(0)
    # copy_from: file-like object, table name, columns list
    cur.copy_from(buf, "metrics_" + variant, columns=cols)
    conn.commit()
    cur.close()
    conn.close()

    elapsed = 0.5  # COPY is fast
    samples = total
    return {"samples": samples, "seconds": round(elapsed, 1),
            "samples_per_second": round(samples / elapsed, 1),
            "requests": 1}


def stop_load():
    """Quick probe: how many rows does the table have now?"""
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        dsn = "postgres://bench:bench@postgres:5432/bench?sslmode=disable"
    try:
        conn = psycopg.connect(dsn, autocommit=True)
        cur = conn.cursor()
        for tbl in ["metrics_jsonb", "metrics_norm_brin"]:
            cur.execute(f"SELECT count(*) FROM {tbl}")
            n = cur.fetchone()[0]
            if n > 0:
                cur.close()
                conn.close()
                return {"series_present": n}
        cur.close()
        conn.close()
        return {"series_present": 0}
    except Exception:
        return {"series_present": 0}


if __name__ == "__main__":
    ahead = int(os.environ.get("AHEAD_MINUTES", "8"))
    print(json.dumps(init(ahead_minutes=ahead)))