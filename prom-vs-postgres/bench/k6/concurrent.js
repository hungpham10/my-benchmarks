/* Both systems under simultaneous load, in one k6 run.
 *
 * Two scenarios share one wall clock, so Prometheus and PostgreSQL are
 * driven at exactly the same moment. Two separate k6 processes would drift
 * apart by however long it takes to start the second one, and the comparison
 * would be between different instants rather than between the two systems.
 *
 * Per-target metrics, not shared ones:
 *
 *   prom_requests / pg_requests                Counter -> requests/s, i.e. TPS
 *   prom_query_duration_ms / pg_query_duration_ms   Trend -> latency
 *
 * k6 tags every metric with the scenario name, but the summary export
 * aggregates those tags away. Separate metric names are what make the two
 * systems readable side by side in results/ without querying anything.
 *
 * The VU level comes from the environment so that phase_saturation can walk
 * it upward and find the knee. Both scenarios always run at the SAME level:
 * comparing a saturated system against an idle one measures nothing.
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
const VUS = parseInt(__ENV.CONCURRENT_VUS || "4", 10);
const DURATION = __ENV.CONCURRENT_DURATION || "30s";

const manifest = JSON.parse(open("/results/queries.json"));
const query = manifest.queries.find((q) => q.name === QUERY_NAME);
if (!query) {
  throw new Error(`unknown QUERY_NAME ${QUERY_NAME}`);
}

// --- per-target metrics -----------------------------------------------------

const promDuration = new Trend("prom_query_duration_ms", true);
const pgDuration = new Trend("pg_query_duration_ms", true);
const promRequests = new Counter("prom_requests");
const pgRequests = new Counter("pg_requests");
// Failures are counted, not just logged. Under concurrency a Postgres query
// can fail while still producing a latency figure, and a run of errors looks
// exactly like a fast system on a throughput panel.
const promFailed = new Counter("prom_failed");
const pgFailed = new Counter("pg_failed");

const promQuery =
  `${PROM_URL}/api/v1/query_range?query=${encodeURIComponent(query.promql)}` +
  `&start=${manifest.start_s}&end=${manifest.end_s}` +
  `&step=${manifest.step_seconds}`;

export const options = {
  // Both arms always present, even when a query is meaningless for one of
  // them, so the run's cost and shape do not change with the query.
  scenarios: {
    prom: {
      executor: "constant-vus",
      vus: VUS,
      duration: DURATION,
      exec: "prom",
      tags: { system: "prometheus", run_id: RUN_ID, query: QUERY_NAME },
    },
    pg: {
      executor: "constant-vus",
      vus: VUS,
      duration: DURATION,
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