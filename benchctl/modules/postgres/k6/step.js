// modules/postgres/k6/step.js — one SQL query via xk6-sql.
// The driver symlinks modules/active -> the current module before the run,
// so this import path is fixed regardless of which module is active.
// Connection is opened lazily per-VU because k6 lifecycle hooks (setup/teardown)
// are taken ONLY from the entry script (ramp.js), so setup() in this module
// never fires. Even if it did, init context ≠ VU context, so module-level
// `conn` set in setup() is a different instance from the one seen in step().
import sql from 'k6/x/sql';
import { check } from 'k6';

const plan = JSON.parse(open('../../../plan.json'));
const QUERY = __ENV.K6_QUERY || plan.query;  // rendered SQL from scenario
const PG_DSN = __ENV.PG_DSN || 'postgres://bench:bench@sut:5432/bench?sslmode=disable';

let conn;

export function step() {
  if (conn === undefined) {
    conn = sql.open('postgres', PG_DSN);
  }
  const result = conn.query(QUERY);
  check(result, { 'query ok': (r) => r.length >= 0 });
  return true;
}

export function teardown(data) {
  if (conn) conn.close();
}