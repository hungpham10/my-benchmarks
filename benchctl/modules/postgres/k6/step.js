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
// When the scenario's leg carries a LAYOUT dimension, this step becomes a
// registration transaction instead of a rendered SELECT: one INSERT per
// arrival, routed exactly like the application would route it. This is the
// point of the userdb variant — a small transaction that pays async_commit
// per arrival, which is where the filesystem's fsync rate shows up.
const LAYOUT = __ENV.LAYOUT || '';
const MODE = __ENV.MODE || 'none';
const SHARDS = parseInt(__ENV.SHARDS || '32', 10);
// POOLS: pool-per-schema mode. Pool p == schema s_p == one users table;
// VU i belongs to pool (i-1) % P, so P * CONNS VUs saturate the pools the
// way the application's pool sizing would. LAYOUT C with POOLS set is
// this mode; POOLS alone is enough to switch it on.
const POOLS = parseInt(__ENV.POOLS || '0', 10);
const POOL = POOLS > 0;

function targetTable(uid, vuIdx) {
  if (POOL) {
    return 's' + String((vuIdx - 1) % POOLS).padStart(2, '0') + '.users';
  }
  if (LAYOUT === 'B') return 'users_' + String(uid % SHARDS).padStart(2, '0');
  if (LAYOUT === 'C') {
    const i = uid % SHARDS;
    return 's' + String(Math.floor(i / 4)).padStart(2, '0')
         + '.users_' + String(i % 4).padStart(2, '0');
  }
  return 'users'; // A single table, E declarative hash (DB routes)
}

function insertSql(uid, vuIdx) {
  const t = targetTable(uid, vuIdx);

// One disjoint user_id range per VU: disjoint namespaces keep the PK a
// unique-insert (always an append into the rightmost pages of one shard)
// AND the email unique-index insert unique across VUs.
let uidBase;
let uidCount = 0;

function targetTable(uid) {
  if (LAYOUT === 'B') return 'users_' + String(uid % SHARDS).padStart(2, '0');
  if (LAYOUT === 'C') {
    const i = uid % SHARDS;
    return 's' + String(Math.floor(i / 4)).padStart(2, '0')
         + '.users_' + String(i % 4).padStart(2, '0');
  }
  return 'users'; // A single table, E declarative hash (DB routes)
}

function insertSql(uid) {
  const t = targetTable(uid);
  const payload = MODE === 'none' ? '' : (
    ', jsonb_build_object('
    + "'tier', 'tier_' || (" + uid + ' % 5 + 1), '
    + "'tags', to_jsonb(array['alpha','beta','gamma']), "
    + "'attrs', jsonb_build_object('blob', repeat(md5(" + uid
    + "::text), (CASE WHEN " + uid + ' % 10 < 3 THEN 40 ELSE 16 END))))'
  );
  const cols = '(user_id, email, name, created_at' + (MODE === 'none' ? ')' : ', payload)');
  return 'INSERT INTO ' + t + ' ' + cols + ' VALUES ('
    + uid + ", 'user" + uid + "@signup.example.com', 'User " + uid
    + "', now()" + (MODE === 'none' ? ')' : (', ' + payload + ')'));
}

let conn;

export function step() {
  if (conn === undefined) {
    conn = sql.open(pgDriver, PG_DSN);
  }
  if (LAYOUT || POOL) {
    // userdb insert mode: disjoint user_id per VU, routed by the leg.
    if (uidBase === undefined) {
      uidBase = 50000000 + (exec.vu.idInTest - 1) * 100000000;
    }
    uidCount += 1;
    const uid = uidBase + uidCount;
    // The result of an INSERT carries no rows; anything but success throws.
    conn.query(insertSql(uid));
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