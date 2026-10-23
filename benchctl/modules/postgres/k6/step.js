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

// Read-side randomisation must happen HERE, not in SQL. `random()` in a
// WHERE clause is volatile, and a volatile expression is not a
// pseudo-constant, so PostgreSQL cannot use an index for it — every
// "point lookup" silently becomes a seq scan with LIMIT 1. Measured
// cost of that mistake: ~5s per login instead of ~0.1ms. Substituting
// the number client-side keeps the SQL literal and the index usable.
const UID_MAX = parseInt(__ENV.K6_UID_MAX || '9999999', 10);
function materialize(q) {
  if (q.indexOf('__UID__') === -1) return q;
  return q.split('__UID__').join(
    String(1 + Math.floor(Math.random() * UID_MAX)));
}

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
// idxlab: the index-mode scenario. One INSERT per iteration against the
// docs table — every access method on the table pays its own per-
// transaction maintenance cost (hash overflow checks, GiST picksplit,
// GIN pending list, BRIN summary updates) plus the shared WAL commit.
const IDXLAB = __ENV.IDXLAB === '1';
const INSERT_MODE = LAYOUT !== '' || POOLS > 0 || IDXLAB;

// MIX_READ_PCT: share of insert iterations that also serve the point
// lookup the application runs between sign-ups. Every read number in the
// other scenarios comes from a quiet database; this leg is the first one
// where reads and writes share the same connection and the same 90 s.
// It only compares against itself: same scenario, same runner, same leg
// shape, one number different.
const MIX_READ_PCT = parseInt(__ENV.MIX_READ_PCT || '0', 10);
// Seeded population, for a read key that is not the row just written.
const SEEDED = parseInt(__ENV.N_USERS || __ENV.K6_UID_MAX || '9999999', 10);

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
  const cols = '(user_id, email, name, created_at'
    + (MODE === 'none' ? ')' : ', payload)');
  if (MODE === 'none') {
    return 'INSERT INTO ' + table + ' ' + cols + ' VALUES ('
      + uid + ", 'user" + uid + "@signup.example.com', 'User " + uid
      + "', now())";
  }
  const payload = 'jsonb_build_object('
    + "'tier', 'tier_' || (" + (uid % 5) + ' + 1), '
    + "'tags', to_jsonb(array['alpha','beta','gamma']), "
    + "'attrs', jsonb_build_object('blob', repeat(md5(" + uid
    + "::text), (CASE WHEN " + (uid % 10) + " < 3 THEN 40 ELSE 16 END))))";
  return 'INSERT INTO ' + table + ' ' + cols + ' VALUES ('
    + uid + ", 'user" + uid + "@signup.example.com', 'User " + uid
    + "', now(), " + payload + ')';
}

// One disjoint user_id range per VU: no PK collisions across VUs, and
// the email unique-index never sees a duplicate insert across VUs.
const uidBase = [];
let conn = undefined;

// idxlab payload: a docs row covering every extended column the index
// legs build on. rating uses a small modulus so an equality lookup
// stays ~100 rows of 10M; loc maps into a known coordinate space.
function idxlabSql(uid) {
  return "INSERT INTO docs (id, created_at, rating, loc, name, tags) VALUES ("
    + uid + ", now(), " + (uid % 100000)
    + ", point(" + (uid % 10000) + ", " + ((uid * 3) % 10000) + ")"
    + ", 'User " + uid + "', ARRAY['t" + (uid % 25)
    + "','tag" + (uid % 7) + "'])";
}

export function step() {
  if (conn === undefined) {
    conn = sql.open(pgDriver, PG_DSN);
    // Base 1e9: safely above every seeded population (10M..100M). The
    // scale-sweep legs seed 50M/100M ids, where the old 50M base
    // collided straight into the PK.
    uidBase[exec.vu.idInTest] =
      1000000000 + (exec.vu.idInTest - 1) * 100000000;
  }
  if (IDXLAB) {
    uidBase[exec.vu.idInTest] += 1;
    const uid = uidBase[exec.vu.idInTest];
    conn.query(idxlabSql(uid));
    check(uid, { 'insert ok': (u) => u > 0 });
    return true;
  }
  if (INSERT_MODE) {
    uidBase[exec.vu.idInTest] += 1;
    const uid = uidBase[exec.vu.idInTest];
    const table = targetTable(uid, exec.vu.idInTest);
    // INSERT returns no rows; anything but success throws here.
    conn.query(insertSql(uid, table));
    check(uid, { 'insert ok': (u) => u > 0 });
    // Mixed leg: a random lookup in the seeded population, not the row just
    // inserted (reading your own write would measure a cache hit, not a
    // lookup). Layout A only: with LAYOUT B the read key would have to be
    // routed to the same table, which is a different question.
    if (MIX_READ_PCT > 0
        && (exec.vu.iterationInInstance % 100) < MIX_READ_PCT) {
      const readUid = 1 + Math.floor(Math.random() * SEEDED);
      const rows = conn.query(
        'SELECT user_id, name FROM ' + table + ' WHERE user_id = ' + readUid);
      check(rows, { 'read ok': (r) => r.length >= 0 });
    }
    return true;
  }
  const result = conn.query(materialize(QUERY));
  check(result, { 'query ok': (r) => r.length >= 0 });
  return true;
}

export function teardown(data) {
  if (conn) conn.close();
}
