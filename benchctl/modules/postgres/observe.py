"""modules/postgres/observe.py — SUT observation (server requests + sample stats).

Combines the old requests.py (server_requests) with new sample_stats() for
parity verification. Both functions query the live SUT and return JSON.

server_requests(filter_sql): counts query executions from pg_stat_statements.
sample_stats(variant): returns {'samples': count, 'sum': value} from the
appropriate table (metrics_jsonb or metrics_norm_brin).

Both print JSON, last line.
"""
import json
import os
import sys
from decimal import Decimal
from urllib.parse import urlparse, urlunparse

import psycopg

# The driver sets PG_DSN and PG_PORT; fall back to defaults.
PG_DSN = os.environ.get(
    "PG_DSN",
    "postgres://bench:bench@postgres:5432/bench?sslmode=disable",
)
PG_PORT = os.environ.get("PG_PORT", "15432")

SHARED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "shared")


def _rewrite_dsn(dsn: str, port: str) -> str:
    """Rewrite DSN to use localhost:mapped_port instead of container hostname."""
    parsed = urlparse(dsn)
    # Preserve username and password from the original DSN
    netloc = f"{parsed.username}:{parsed.password}@localhost:{port}" if parsed.username else f"localhost:{port}"
    return urlunparse((
        parsed.scheme,
        netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment
    ))


def server_requests(filter_sql=None):
    """Count total query executions in pg_stat_statements.

    pg_stat_statements stores one row per normalized query text, with a
    `calls` column counting how many times that query was executed. The
    correct metric for "how many queries did k6 send" is SUM(calls), not
    COUNT(*) — the latter counts distinct normalized queries, which is
    always 1 for a single parametrized query run many times.
    """
    dsn = _rewrite_dsn(PG_DSN, PG_PORT)
    conn = psycopg.connect(dsn, autocommit=True)
    cur = conn.cursor()

    if filter_sql:
        cur.execute(
            "SELECT sum(calls) FROM pg_stat_statements WHERE " + filter_sql,
        )
    else:
        cur.execute("SELECT sum(calls) FROM pg_stat_statements")

    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    # Convert Decimal to int for JSON serialization
    return int(n) if n is not None else 0


def sample_stats(variant=None):
    """Return {'samples': count, 'sum': value} from the data table.

    variant: "jsonb" or "norm" — determines which table to query.
    If not provided, infers from VARIANT env var or tries both.

    Returns:
    - samples: count of all rows (counter + gauge), matching Prometheus's
      count across both metric families
    - sum: sum of LAST value per series for the COUNTER metric only, which
      matches Prometheus's sum(http_requests_total) and keeps the two
      comparable now that the gauge metric is also loaded.
    """
    sys.path.insert(0, SHARED)
    from dataset import COUNTER_METRIC

    dsn = _rewrite_dsn(PG_DSN, PG_PORT)
    conn = psycopg.connect(dsn, autocommit=True)
    cur = conn.cursor()

    if variant is None:
        variant = os.environ.get("VARIANT", "jsonb")

    if variant == "norm":
        table = "metrics_norm_brin"
        # Count all rows
        cur.execute(f"SELECT count(*) FROM {table}")
        count = int(cur.fetchone()[0] or 0)
        # Sum of last counter value per series_id (counter metric only).
        cur.execute(f"""
            SELECT sum(last_val) FROM (
                SELECT DISTINCT ON (m.series_id) m.value AS last_val
                FROM {table} m
                JOIN series_dim s ON s.id = m.series_id
                WHERE s.metric = '{COUNTER_METRIC}'
                ORDER BY m.series_id, m.time DESC
            ) sub
        """)
        sum_val = float(cur.fetchone()[0] or 0.0)
    else:
        table = "metrics_jsonb"
        # Count all rows
        cur.execute(f"SELECT count(*) FROM {table}")
        count = int(cur.fetchone()[0] or 0)
        # Sum of last counter value per series (counter metric only).
        cur.execute(f"""
            SELECT sum(last_val) FROM (
                SELECT DISTINCT ON (labels) value AS last_val
                FROM {table}
                WHERE labels @> '{{"__name__":"{COUNTER_METRIC}"}}'
                ORDER BY labels, time DESC
            ) sub
        """)
        sum_val = float(cur.fetchone()[0] or 0.0)

    cur.close()
    conn.close()

    return {"samples": count, "sum": sum_val}


