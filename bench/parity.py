"""Prove both targets stored the same data before any number is published.

Compares count, sum, min and max for one metric across Prometheus and both
PostgreSQL schemas. Sums are floats accumulated over millions of rows in a
different order on each side, so they are compared with a relative tolerance;
the count is compared exactly.

This reads each system through its own query API rather than through
`promtool tsdb dump-openmetrics`. promtool would also work -- `dumpTSDBData`
opens the DB read-only and replays the WAL into a sandbox, so head data is
visible even before compaction -- but it formats values with `%g`
(`cmd/promtool/tsdb.go`), which makes exact comparison impossible and only
adds a parser to the harness.

What this cannot catch: a wrong `PARTITION BY` or a bad join in a query over
many series. Both systems would agree on the stored rows while the derived
figure is wrong. Those bugs live in bench/queries.py and are argued from the
PromQL semantics, not measured here.
"""

import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Inside the container the code lives at /work and the results are bind-mounted
# at /results, so the RESULTS_DIR the compose file sets must win over the
# source-tree default.
RESULTS = os.environ.get("RESULTS_DIR", os.path.join(ROOT, "results"))
PROM = os.environ.get("PROM_URL", "http://localhost:9090")
PG_DSN = (
    "host=%s port=%s user=%s password=%s dbname=%s"
    % (
        os.environ.get("PG_HOST", "localhost"),
        os.environ.get("PG_PORT", "5432"),
        os.environ.get("PG_USER", "bench"),
        os.environ.get("PG_PASSWORD", "bench"),
        os.environ.get("PG_DATABASE", "bench"),
    )
)

METRIC = "queue_depth"
REL_TOLERANCE = 1e-9


def log(message):
    print("[parity] %s" % message, flush=True)


def prom_query(expr, eval_at_s):
    """Instant query pinned to the end of the data window.

    Two ways this goes wrong silently:

    * The instant endpoint takes `time`, not `start`/`end`. Passing those makes
      the query evaluate at `now`; the dataset is future-dated, so that returns
      nothing -- a zero that reads like an empty Prometheus.
    * A `[Ns]` range selector is left-open at the lower bound, and the samples
      span (span - interval). Ask for exactly `span` and the first sample of
      every series falls outside. Callers add one interval of slack.
    """
    params = urllib.parse.urlencode({"query": expr, "time": eval_at_s})
    url = "%s/api/v1/query?%s" % (PROM, params)
    with urllib.request.urlopen(url, timeout=300) as response:
        payload = json.loads(response.read())
    if payload.get("status") != "success":
        raise RuntimeError(payload.get("error"))
    result = payload["data"]["result"]
    return float(result[0]["value"][1]) if result else 0.0


def postgres_agg(conn, table, where):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), coalesce(sum(value),0), coalesce(min(value),0), "
            "coalesce(max(value),0) FROM %s WHERE %s" % (table, where)
        )
        row = cur.fetchone()
    return {"count": int(row[0]), "sum": float(row[1]),
            "min": float(row[2]), "max": float(row[3])}


def close(a, b, tol=REL_TOLERANCE):
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= tol * scale


def main():
    with open(os.path.join(RESULTS, "dataset.json")) as fh:
        manifest = json.load(fh)
    start_s = manifest["t_start_ms"] / 1000.0
    end_s = manifest["t_end_ms"] / 1000.0
    window = int(end_s - start_s)

    # One interval of slack, see prom_query.
    lookback = window + int(manifest.get("interval_seconds", 15)) + 1
    selector = '{__name__="%s"}' % METRIC

    # The inner *_over_time runs per series; the outer function combines series.
    # They are not the same function: the global count is sum(count_over_time).
    def agg(outer, inner):
        expr = "%s(%s_over_time(%s[%ds]))" % (outer, inner, selector, lookback)
        return prom_query(expr, end_s)

    prom = {"count": agg("sum", "count"), "sum": agg("sum", "sum"),
            "min": agg("min", "min"), "max": agg("max", "max")}

    import psycopg

    checks = {"prometheus": prom}
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        checks["metrics_jsonb"] = postgres_agg(
            conn, "metrics_jsonb", "labels @> '{\"__name__\":\"%s\"}'" % METRIC)
        checks["metrics_norm_brin"] = postgres_agg(
            conn,
            "metrics_norm_brin",
            "series_id IN (SELECT id FROM series_dim WHERE metric = '%s')" % METRIC,
        )

    failures = []
    for backend, got in checks.items():
        if got["count"] != int(prom["count"]):
            failures.append("%s count %s != %s" % (backend, got["count"], int(prom["count"])))
        for field in ("sum", "min", "max"):
            if not close(got[field], prom[field]):
                failures.append("%s %s %r != %r" % (backend, field, got[field], prom[field]))

    payload = {
        "metric": METRIC,
        "expected_samples": int(manifest.get("series_gauge", 0) * manifest.get("samples", 0)),
        "prometheus": prom,
        "postgres": {k: v for k, v in checks.items() if k != "prometheus"},
        "passed": not failures,
        "failures": failures,
    }
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "parity.json"), "w") as fh:
        json.dump(payload, fh, indent=1)

    for backend, got in checks.items():
        log("%-18s count=%-10d sum=%.6f" % (backend, got["count"], got["sum"]))
    if failures:
        log("PARITY FAILED: %s" % "; ".join(failures))
    else:
        log("parity PASS")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
