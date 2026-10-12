/* One run that walks the load upward, not one run per load level.
 *
 * The previous design started a separate k6 process for each VU level. That
 * broke the metric it existed to produce: prom_requests and pg_requests are
 * cumulative counters, a counter reset by a new process is indistinguishable
 * from a restart, and Prometheus reads the reset as a spike. The recorded
 * series went 331111 -> 199974 -> 402651, a counter going backwards, and rate()
 * over that is not a throughput.
 *
 * One process makes the counters monotonic, so rate() is real, so a dashboard
 * can show throughput against load against latency in one graph -- which is the
 * only view where saturation is visible. The trade is honest and it costs
 * something: the k6 summary no longer contains a p95 per VU level, because k6
 * cannot attribute a sample to the stage that produced it. Per-stage numbers
 * stay available by setting SATURATION_MODE=steps, which runs the old
 * one-process-per-level sequence and is what the blog's exact figures come from.
 *
 * Both arms always ramp through the same stages, at the same moment: a
 * saturated system next to an idle one measures nothing.
 *
 * queries.json is written by bench/run.py from bench/queries.py.
 */

import http from "k6/http";
// sql.open takes the driver's default export -- a Symbol, not a string.
import postgres from "k6/x/sql/driver/postgres";
import { Trend, Counter } from "k6/metrics";
import sqlmod from "k6/x/sql";

const PROM_URL = __ENV.PROM_URL || "http://prometheus:9090";
const PG_DSN =
  __ENV.PG_DSN ||
  "postgres://bench:bench@postgres:5432/bench?sslmode=disable";
const BACKEND = __ENV.BACKEND || "prometheus";
const QUERY_NAME = __ENV.QUERY_NAME || "q1_counter_rate";
const RUN_ID = __ENV.BENCH_RUN_ID || "";

// "1:30s" per rung. Ramp rather than one constant-VU run, so the point where
// throughput stops answering to load sits inside the data instead of being
// inferred from separate runs.
const STAGES = (__ENV.SATURATION_STAGES ||
                "1:30s,2:30s,4:30s,8:30s,16:30s,32:30s")
  .split(",")
  .map((chunk) => {
    const [vus, duration] = chunk.split(":");
    // `target`, not the older `targetVUs`: k6 v2 rejects the old name with
// json: unknown field "targetVUs" at load time, before any iteration runs.
  return { target: parseInt(vus, 10), duration: duration.trim() };
  });

const manifest = JSON.parse(open("/results/queries.json"));
const query = manifest.queries.find((q) => q.name === QUERY_NAME);
if (!query) {
  throw new Error(`unknown QUERY_NAME ${QUERY_NAME}`);
}

// --- per-target metrics -----------------------------------------------------

// Cumulative counters, and monotonic because this is one process. That is what
// makes rate() a throughput rather than an artefact.
const promRequests = new Counter("prom_requests");
const pgRequests = new Counter("pg_requests");
const promDuration = new Trend("prom_query_duration_ms", true);
const pgDuration = new Trend("pg_query_duration_ms", true);
// Failures are counted, not just logged. Under concurrency a query can fail
// and still contribute a latency figure, so a run of errors looks exactly
// like a fast system on a throughput panel.
const promFailed = new Counter("prom_failed");
const pgFailed = new Counter("pg_failed");

const promQuery =
  `${PROM_URL}/api/v1/query_range?query=${encodeURIComponent(query.promql)}` +
  `&start=${manifest.start_s}&end=${manifest.end_s}` +
  `&step=${manifest.step_seconds}`;

export const options = {
  // Both arms always present, even when a query is meaningless for one of
  // them, so the run's shape does not change with the query.
  scenarios: {
    prom: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: STAGES,
      gracefulStop: "10s",
      exec: "prom",
      tags: { system: "prometheus", run_id: RUN_ID, query: QUERY_NAME },
    },
    pg: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: STAGES,
      gracefulStop: "10s",
      exec: "pg",
      tags: { system: "postgres", run_id: RUN_ID, query: QUERY_NAME },
    },
  },
};

let conn = null;

export function prom() {
  const started = Date.now();
  const res = http.get(promQuery, { timeout: "120s" });
  promRequests.add(1);
  // Clamp at zero. k6 has been seen to report a small negative min for a
  // Trend fed from Date.now() deltas, and a negative duration in the report
  // reads as a broken measurement rather than a rounding artefact.
  promDuration.add(Math.max(0, Date.now() - started));
  if (res.status !== 200) {
    promFailed.add(1);
    console.error("prometheus %d: %s", res.status, res.body.slice(0, 200));
  }
}

export function pg() {
  const started = Date.now();
  if (conn === null) {
    // First iteration of this VU. Each VU gets its own connection.
    conn = sqlmod.open(postgres, PG_DSN);
  }
  try {
    conn.query(query.sql[BACKEND]);
    pgRequests.add(1);
  } catch (e) {
    pgFailed.add(1);
    console.error("postgres: %s", String(e).slice(0, 200));
  }
  pgDuration.add(Math.max(0, Date.now() - started));
}