def io_stats():
    """What the filesystem did, as PostgreSQL accounted for it.

    pg_stat_io counts the syscalls PostgreSQL made and how long each one
    took. write_time, fsync_time, writeback_time and read_time are the
    filesystem's own numbers, measured at the syscall boundary, from the same
    process that produced the workload. That makes them the closest thing to
    a filesystem benchmark a database can give us — and unlike a synthetic
    fio run, they describe the I/O the database actually did.

    The columns changed between versions (fsyncs/fsync_time arrived in
    PG 17; writebacks/writeback_time and the per-operation byte counters are
    new in PG 18), so the query is built from the columns the server
    actually has rather than from a guess.
    """
    dsn = _rewrite_dsn(PG_DSN, PG_PORT)
    conn = psycopg.connect(dsn, autocommit=True)
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'pg_catalog' AND table_name = 'pg_stat_io'
        """)
        have = {r[0] for r in cur.fetchall()}
        if not have:
            return {"available": False, "reason": "pg_stat_io does not exist"}

        wanted = [
            ("reads", "reads"), ("read_time", "read_time"),
            ("read_bytes", "read_bytes"),
            ("writes", "writes"), ("write_time", "write_time"),
            ("write_bytes", "write_bytes"),
            ("writebacks", "writebacks"), ("writeback_time", "writeback_time"),
            ("extends", "extends"), ("extend_time", "extend_time"),
            ("extend_bytes", "extend_bytes"),
            ("fsyncs", "fsyncs"), ("fsync_time", "fsync_time"),
        ]
        cols = [(col, alias) for col, alias in wanted if col in have]
        select = ", ".join(f"sum({col}) AS {alias}" for col, alias in cols)
        cur.execute(f"""
            SELECT backend_type, {select}
            FROM pg_stat_io
            GROUP BY backend_type
            ORDER BY backend_type
        """)
        names = [d[0] for d in cur.description]
        # The byte counters are `numeric`, which comes back as Decimal.
        # Decimal does not survive JSON serialisation, and mixing it with
        # float raises on division, so everything is normalised to float
        # here rather than at every use.
        per_backend = [
            {k: (float(v) if isinstance(v, Decimal) else v)
             for k, v in zip(names, r)}
            for r in cur.fetchall()
        ]
    finally:
        cur.close()
        conn.close()

    def total(key):
        return float(sum((r.get(key) or 0) for r in per_backend))

    def avg_us(time_key, count_key):
        n = total(count_key)
        t = total(time_key)
        return round(t * 1000 / n, 1) if n else None

    return {
        "available": True,
        "columns": sorted(have),
        "writes": int(total("writes")),
        "write_ms": float(total("write_time")),
        # Average latency per operation. These are the numbers the report
        # puts side by side: the same write, on four filesystems.
        "write_us_avg": avg_us("write_time", "writes"),
        "writebacks": int(total("writebacks")),
        "writeback_ms": float(total("writeback_time")),
        "writeback_us_avg": avg_us("writeback_time", "writebacks"),
        "fsyncs": int(total("fsyncs")),
        "fsync_ms": float(total("fsync_time")),
        "fsync_us_avg": avg_us("fsync_time", "fsyncs"),
        "reads": int(total("reads")),
        "read_ms": float(total("read_time")),
        "read_us_avg": avg_us("read_time", "reads"),
        "extends": int(total("extends")),
        "extend_ms": float(total("extend_time")),
        "extend_us_avg": avg_us("extend_time", "extends"),
        "write_bytes": int(total("write_bytes")),
        "read_bytes": int(total("read_bytes")),
        "per_backend": per_backend,
    }


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


def qbench(variant=None):
    """Measure latency for each query in shared/queries.py via psycopg SQL.

    Runs every q1-q5 SQL against the appropriate table (metrics_jsonb or
    metrics_norm_brin) with the benchmark time window substituted. Returns
    JSON dict keyed by query name with p50/p95/max ms plus iteration count.
    """
    import sys
    sys.path.insert(0, SHARED)
    from querybench import run_all

    if variant is None:
        variant = os.environ.get("VARIANT", "jsonb")

    # variant is "jsonb" | "norm" from driver (split_system), but run_all()
    # looks up shared/queries.py by backend key: "metrics_jsonb" | "metrics_norm_brin".
    VARIANT_TO_BACKEND = {"jsonb": "metrics_jsonb", "norm": "metrics_norm_brin"}
    if variant not in VARIANT_TO_BACKEND:
        raise RuntimeError(f"unknown postgres variant: {variant!r} (expected jsonb or norm)")
    backend = VARIANT_TO_BACKEND[variant]

    dsn = _rewrite_dsn(PG_DSN, PG_PORT)
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        _, t_start_ms, t_end_ms = _read_dataset()

        def exec_fn(name, sql):
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()  # consume results so timing includes data transfer
            n = len(rows)
            cur.close()
            return n

        results = run_all(exec_fn, t_start_ms, t_end_ms, backend,
                          measure=int(os.environ.get("QBENCH_MEASURE", "30")),
                          max_budget_s=int(os.environ.get("QBENCH_BUDGET_S", "120")))
    finally:
        conn.close()
    print(json.dumps(results))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        variant = os.environ.get("VARIANT", "jsonb")
        if len(sys.argv) > 2:
            variant = sys.argv[2]
        print(json.dumps(sample_stats(variant)))
    elif len(sys.argv) > 1 and sys.argv[1] == "qbench":
        variant = sys.argv[2] if len(sys.argv) > 2 else None
        qbench(variant)
    elif len(sys.argv) > 1 and sys.argv[1] == "io":
        print(json.dumps(io_stats()))
    else:
        # When run standalone, use QUERY_MARKER from env if set.
        filter_sql = os.environ.get("QUERY_MARKER")
        print(json.dumps({"server_requests": server_requests(filter_sql)}))