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
import time
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

    # modshard: physical layout differs, but the read contract is the same
    # VIEW (time, series_id, value) over the UNION of all shards.
    # userdb: the read contract is the users_all view (A/E: single table;
    # B/C: UNION ALL of the physical shards). sample count == N_USERS.
    if variant == "userdb":
        # No series_dim in this schema; parity is the row count itself.
        cur.execute("SELECT count(*) FROM users_all")
        count = int(cur.fetchone()[0] or 0)
        cur.close()
        conn.close()
        return {"samples": count, "sum": float(count)}

    # idxlab: parity is the docs row count itself (no sum side).
    if variant == "idxlab":
        cur.execute("SELECT count(*) FROM docs")
        count = int(cur.fetchone()[0] or 0)
        cur.close()
        conn.close()
        return {"samples": count, "sum": float(count)}

    if variant in ("norm", "sharded", "modshard"):
        table = ("metrics_norm_brin" if variant == "norm"
                 else "metrics_sharded" if variant == "sharded"
                 else "events_all")
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


# Read shapes for the filesystem matrix.
#
# The q1-q5 suite cannot separate filesystems: q1 is a window function over
# 3.6M rows and costs CPU, not I/O. These shapes are chosen for what they
# make the storage layer do — a sequential scan of the whole table, one
# metric's rows through the label index, and a single series (index lookup
# plus a heap fetch). Each is a different mix of sequential reads, metadata
# reads and random reads, which is exactly where filesystems differ.
READ_SHAPES = {
    "jsonb": {
        "seq_scan": """
            SELECT count(*), sum(value) FROM metrics_jsonb
            WHERE time BETWEEN %s::timestamptz AND %s::timestamptz""",
        "one_metric": """
            SELECT count(*), sum(value) FROM metrics_jsonb
            WHERE time BETWEEN %s::timestamptz AND %s::timestamptz
              AND labels @> '{"__name__":"queue_depth"}'""",
        "one_series": """
            SELECT count(*), sum(value) FROM metrics_jsonb
            WHERE time BETWEEN %s::timestamptz AND %s::timestamptz
              AND labels @> '{"__name__":"queue_depth","instance":"host-0000"}'""",
        # A median over every sample of one metric. The sort cannot be
        # optimised away, and at work_mem=8MB it spills to a temp file —
        # which PostgreSQL puts in PGDATA, on the filesystem under test.
        # This is the shape where the filesystem shows up in the read path.
        "temp_sort": """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY value) FROM metrics_jsonb
            WHERE time BETWEEN %s::timestamptz AND %s::timestamptz
              AND labels @> '{"__name__":"queue_depth"}'""",
    },
    "norm": {
        "seq_scan": """
            SELECT count(*), sum(m.value) FROM metrics_norm_brin m
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz""",
        "one_metric": """
            SELECT count(*), sum(m.value) FROM metrics_norm_brin m
            JOIN series_dim s ON s.id = m.series_id
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
              AND s.metric = 'queue_depth'""",
        "one_series": """
            SELECT count(*), sum(m.value) FROM metrics_norm_brin m
            JOIN series_dim s ON s.id = m.series_id
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
              AND s.metric = 'queue_depth' AND s.instance = 'host-0000'""",
        # A median over every sample of one metric: the sort spills to a
        # temp file in PGDATA, on the filesystem under test.
        "temp_sort": """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY m.value)
            FROM metrics_norm_brin m
            JOIN series_dim s ON s.id = m.series_id
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
              AND s.metric = 'queue_depth'""",
    },
    "idxlab": {
        # Index-lab read shapes: each one is the case an access method
        # claims as its own. All literals are fixed (no time-window
        # substitution), so the plan stays comparable across legs.
        #  - range_time: a 7-day window over a heap whose created_at is
        #    insertion-ordered — BRIN's advertised case.
        #  - eq_rating: ~100 rows of 10M — hash's only case.
        #  - knn_point: ORDER BY <-> — GiST/SP-GiST only.
        #  - prefix_name: LIKE 'User 1234%' — SP-GiST text vs nothing
        #    (no btree(name) outside the btree_name leg).
        #  - trgm_like: LIKE '%12345%' — pg_trgm GIN, unreachable to
        #    the other index types.
        "range_time": """
            SELECT count(*) FROM docs
            WHERE created_at BETWEEN '2025-06-01'::timestamptz
                                 AND '2025-06-08'::timestamptz""",
        "eq_rating": "SELECT count(*) FROM docs WHERE rating = 50000",
        "knn_point": """
            SELECT id FROM docs
            ORDER BY loc <-> point(1234, 5678)
            LIMIT 20""",
        "prefix_name": "SELECT id FROM docs WHERE name LIKE 'User 1234%' LIMIT 100",
        "trgm_like": "SELECT id FROM docs WHERE name LIKE '%12345%' LIMIT 100",
    },
    "userdb": {
        # Same three read shapes, on the user schema: point logins (the
        # unique-email index), point fetches (the PK), and a created-at
        # range. B/C pay the UNION ALL of users_all here — that is the
        # app-side read-path cost being measured.
        "login": """
            SELECT user_id, name FROM users_all
            WHERE email = 'user5000000@example.com'::citext
            LIMIT 1""",
        "by_id": """
            SELECT user_id, email, created_at FROM users_all
            WHERE user_id = 5000000
            LIMIT 1""",
        # Half the table (seed spans 730 days): big enough that the plan
        # is a real scan, not a 13k-row index lookup. sum(user_id) forces
        # heap traffic; count(*) is served by an index-only scan on
        # created_at and shows no parallel gain.
        # pg-readscan-* uses this shape to compare connection count
        # against parallel_workers_per_gather.
        "recent": """
            SELECT sum(user_id) FROM users_all
            WHERE created_at > now() - interval '365 days'""",
    },
    "modshard": {
        # Same shapes as norm, but over the events_all view: the UNION ALL
        # of every physical shard in the leg's layout. For layout B the
        # view text itself carries the N-table UNION cost — an honest part
        # of the app-side sharding read path.
        "seq_scan": """
            SELECT count(*), sum(m.value) FROM events_all m
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz""",
        "one_metric": """
            SELECT count(*), sum(m.value) FROM events_all m
            JOIN series_dim s ON s.id = m.series_id
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
              AND s.metric = 'queue_depth'""",
        "one_series": """
            SELECT count(*), sum(m.value) FROM events_all m
            JOIN series_dim s ON s.id = m.series_id
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
              AND s.metric = 'queue_depth' AND s.instance = 'host-0000'""",
        # A median over every sample of one metric: the sort spills to a
        # temp file in PGDATA, on the filesystem under test.
        "temp_sort": """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY m.value)
            FROM events_all m
            JOIN series_dim s ON s.id = m.series_id
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
              AND s.metric = 'queue_depth'""",
    },
    "sharded": {
        # The scan that answers the sharding question, and the same scan
        # without sum(float8).
        #
        # sum(double precision) is not a partitionwise-compatible
        # aggregate: PostgreSQL cannot add up partial sums of a float
        # and get the same answer as summing the whole, so one float
        # aggregate disables the partitionwise aggregate for the whole
        # query. That in turn removes the Parallel Append, and the
        # partitions end up scanned one after another by the leader.
        #
        # count(*) has no such problem, so it gets the plan this
        # scenario is actually looking for: a Parallel Append over the
        # shards, one worker per partition. Keeping both shapes is the
        # point — the difference between them is the finding.
        "shard_scan": """
            SELECT count(*) FROM metrics_sharded m
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz""",
        "seq_scan": """
            SELECT count(*), sum(m.value) FROM metrics_sharded m
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz""",
        "one_metric": """
            SELECT count(*), sum(m.value) FROM metrics_sharded m
            JOIN series_dim s ON s.id = m.series_id
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
              AND s.metric = 'queue_depth'""",
        "one_series": """
            SELECT count(*), sum(m.value) FROM metrics_sharded m
            JOIN series_dim s ON s.id = m.series_id
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
              AND s.metric = 'queue_depth' AND s.instance = 'host-0000'""",
        "temp_sort": """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY m.value)
            FROM metrics_sharded m
            JOIN series_dim s ON s.id = m.series_id
            WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
              AND s.metric = 'queue_depth'""",
    },
}


