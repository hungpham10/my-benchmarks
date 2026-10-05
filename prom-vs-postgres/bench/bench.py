"""Query latency: run every shape against every backend and report percentiles.

Replaces the k6 harness. Every query here is a single statement returning a
modest result set, so a serial Python loop measures the same thing k6 would
without an image build, a JS runtime, or a second service.

Measurement rules that make the numbers mean anything:

1. **Read the whole result.** `fetchall()` on the Postgres side and a full body
   read on the Prometheus side. Timing only the dispatch would measure the
   server's "accept the query" latency, which is the cheapest part.
2. **Discard a warmup.** The first executions pay for plan caching, block
   cache misses and cold mmap. They are not what a dashboard query feels like.
3. **Many iterations, because percentiles need a distribution.** At 30 samples
   a P95 is indistinguishable from the maximum. ITERATIONS defaults to 300.
4. **Report the row count** alongside the latency, so a reader can check the two
   backends actually returned comparable work.

Percentiles use the nearest-rank method on the sorted sample list.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import queries as Q  # noqa: E402

PROM_URL = os.environ.get("PROM_URL", "http://prometheus:9090")
PG_DSN = (
    "host=%s port=%s user=%s password=%s dbname=%s"
    % (
        os.environ.get("PG_HOST", "postgres"),
        os.environ.get("PG_PORT", "5432"),
        os.environ.get("PG_USER", "bench"),
        os.environ.get("PG_PASSWORD", "bench"),
        os.environ.get("PG_DATABASE", "bench"),
    )
)

ITERATIONS = int(os.environ.get("QUERY_ITERATIONS", "300"))
WARMUP = int(os.environ.get("QUERY_WARMUP", "20"))
TIMEOUT = int(os.environ.get("QUERY_TIMEOUT_S", "120"))
RESULTS_DIR = os.environ.get("RESULTS_DIR", "/results")


def log(message):
    print("[bench] %s" % message, flush=True)


def percentile(sorted_values, fraction):
    """Nearest-rank percentile. `sorted_values` must already be sorted."""
    if not sorted_values:
        return None
    rank = int(-(-fraction * len(sorted_values) // 1))  # ceil
    index = min(max(rank - 1, 0), len(sorted_values) - 1)
    return sorted_values[index]


def summarize(samples_ms, rows):
    ordered = sorted(samples_ms)
    return {
        "iterations": len(ordered),
        "rows": rows,
        "min_ms": round(ordered[0], 3),
        "p50_ms": round(percentile(ordered, 0.50), 3),
        "p75_ms": round(percentile(ordered, 0.75), 3),
        "p95_ms": round(percentile(ordered, 0.95), 3),
        "p99_ms": round(percentile(ordered, 0.99), 3),
        "max_ms": round(ordered[-1], 3),
        "mean_ms": round(sum(ordered) / len(ordered), 3),
    }


# --- backends -------------------------------------------------------------

def run_prometheus(promql, start_s, end_s, step):
    params = urllib.parse.urlencode(
        {"query": promql, "start": start_s, "end": end_s, "step": step}
    )
    url = "%s/api/v1/query_range?%s" % (PROM_URL, params)
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
        payload = json.loads(response.read())
    if payload.get("status") != "success":
        raise RuntimeError("prometheus error: %s" % payload.get("error"))
    series = payload.get("data", {}).get("result", [])
    return sum(len(s.get("values", [])) for s in series)


def run_postgres(conn, sql, start_s, end_s):
    import datetime

    begin = datetime.datetime.fromtimestamp(start_s, datetime.timezone.utc)
    finish = datetime.datetime.fromtimestamp(end_s, datetime.timezone.utc)
    with conn.cursor() as cur:
        cur.execute(sql, (begin, finish))
        rows = cur.fetchall()
    return len(rows)


# --- driver ---------------------------------------------------------------

def main():
    manifest_path = os.path.join(RESULTS_DIR, "dataset.json")
    with open(manifest_path) as fh:
        manifest = json.load(fh)

    t_start_ms = manifest["t_start_ms"]
    t_end_ms = manifest["t_end_ms"]
    start_s = t_start_ms / 1000.0
    end_s = t_end_ms / 1000.0

    results = []
    conn = None
    try:
        import psycopg

        conn = psycopg.connect(PG_DSN, autocommit=True)
        log("postgres connected")

        for query in Q.QUERIES:
            q_start, q_end = start_s, end_s
            if query["name"] == "q5_short_range":
                q_end = (t_start_ms + Q.SHORT_RANGE_MS) / 1000.0

            targets = [("prometheus", None)]
            targets += [(name, query["sql"][name]) for name in ("metrics_jsonb",
                                                               "metrics_norm_brin")]

            for backend, sql in targets:
                if backend == "prometheus":
                    call = lambda: run_prometheus(  # noqa: E731
                        query["promql"], q_start, q_end, Q.STEP_SECONDS)
                else:
                    call = lambda s=sql: run_postgres(conn, s, q_start, q_end)  # noqa: E731

                for _ in range(WARMUP):
                    call()

                samples = []
                rows = 0
                for _ in range(ITERATIONS):
                    started = time.perf_counter()
                    rows = call()
                    samples.append((time.perf_counter() - started) * 1000.0)

                entry = {"query": query["name"], "backend": backend,
                         "intent": query["intent"], "expected_winner": query["winner"]}
                entry.update(summarize(samples, rows))
                results.append(entry)
                log("%-20s %-18s p50=%8.2fms p95=%8.2fms rows=%d"
                    % (query["name"], backend, entry["p50_ms"], entry["p95_ms"], rows))
    finally:
        if conn is not None:
            conn.close()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out = os.path.join(RESULTS_DIR, "query.json")
    with open(out, "w") as fh:
        json.dump({"iterations": ITERATIONS, "warmup": WARMUP,
                   "step_seconds": Q.STEP_SECONDS, "results": results}, fh, indent=1)
    log("wrote %s" % out)


if __name__ == "__main__":
    main()
