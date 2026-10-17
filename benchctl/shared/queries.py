"""The five query shapes the benchmark runs against every backend.

Each shape exists because it probes a specific claim from the comparison, not
to pad the table. `intent` is carried into the report so a reader can tell a
Prometheus win from a Postgres win without guessing.

Time handling: the dataset is timestamped in the future (Prometheus rejects
samples older than MaxTime - chunkRange/2), so every query is given explicit
`start`/`end` from results/dataset.json. Nothing here may default to now().

`STEP_SECONDS` is shared across backends on purpose. Prometheus `query_range`
returns one point per step, so the step decides the response size; if the SQL
side returned a different number of rows the two latencies would not be
comparable.

Placeholders are `%s`, not `$1`. psycopg3 interpolates client-side and does not
parse PostgreSQL's numbered syntax; passing parameters alongside `$1` fails with
"the query has 0 placeholders but 2 parameters were passed".
"""

STEP_SECONDS = 300

QUERIES = [
    {
        "name": "q1_counter_rate",
        "intent": "Counter rate across every counter series. Prometheus walks an "
                  "already-sorted slice; Postgres needs lag() OVER a window.",
        "winner": "prometheus",
        "promql": "sum(rate(http_requests_total[5m]))",
        "sql": {
            "metrics_jsonb": """
                WITH d AS (
                  SELECT time, value AS v,
                         lag(value) OVER (PARTITION BY labels ORDER BY time) AS prev
                  FROM metrics_jsonb
                  WHERE time BETWEEN %s::timestamptz AND %s::timestamptz
                    AND labels @> '{"__name__":"http_requests_total"}'
                )
                SELECT sum(v - prev + CASE WHEN v < prev THEN prev ELSE 0 END) AS rate
                FROM d WHERE prev IS NOT NULL""",
            "metrics_norm_brin": """
                WITH d AS (
                  SELECT m.time AS time, m.value AS v,
                         lag(m.value) OVER (PARTITION BY m.series_id ORDER BY m.time) AS prev
                  FROM metrics_norm_brin m
                  JOIN series_dim s ON s.id = m.series_id
                  WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
                    AND s.metric = 'http_requests_total'
                )
                SELECT sum(v - prev + CASE WHEN v < prev THEN prev ELSE 0 END) AS rate
                FROM d WHERE prev IS NOT NULL""",
        },
    },
    {
        "name": "q2_sum_by_job",
        "intent": "Grouped aggregation across many series. The plain case, where "
                  "both engines are comfortable.",
        "winner": "even",
        "promql": "sum by (job) (queue_depth)",
        "sql": {
            "metrics_jsonb": """
                SELECT labels->>'job' AS job, avg(value) AS v
                FROM metrics_jsonb
                WHERE time BETWEEN %s::timestamptz AND %s::timestamptz
                  AND labels @> '{"__name__":"queue_depth"}'
                GROUP BY 1 ORDER BY 1""",
            "metrics_norm_brin": """
                SELECT s.job AS job, avg(m.value) AS v
                FROM metrics_norm_brin m
                JOIN series_dim s ON s.id = m.series_id
                WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
                  AND s.metric = 'queue_depth'
                GROUP BY 1 ORDER BY 1""",
        },
    },
    {
        "name": "q3_p90",
        "intent": "Per-series p90 over a sliding hour. Postgres sorts in the "
                  "database and has no sample ceiling; quantile_over_time sorts "
                  "in Prometheus RAM and is capped by --query.max-samples.",
        "winner": "postgres",
        "promql": "quantile_over_time(0.9, queue_depth{job=\"job-00\"}[1h])",
        "sql": {
            "metrics_jsonb": """
                SELECT labels->>'instance' AS instance,
                       percentile_cont(0.9) WITHIN GROUP (ORDER BY value) AS p90
                FROM metrics_jsonb
                WHERE time BETWEEN %s::timestamptz AND %s::timestamptz
                  AND labels @> '{"__name__":"queue_depth","job":"job-00"}'
                GROUP BY 1 ORDER BY 1""",
            "metrics_norm_brin": """
                SELECT s.instance AS instance,
                       percentile_cont(0.9) WITHIN GROUP (ORDER BY m.value) AS p90
                FROM metrics_norm_brin m
                JOIN series_dim s ON s.id = m.series_id
                WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
                  AND s.metric = 'queue_depth' AND s.job = 'job-00'
                GROUP BY 1 ORDER BY 1""",
        },
    },
    {
        "name": "q4_selective_wide",
        "intent": "One job selected across the whole retention window. Prometheus "
                  "resolves it through postings and reads only those series; "
                  "Postgres must consult the index and still fetch the heap.",
        "winner": "prometheus",
        "promql": "sum(queue_depth{job=\"job-00\"})",
        "sql": {
            "metrics_jsonb": """
                SELECT avg(value) AS v
                FROM metrics_jsonb
                WHERE time BETWEEN %s::timestamptz AND %s::timestamptz
                  AND labels @> '{"__name__":"queue_depth","job":"job-00"}'""",
            "metrics_norm_brin": """
                SELECT avg(m.value) AS v
                FROM metrics_norm_brin m
                JOIN series_dim s ON s.id = m.series_id
                WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
                  AND s.metric = 'queue_depth' AND s.job = 'job-00'""",
        },
    },
    {
        "name": "q5_short_range",
        "intent": "A single series over 15 minutes. One block, few rows, good "
                  "planner: the case where Postgres is genuinely competitive.",
        "winner": "postgres",
        "promql": "queue_depth{instance=\"host-0000\"}",
        "sql": {
            "metrics_jsonb": """
                SELECT avg(value) AS v
                FROM metrics_jsonb
                WHERE time BETWEEN %s::timestamptz AND %s::timestamptz
                  AND labels @> '{"__name__":"queue_depth","instance":"host-0000"}'""",
            "metrics_norm_brin": """
                SELECT avg(m.value) AS v
                FROM metrics_norm_brin m
                JOIN series_dim s ON s.id = m.series_id
                WHERE m.time BETWEEN %s::timestamptz AND %s::timestamptz
                  AND s.metric = 'queue_depth' AND s.instance = 'host-0000'""",
        },
    },
]

# q5 deliberately overrides the window to 15 minutes so it measures the short
# range case rather than repeating q4.
SHORT_RANGE_MS = 15 * 60 * 1000

BACKENDS = ["prometheus", "metrics_jsonb", "metrics_norm_brin"]

LABELS = {
    "prometheus": "Prometheus",
    # "postgres" is the ingest target, which loads both schemas at once.
    "postgres": "PostgreSQL (both schemas)",
    "metrics_jsonb": "Postgres jsonb",
    "metrics_norm_brin": "Postgres norm+BRIN",
}