def _plan_summary(plan):
    """The part of an EXPLAIN plan that says how the work was spread.

    Node types, the workers planned and the workers launched. A query that
    says "Parallel Append, 3 workers" and a query that says "Seq Scan" are
    different measurements even when they take the same time.
    """
    nodes = []
    workers_planned = 0
    workers_launched = 0

    def walk(node):
        nonlocal workers_planned, workers_launched
        nodes.append(node.get("Node Type"))
        if "Workers Planned" in node:
            workers_planned = max(workers_planned, node["Workers Planned"])
        if "Workers Launched" in node:
            workers_launched = max(workers_launched, node["Workers Launched"])
        for child in node.get("Plans", []):
            walk(child)

    walk(plan)
    return {
        "nodes": nodes,
        "workers_planned": workers_planned,
        "workers_launched": workers_launched,
        "parallel_append": "Parallel Append" in nodes,
    }


def _explain(conn, sql):
    """EXPLAIN ANALYZE the query and summarise the plan.

    ANALYZE, not plain EXPLAIN: the number of workers that actually
    launched only exists in an executed plan. Without it, "Workers
    Launched" is missing and every plan looks like it ran on one core.
    """
    cur = conn.cursor()
    cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql)
    plan = cur.fetchone()[0][0]["Plan"]
    cur.close()
    return _plan_summary(plan)


