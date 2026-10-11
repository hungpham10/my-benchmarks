/* One k6 scenario: a single query shape against a single backend.
 *
 *   BACKEND     prometheus | metrics_jsonb | metrics_norm_brin
 *   QUERY_NAME  one of names in queries.json
 *   VUS / duration come from the CLI.
 *
 * queries.json is written by bench/run.py from bench/queries.py so the shapes
 * stay defined in exactly one place. Timestamps are inlined there as ISO
 * strings because the xk6-sql Postgres driver does not take psycopg3's %s
 * parameters.
 *
 * Each iteration is timed with a Trend tagged by backend and query; the trend
 * values go to the local monitoring Prometheus (and, when K6_CLOUD_RW_URL is
 * set, straight to Grafana Cloud) via the experimental remote-write output.
 */

import http from "k6/http";
import sql from "k6/x/sql";
import { check } from "k6";
import { Trend } from "k6/metrics";

const PROM_URL = __ENV.PROM_URL || "http://prometheus:9090";
const PG_DSN =
  __ENV.PG_DSN ||
  "postgres://bench:bench@postgres:5432/bench?sslmode=disable";
const BACKEND = __ENV.BACKEND;
const QUERY_NAME = __ENV.QUERY_NAME;
const RUN_ID = __ENV.BENCH_RUN_ID || "";
const COMMIT = __ENV.BENCH_COMMIT || "";

const manifest = JSON.parse(open("/results/queries.json"));
const query = manifest.queries.find((q) => q.name === QUERY_NAME);
if (!query) {
  throw new Error(`unknown QUERY_NAME ${QUERY_NAME}`);
}

const duration = new Trend("query_duration_ms", true);

// Top-level runs once per VU, so each VU owns its connection.
let conn = null;
let promQuery = null;
if (BACKEND === "prometheus") {
  promQuery =
    `${PROM_URL}/api/v1/query_range?query=${encodeURIComponent(query.promql)}` +
    `&start=${manifest.start_s}&end=${manifest.end_s}` +
    `&step=${manifest.step_seconds}`;
} else {
  conn = sql.open("postgres", PG_DSN);
}

export default function () {
  const started = Date.now();
  let ok = true;
  if (BACKEND === "prometheus") {
    const res = http.get(promQuery, { timeout: "120s" });
    ok = res.status === 200;
  } else {
    try {
      sql.query(conn, query.sql[BACKEND]);
    } catch (e) {
      ok = false;
      console.error(e);
    }
  }
  duration.add(Date.now() - started, { backend: BACKEND, query: QUERY_NAME, run_id: RUN_ID, commit: COMMIT });
  check(null, { "query succeeded": () => ok });
}
