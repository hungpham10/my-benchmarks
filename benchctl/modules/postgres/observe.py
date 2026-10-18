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
    - samples: count of all http_requests_total rows
    - sum: sum of LAST value per series (matching Prometheus's sum(http_requests_total))
    """
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
        # Sum of last value per series_id
        cur.execute(f"""
            SELECT sum(last_val) FROM (
                SELECT DISTINCT ON (series_id) value AS last_val
                FROM {table}
                ORDER BY series_id, time DESC
            ) sub
        """)
        sum_val = float(cur.fetchone()[0] or 0.0)
    else:
        table = "metrics_jsonb"
        # Count all rows
        cur.execute(f"SELECT count(*) FROM {table}")
        count = int(cur.fetchone()[0] or 0)
        # Sum of last value per labels (series)
        cur.execute(f"""
            SELECT sum(last_val) FROM (
                SELECT DISTINCT ON (labels) value AS last_val
                FROM {table}
                ORDER BY labels, time DESC
            ) sub
        """)
        sum_val = float(cur.fetchone()[0] or 0.0)

    cur.close()
    conn.close()

    return {"samples": count, "sum": sum_val}


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

    dsn = _rewrite_dsn(PG_DSN, PG_PORT)
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        _, t_start_ms, t_end_ms = _read_dataset()

        def exec_fn(name, sql):
            cur = conn.cursor()
            cur.execute(sql)
            cur.fetchall()  # consume results so timing includes data transfer
            cur.close()

        results = run_all(exec_fn, t_start_ms, t_end_ms, variant)
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
    else:
        # When run standalone, use QUERY_MARKER from env if set.
        filter_sql = os.environ.get("QUERY_MARKER")
        print(json.dumps({"server_requests": server_requests(filter_sql)}))