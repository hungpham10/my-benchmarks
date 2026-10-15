// modules/postgres/k6/step.js — one SQL query via xk6-sql.
// The driver symlinks modules/active -> the current module before the run,
// so this import path is fixed regardless of which module is active.
// Uses a connection POOL (not per-VU connection) to bound resource usage.
// Pool is created at module load time (init context) and shared across all VUs.
// Lifecycle hooks from this module are ignored because k6 only takes
// setup/teardown from the entry script (ramp.js).
// xk6-sql requires the driver OBJECT, not a string name.
// Pool is created via sql.open with { maxOpenConns } option.
import sql from 'k6/x/sql';
import pgDriver from 'k6/x/sql/driver/postgres';
import { check } from 'k6';

const plan = JSON.parse(open('../../../plan.json'));
const QUERY = __ENV.K6_QUERY || plan.query;  // rendered SQL from scenario
const PG_DSN = __ENV.PG_DSN || 'postgres://bench:bench@sut:5432/bench?sslmode=disable';
const MAX_CONN = parseInt(__ENV.PG_MAX_CONN || '32', 10);

// Created once in init context, shared by all VUs.
const pool = sql.open(pgDriver, PG_DSN, { maxOpenConns: MAX_CONN });

export function step() {
  const result = pool.query(QUERY);
  check(result, { 'query ok': (r) => r.length >= 0 });
  return true;
}

export function teardown(data) {
  pool.close();
}