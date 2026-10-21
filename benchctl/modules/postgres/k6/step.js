// modules/postgres/k6/step.js — one SQL query via xk6-sql.
// The driver symlinks modules/active -> the current module before the run,
// so this import path is fixed regardless of which module is active.
// Connection created per-VU; maxVUs in scenario limits total concurrent connections.
// xk6-sql requires the driver OBJECT, not a string name.
import sql from 'k6/x/sql';
import pgDriver from 'k6/x/sql/driver/postgres';
import { check } from 'k6';
import exec from 'k6/execution';

const plan = JSON.parse(open('../../../plan.json'));
const QUERY = __ENV.K6_QUERY || plan.query;  // rendered SQL from scenario
const PG_DSN = __ENV.PG_DSN || 'postgres://bench:bench@sut:5432/bench?sslmode=disable';

// ---- userdb insert mode ------------------------------------------------
// When the scenario's leg carries a LAYOUT/POOLS dimension, this step
// becomes a registration transaction instead of a rendered SELECT: one
// INSERT per arrival, routed exactly like the application would route
// it. This is the point of the userdb variant — a small transaction that
// pays one WAL commit per arrival, which is where the filesystem's
// fsync rate shows up.
const LAYOUT = __ENV.LAYOUT || '';
const MODE = __ENV.MODE || 'none';
const SHARDS = parseInt(__ENV.SHARDS || '32', 10);
// POOLS: pool-per-schema mode. Pool p == schema s_p == one users table;
// VU i belongs to pool (i-1) % P, so P * CONNS VUs saturate the pools
// the way the application's pool sizing would.
const POOLS = parseInt(__ENV.POOLS || '0', 10);
const INSERT_MODE = LAYOUT !== '' || POOLS > 0;

function targetTable(uid, vuIdx) {
  if (POOLS > 0 || LAYOUT === 'C') {
    // M1: pool == schema, one users table per schema. POOLS comes from
    // pool-sweep legs; a plain C leg routes data by SHARDS.
    const p = POOLS > 0 ? POOLS : SHARDS;
    const shard = (vuIdx - 1) % p;
    return 's' + String(shard).padStart(2, '0') + '.users';
  }
  if (LAYOUT === 'B') {
    // M2: table-suffix sharding, one pool.
    return 'users_' + String(uid % SHARDS).padStart(2, '0');
  }
  return 'users'; // A single table (M0)
}

function insertSql(uid, table) {
  const payload = MODE === 'none' ? '' : (
    ', jsonb_build_object('
    + "'tier', 'tier_' || (" + (uid % 5) + ' + 1), '
    + "'tags', to_jsonb(array['alpha','beta','gamma']), "
    + "'attrs', jsonb_build_object('blob', repeat(md5(" + uid
    + "::text), (CASE WHEN " + (uid % 10) + ' < 3 THEN 40 ELSE 16 END))))'
  );
  const cols = '(user_id, email, name, created_at'
    + (MODE === 'none' ? ')' : ', payload)');
  return 'INSERT INTO ' + table + ' ' + cols + ' VALUES ('
    + uid + ", 'user" + uid + "@signup.example.com', 'User " + uid
    + "', now()" + (MODE === 'none' ? ')' : (', ' + payload + ')'));
}

// One disjoint user_id range per VU: no PK collisions across VUs, and
// the email unique-index never sees a duplicate insert across VUs.
const uidBase = [];
let conn = undefined;

export function step() {
  if (conn === undefined) {
    conn = sql.open(pgDriver, PG_DSN);
    uidBase[exec.vu.idInTest] =
      50000000 + (exec.vu.idInTest - 1) * 100000000;
  }
  if (INSERT_MODE) {
    uidBase[exec.vu.idInTest] += 1;
    const uid = uidBase[exec.vu.idInTest];
    const table = targetTable(uid, exec.vu.idInTest);
    // INSERT returns no rows; anything but success throws here.
    conn.query(insertSql(uid, table));
    check(uid, { 'insert ok': (u) => u > 0 });
    return true;
  }
  const result = conn.query(QUERY);
  check(result, { 'query ok': (r) => r.length >= 0 });
  return true;
}

export function teardown(data) {
  if (conn) conn.close();
}
