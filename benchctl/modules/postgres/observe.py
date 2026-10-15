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
    """
    dsn = _rewrite_dsn(PG_DSN, PG_PORT)
    conn = psycopg.connect(dsn, autocommit=True)
    cur = conn.cursor()

    if variant is None:
        variant = os.environ.get("VARIANT", "jsonb")

    table = "metrics_norm_brin" if variant == "norm" else "metrics_jsonb"

    cur.execute(f"SELECT count(*), sum(value) FROM {table}")
    count, sum_val = cur.fetchone()
    sum_val = float(sum_val) if sum_val else 0.0
    count = int(count) if count else 0

    cur.close()
    conn.close()

    return {"samples": count, "sum": sum_val}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        variant = os.environ.get("VARIANT", "jsonb")
        if len(sys.argv) > 2:
            variant = sys.argv[2]
        print(json.dumps(sample_stats(variant)))
    else:
        # When run standalone, use QUERY_MARKER from env if set.
        filter_sql = os.environ.get("QUERY_MARKER")
        print(json.dumps({"server_requests": server_requests(filter_sql)}))