def _setup_parallel(conn):
    """Turn on parallel workers for this session, if the scenario asks.

    The module runs with max_parallel_workers_per_gather=0 so that a
    filesystem comparison is not measured through a scheduler lottery; the
    sharding scenario is the opposite case and needs the workers, so it
    asks for them.

    The costs are zeroed too. PostgreSQL's planner will not choose a
    parallel plan for a query with a window function and a join on its
    own, and "the planner did not pick it" is a different experiment from
    "how well does it scale when it does".
    """
    workers = os.environ.get("BENCHCTL_PARALLEL_WORKERS", "").strip()
    if not workers:
        return ""
    n = max(1, int(workers))
    cur = conn.cursor()
    cur.execute(f"SET max_parallel_workers_per_gather = {n}")
    cur.execute(f"SET max_parallel_workers = {max(n, 8)}")
    cur.execute("SET min_parallel_table_scan_size = 0")
    cur.execute("SET min_parallel_index_scan_size = 0")
    cur.execute("SET parallel_setup_cost = 0")
    cur.execute("SET parallel_tuple_cost = 0")
    # The default is on, but the sharding scenario depends on it, so it
    # is asked for explicitly: without the partitionwise aggregate the
    # planner has no Parallel Append to offer.
    cur.execute("SET enable_partitionwise_aggregate = on")
    cur.close()
    return str(n)


