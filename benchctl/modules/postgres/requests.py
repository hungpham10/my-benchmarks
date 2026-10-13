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

import psycopg

SUT_URL = os.environ.get("SUT_URL", "http://localhost:19090")
# The driver sets PG_DSN; fall back to a compose-service name.
PG_DSN = os.environ.get(
    "PG_DSN",
    "postgres://bench:bench@postgres:5432/bench?sslmode=disable",
)


def server_requests(filter_sql=None):
    """Count queries in pg_stat_statements.

    filter_sql: SQL fragment to narrow the count (e.g.
    "query LIKE '%/api/v1/query%'" ). When None we count *all* statements
    that pg_stat_statements has tracked; the caller (verify) usually passes
    a filter so we don't count VACUUM, PL/pgSQL setup, etc.
    """
    conn = psycopg.connect(PG_DSN, autocommit=True)
    cur = conn.cursor()

    if filter_sql:
        cur.execute(
            "SELECT count(*) FROM pg_stat_statements WHERE " + filter_sql,
        )
    else:
        cur.execute("SELECT count(*) FROM pg_stat_statements")

    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


if __name__ == "__main__":
    # When run standalone, just print the total count (no filter).
    print(json.dumps({"server_requests": server_requests()}))