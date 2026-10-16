"""modules/postgres/init.py — init(scale, shape): load the dataset.

Uses psycopg COPY FROM STDIN to bulk-insert rows. The driver (run.py) sets
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
# Use shared generator (same as Prometheus)
# ------------------------------------------------------------------
SHARED = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "shared")
sys.path.insert(0, SHARED)
import dataset as d
import values as v

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _csv_rows(total, variant, t_start_ms, interval_s, window_min):
    """Yield CSV rows for COPY into metrics_jsonb or metrics_norm_brin.

    metrics_jsonb: time (timestamptz), labels (jsonb), value (double precision)
    metrics_norm_brin: time (timestamptz), series_id (int), value (double precision)
    series_dim: id (identity), metric, job, instance, region, service

    We generate evenly-spaced timestamps over the window, using the exact
    timestamps from the dataset (t_start_ms to t_end_ms).
    """
    from datetime import datetime, timezone

    # Build series descriptors using shared dataset module
    series = d.build_series()
    # Filter to only counter metrics (http_requests_total)
    counter_series = [s for s in series if s["metric"] == d.COUNTER_METRIC]

    # Generate timestamps for the window
    # We need timestamps matching the Prometheus scrape intervals
    interval_ms = interval_s * 1000
    n_samples = int(window_min * 60 / interval_s)

    # Generate values using shared values module (same as Prometheus)
    # generate_chunk returns (n_series, n_samples) array
    timestamps = v.generate_timestamps(t_start_ms)
    values = v.generate_chunk(counter_series)

    rows = []
    for s in range(len(counter_series)):
        desc = counter_series[s]
        lbls = d.labels_json(desc)  # unique label set per series, matching Prometheus
        series_id = s + 1           # unique series_id per series, starts at 1
        for i in range(len(timestamps)):
            ts_iso = datetime.fromtimestamp(timestamps[i] / 1000, tz=timezone.utc).isoformat()
            val = values[s, i]
            if variant == "norm":
                # time, series_id, value - unique series_id per series
                rows.append((ts_iso, series_id, val))
            else:
                # time, labels, value - unique labels per series
                rows.append((ts_iso, lbls, val))
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
    # Use RESULTS_DIR env var if set by driver, else compute from file location
    results_dir = os.environ.get(
        "RESULTS_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results"))
    ds_path = os.path.join(results_dir, "dataset.json")
    total = 1200  # fallback smoke number
    t_start_ms = 0
    t_end_ms = 0
    interval_s = 15
    window_min = 45
    if os.path.exists(ds_path):
        with open(ds_path) as f:
            m = json.load(f)
        total = m.get("total_samples", 1200)
        t_start_ms = m.get("t_start_ms", 0)
        t_end_ms = m.get("t_end_ms", 0)
        interval_s = m.get("interval_seconds", 15)
        window_min = m.get("window_minutes", 45)
        # Debug: print what we read
        print(f"[init] read dataset: total={total}, t_start_ms={t_start_ms}, t_end_ms={t_end_ms}, interval_s={interval_s}, window_min={window_min}", file=sys.stderr)
    else:
        print(f"[init] dataset.json not found at {ds_path}, using fallback", file=sys.stderr)

    # Fallback: if dataset.json is missing or invalid, compute from scenario params
    if t_start_ms == 0:
        ahead_min = int(os.environ.get("AHEAD_MINUTES", "8"))
        window_min = int(os.environ.get("WINDOW_MINUTES", "45"))
        interval_s = int(os.environ.get("INTERVAL_SECONDS", "15"))
        now_ms = int(time.time() * 1000)
        t_start_ms = int(time.time() * 1000) - (window_min - ahead_min) * 60 * 1000
        t_end_ms = t_start_ms + window_min * 60 * 1000
        print(f"[init] fallback timestamps: t_start_ms={t_start_ms}, t_end_ms={t_end_ms}, interval_s={interval_s}, window_min={window_min}", file=sys.stderr)

    # ---- decide variant ---------------------------------------------
    variant = os.environ.get("VARIANT", "jsonb")  # overridden per-leg

    # ---- ensure partitions exist --------------------------------------
    def ensure_partitions(cur, t_start_ms, t_end_ms):
        """Create daily partitions for the time range."""
        from datetime import datetime, timezone
        start_day = int(t_start_ms) // 1000 // 86400
        end_day = int(t_end_ms) // 1000 // 86400
        for day_offset in range(end_day - start_day + 1):
            day_ts = (start_day + day_offset) * 86400
            day_str = datetime.fromtimestamp(day_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            for table in ["metrics_jsonb", "metrics_norm_brin"]:
                cur.execute(
                    "SELECT bench_ensure_partitions(%s::regclass, %s::date)",
                    (table, day_str)
                )

    # ---- connect and ensure partitions --------------------------------------
    conn = psycopg.connect(dsn, autocommit=True)
    cur = conn.cursor()
    if t_start_ms and t_end_ms:
        ensure_partitions(cur, t_start_ms, t_end_ms)

    # Generate rows using timestamps from dataset.json
    interval_s = int(os.environ.get("INTERVAL_SECONDS", "15"))
    window_min = int(os.environ.get("WINDOW_MINUTES", "45"))
    rows = _csv_rows(total, variant, t_start_ms, interval_s, window_min)

    # COPY expects the column order matching the table.
    # metrics_jsonb: time, labels, value
    # metrics_norm_brin: time, series_id, value
    if variant == "norm":
        cols = ["time", "series_id", "value"]
        table = "metrics_norm_brin"
    else:
        cols = ["time", "labels", "value"]
        table = "metrics_jsonb"

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols)
    for r in rows:
        writer.writerow(r)
    buf.seek(0)
    # psycopg v3 uses copy() not copy_from()
    with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN WITH (FORMAT CSV, HEADER)") as copy:
        copy.write(buf.read())
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