def readshapes(variant=None):
    """Measure the read shapes, warm or cold.

    Warm (default): a few iterations per shape, the first one pays for the
    cache miss and the rest read from it.

    Cold (BENCHCTL_READSHAPES_COLD=1): exactly one iteration, no warmup.
    The caller runs this right after restarting the container and dropping
    the page cache, so the measurement is the disk's, not the cache's.
    """
    import sys
    import time
    sys.path.insert(0, SHARED)
    from querybench import render_sql

    if variant is None:
        variant = os.environ.get("VARIANT", "jsonb")
    if variant not in READ_SHAPES:
        raise RuntimeError(f"unknown postgres variant: {variant!r} "
                           f"(expected jsonb, norm, sharded or modshard)")

    cold = os.environ.get("BENCHCTL_READSHAPES_COLD", "") == "1"
    iterations = 1 if cold else int(os.environ.get("BENCHCTL_READSHAPES_ITER", "5"))
    warmup = 0 if cold else 1

    dsn = _rewrite_dsn(PG_DSN, PG_PORT)
    conn = psycopg.connect(dsn, autocommit=True)
    out = {}
    try:
        _setup_parallel(conn)
        _, t_start_ms, t_end_ms = _read_dataset()
        for name, sql in READ_SHAPES[variant].items():
            text = render_sql(sql, t_start_ms, t_end_ms)
            for _ in range(warmup):
                cur = conn.cursor()
                cur.execute(text)
                cur.fetchall()
                cur.close()
            times = []
            rows = 0
            for _ in range(iterations):
                cur = conn.cursor()
                t0 = time.perf_counter()
                cur.execute(text)
                res = cur.fetchall()
                times.append((time.perf_counter() - t0) * 1000.0)
                rows = len(res)
                cur.close()
            times.sort()
            entry = {
                "n": len(times),
                "cold": cold,
                "rows": rows,
                "p50_ms": round(times[len(times) // 2], 2),
                "min_ms": round(times[0], 2),
                "max_ms": round(times[-1], 2),
            }
            # The plan of the full-table scans: this is the shape that
            # says whether the shards were read in parallel or one
            # after another. Both scans are captured — the one with
            # sum(float8) and the one without it — because the
            # difference between their plans is the result.
            if name in ("seq_scan", "shard_scan") and \
                    os.environ.get("BENCHCTL_PARALLEL_WORKERS"):
                try:
                    entry["plan"] = _explain(conn, text)
                except Exception as e:  # noqa: BLE001 — diagnostic, never fatal
                    entry["plan_error"] = str(e)
            out[name] = entry
    finally:
        conn.close()
    print(json.dumps(out))


def qbench(variant=None):
    """Measure latency for each query in shared/queries.py via psycopg SQL.

    Runs every q1-q5 SQL against the appropriate table (metrics_jsonb or
    metrics_norm_brin) with the benchmark time window substituted. Returns
    JSON dict keyed by query name with p50/p95/max ms plus iteration count.
    """
    import sys
    sys.path.insert(0, SHARED)
    from querybench import run_all, render_sql
    import queries as Q

    if variant is None:
        variant = os.environ.get("VARIANT", "jsonb")

    # variant is "jsonb" | "norm" | "sharded" from driver (split_system),
    # but run_all() looks up shared/queries.py by backend key.
    if variant in ("userdb", "idxlab"):
        # These schemas have no metrics catalog: run the bespoke read
        # shapes instead. userdb additionally times the ROUTED forms —
        # the same point lookups against the one shard table the
        # application's modulo routing would touch. That gap is the
        # "does sharding help reads" answer. idxlab's shapes are the
        # access-method claim cases (range, equality, kNN, LIKE).
        dsn = _rewrite_dsn(PG_DSN, PG_PORT)
        conn = psycopg.connect(dsn, autocommit=True)
        measure = int(os.environ.get("QBENCH_MEASURE", "30"))

        def timed(sql):
            cur = conn.cursor()
            samples = []
            try:
                for _ in range(measure):
                    t0 = time.perf_counter()
                    cur.execute(sql)
                    cur.fetchall()
                    samples.append((time.perf_counter() - t0) * 1000.0)
            finally:
                cur.close()
            samples.sort()
            p = lambda q: round(samples[min(len(samples) - 1,
                                            int(len(samples) * q))], 2)
            return {"p50_ms": p(0.5), "p95_ms": p(0.95),
                    "max_ms": round(samples[-1], 2)}

        layout = (os.environ.get("LAYOUT")
                  or os.environ.get("SHARD_LAYOUT") or "")
        pools = int(os.environ.get("POOLS") or 0)
        shards = int(os.environ.get("SHARDS")
                     or os.environ.get("SHARD_N") or 0)
        n_users = int(os.environ.get("N_USERS") or "10000000")
        try:
            results = {}
            for name, sql in READ_SHAPES[variant].items():
                results[name] = timed(sql)

            # Routed target: mirror step.js's routing exactly. C and
            # POOLS legs are pool-per-schema (p = pools or shards); B is
            # table-suffix with SHARDS tables. idxlab has no routing.
            routed_target = None
            if variant == "userdb":
                if layout == "C" or pools > 0:
                    routed_target = ("s{0:02d}.users", pools or shards or 32)
                elif layout == "B":
                    routed_target = ("users_{0:02d}", shards or 0)
            if routed_target:
                fmt, modn = routed_target
                uid = 1 + (n_users // 2)  # a mid-population user
                table = fmt.format(uid % modn)
                results["routed_target"] = table
                results["routed_login"] = timed(
                    "SELECT user_id, name FROM " + table
                    + " WHERE email = 'user" + str(uid) + "@example.com' LIMIT 1")
                results["routed_by_id"] = timed(
                    "SELECT user_id, email, created_at FROM " + table
                    + " WHERE user_id = " + str(uid) + " LIMIT 1")
        finally:
            conn.close()
        print(json.dumps(results))
        return

    VARIANT_TO_BACKEND = {"jsonb": "metrics_jsonb",
                           "norm": "metrics_norm_brin",
                           "sharded": "metrics_sharded",
                           "modshard": "events_all"}
    if variant not in VARIANT_TO_BACKEND:
        raise RuntimeError(f"unknown postgres variant: {variant!r} (expected jsonb, norm, sharded, modshard or userdb)")
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

        workers = _setup_parallel(conn)

        results = run_all(exec_fn, t_start_ms, t_end_ms, backend,
                          measure=int(os.environ.get("QBENCH_MEASURE", "30")),
                          max_budget_s=int(os.environ.get("QBENCH_BUDGET_S", "120")),
                          warmup=int(os.environ.get("QBENCH_WARMUP", "3")))

        # The plan of the heaviest query, so the report can prove what the
        # executor did: a Parallel Append with three workers is a different
        # measurement from a serial scan, and the number has to be in the
        # record rather than in a comment.
        if workers:
            q1 = next((q for q in Q.QUERIES if q["name"] == "q1_counter_rate"), None)
            if q1:
                sql = q1["sql"].get(backend) or \
                    q1["sql"]["metrics_norm_brin"].replace(
                        "metrics_norm_brin", backend)
                sql = render_sql(sql, t_start_ms, t_end_ms)
                results["q1_counter_rate"]["plan"] = _explain(conn, sql)
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
    elif len(sys.argv) > 1 and sys.argv[1] == "readshapes":
        variant = sys.argv[2] if len(sys.argv) > 2 else None
        readshapes(variant)
    elif len(sys.argv) > 1 and sys.argv[1] == "io":
        print(json.dumps(io_stats()))
    else:
        # When run standalone, use QUERY_MARKER from env if set.
        filter_sql = os.environ.get("QUERY_MARKER")
        print(json.dumps({"server_requests": server_requests(filter_sql)}))