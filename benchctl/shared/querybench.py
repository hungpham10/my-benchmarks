"""benchctl/shared/querybench.py — measure per-query latency for q1-q5.

Library with bench_once() and run_all(). Backend-specific observe modules
provide the exec_fn (name, text) -> None and read t_start/t_end from
dataset.json. Each query gets warmup iterations (discarded) then measured
iterations; returns p50/p95/max ms plus iteration count and error flag.

Usage (library):
    from querybench import run_all, ts_literal, render_sql
    results = run_all(exec_fn, t_start_ms, t_end_ms, backend="prometheus")
"""
import os
import statistics
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import queries as Q


def ts_literal(ms):
    """Quoted ISO8601 timestamptz literal for SQL."""
    return "'" + datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat() + "'"


def render_sql(sql, t_start_ms, t_end_ms):
    """Substitute the two %s placeholders with timestamptz literals."""
    out = sql.replace("%s", ts_literal(t_start_ms), 1).replace("%s", ts_literal(t_end_ms), 1)
    assert "%s" not in out, "unrendered placeholder in SQL"
    return out


def bench_once(exec_fn, warmup=3, measure=30, max_budget_s=120):
    """Run exec_fn warmup times (discard), then measure up to `measure` times.

    Stops early if cumulative time exceeds max_budget_s (protects against
    very slow queries like JSONB containment scanning millions of rows).
    Returns dict with n, errors, p50_ms, p95_ms, max_ms, error, rows.
    exec_fn returns number of rows matched (used to detect empty result sets).
    """
    for _ in range(warmup):
        try:
            exec_fn()
        except Exception:
            pass

    times = []
    errors = 0
    last_rows = 0
    deadline = time.perf_counter() + max_budget_s
    for i in range(measure):
        if time.perf_counter() > deadline and i > 0:
            break
        t0 = time.perf_counter()
        try:
            last_rows = exec_fn()
        except Exception:
            errors += 1
            continue
        times.append((time.perf_counter() - t0) * 1000.0)

    if not times:
        return {"n": 0, "errors": errors, "p50_ms": None, "p95_ms": None,
                "max_ms": None, "error": "all iterations failed", "rows": 0}

    times.sort()
    p95_idx = max(0, int(len(times) * 0.95) - 1)
    return {
        "n": len(times),
        "errors": errors,
        "p50_ms": round(statistics.median(times), 2),
        "p95_ms": round(times[p95_idx], 2),
        "max_ms": round(max(times), 2),
        "rows": last_rows,
    }


def run_all(exec_fn, t_start_ms, t_end_ms, backend, measure=30, max_budget_s=120):
    """Run every query in Q.QUERIES through exec_fn.

    exec_fn(name, text) -> int rows matched on success, raises on failure.
    backend: "prometheus" | "metrics_jsonb" | "metrics_norm_brin"
    Returns dict keyed by query name with intent, winner_expected, promql,
    and measured stats including "empty" (True when query matched no rows,
    meaning latency numbers are not meaningful).
    """
    out = {}
    for q in Q.QUERIES:
        name = q["name"]
        # q5_short_range intentionally measures a 15-minute slice at the end of
        # the window (the "short range" case), not the full 45-minute window.
        if name == "q5_short_range":
            q_start_ms = max(t_start_ms, t_end_ms - Q.SHORT_RANGE_MS)
        else:
            q_start_ms = t_start_ms
        if backend == "prometheus":
            text = q["promql"]
        else:
            text = render_sql(q["sql"][backend], q_start_ms, t_end_ms)

        def make_exec(text=text):
            def f():
                return exec_fn(name, text)
            return f

        stats = bench_once(make_exec(), measure=measure, max_budget_s=max_budget_s)
        out[name] = {
            "intent": q["intent"],
            "winner_expected": q.get("winner"),
            "promql": q.get("promql"),
            "rows_matched": stats.get("rows"),
            "empty": stats.get("rows") == 0,
            **stats,
        }
    return out
