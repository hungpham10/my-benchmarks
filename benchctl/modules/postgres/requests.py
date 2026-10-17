"""modules/postgres/requests.py — server_requests(): what did the POSTGRES server handle?

Reads pg_stat_statements directly via psycopg. The driver calls it before
and after the k6 leg; the delta is what k6 caused (healthchecks + Alloy
scrapes also hit the server, counting them as bench traffic would be dishonest).

The query text filter in check_server_requests (verify/check.py) uses
the module's `query_marker` (set to the rendered SQL from the scenario),
so we only count queries matching that marker, not every statement.
"""
import json
import os
import sys

import psycopg

SUT_URL = os.environ.get("SUT_URL", "http://localhost:19090")
# The driver sets PG_DSN and PG_PORT; fall back to defaults.
PG_DSN = os.environ.get(
    "PG_DSN",
    "postgres://bench:bench@postgres:5432/bench?sslmode=disable",
)
PG_PORT = os.environ.get("PG_PORT", "15432")


def _rewrite_dsn(dsn: str, port: str) -> str:
    """Rewrite DSN to use localhost:mapped_port instead of container hostname."""
    from urllib.parse import urlparse, urlunparse
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


if __name__ == "__main__":
    # When run standalone, just print the total count (no filter).
    print(json.dumps({"server_requests": server_requests()}))