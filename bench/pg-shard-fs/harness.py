#!/usr/bin/env python3
"""
PostgreSQL modulo-suffix sharding benchmark harness.

Usage:
  python3 harness.py --dsn postgres://postgres@localhost/bench \
      --layout B --index I1 --shards 32 --rows 50000000 --phases load,hybrid,read

Layouts:
  A  single table `events`
  B  N tables `events_XX` (user_id % N)
  C  8 schemas x 4 tables (same footprint as B, namespace-only control)
  D  layout B + jsonb payload column
  E  declarative: PARTITION BY HASH (user_id) PARTITIONS N
  F  layout B + BRIN (created_at) per shard

Index variants (per table/partition):
  I1  btree (user_id, created_at DESC)
  I2  btree (user_id)
  I3  layout D + GIN jsonb_ops (fastupdate=on)
  I4  layout D + GIN jsonb_path_ops (fastupdate=off)  [requires layout D]
  I5  layout D + generated col kind + btree (user_id, kind)

Metrics written to <out_dir>/result_<tag>.jsonl (JSON lines: one per phase)
plus a per-run metadata header record.
"""
import argparse
import json
import math
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import psycopg2.extensions

KINDS = [f"kind_{i:02d}" for i in range(20)]
TAGS = ["alpha", "beta", "gamma", "delta", "eps"]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def connect(dsn, autocommit=True):
    conn = psycopg2.connect(dsn)
    conn.autocommit = autocommit
    with conn.cursor() as cur:
        cur.execute("SET synchronous_commit = on")
    return conn


def q(conn, sql, args=None, fetch=False):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        if fetch:
            return cur.fetchall()
        return None


def now_ms():
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# layout creation
# --------------------------------------------------------------------------

def drop_all(conn):
    q(conn, """
        DO $$
        DECLARE r RECORD;
        BEGIN
          FOR r IN SELECT nspname FROM pg_namespace
                   WHERE nspname ~ '^s[0-9]+_|^bench' LOOP
            EXECUTE format('DROP SCHEMA IF EXISTS %I CASCADE', r.nspname);
          END LOOP;
        END $$""")
    q(conn, "DROP TABLE IF EXISTS events CASCADE")


def physical_tables(layout, n):
    """Return list of quoted physical table names."""
    if layout in ("A", "E"):
        return ["events"]
    if layout == "C":
        return [f"s{i // 4:02d}.events_{i % 4:02d}" for i in range(n)]
    # B, D, F
    return [f"events_{i:02d}" for i in range(n)]


def payload_cols(layout):
    return ", payload jsonb" if layout in ("D",) else ""


def create_layout(conn, layout, n, index):
    drop_all(conn)
    has_jsonb = layout == "D" or index == "I5"
    if index == "I5" and layout not in ("D",):
        layout = "D"  # I5 requires a jsonb column to feed the generated col
    extra_payload = ", payload jsonb" if has_jsonb else ""
    kind_gen = (", kind text GENERATED ALWAYS AS "
                "(payload->>'kind') STORED") if index == "I5" else ""
    meta = {"layout": layout, "index": index, "shards": n}

    if layout == "E":
        q(conn, f"""
            CREATE TABLE events (
                id bigint primary key,
                user_id bigint not null,
                created_at timestamp not null,
                note text{extra_payload}{kind_gen}
            ) PARTITION BY HASH (user_id)""")
        for i in range(n):
            q(conn, f"CREATE TABLE events_p{i} PARTITION OF events "
                    f"FOR VALUES WITH (MODULUS {n}, REMAINDER {i})")
        tables = ["events"]
    elif layout == "C":
        for i in range(8):
            q(conn, f"CREATE SCHEMA IF NOT EXISTS s{i:02d}")
        for i in range(n):
            sc, tb = f"s{i // 4:02d}", f"events_{i % 4:02d}"
            q(conn, f"""
                CREATE TABLE {sc}.{tb} (
                    id bigint primary key,
                    user_id bigint not null,
                    created_at timestamp not null,
                    note text{extra_payload}{kind_gen}
                )""")
        tables = physical_tables("C", n)
    elif layout in ("B", "D", "F"):
        # N physical shard tables, routed by user_id %% N (the core layout)
        for i in range(n):
            q(conn, f"""
                CREATE TABLE events_{i:02d} (
                    id bigint primary key,
                    user_id bigint not null,
                    created_at timestamp not null,
                    note text{extra_payload}{kind_gen}
                )""")
        tables = physical_tables("B", n)
    else:  # layout A: single baseline table
        q(conn, f"""
            CREATE TABLE events (
                id bigint primary key,
                user_id bigint not null,
                created_at timestamp not null,
                note text{extra_payload}{kind_gen}
            )""")
        tables = ["events"]

    # secondary indexes
    all_t = tables if layout == "E" else physical_tables(layout, n)
    for t in all_t:
        if layout == "E" and t == "events":
            continue  # partitions created after; index via parent after loop
        add_indexes(conn, layout, index, t, has_jsonb)
    if layout == "E":
        # index parent table (partitions inherit)
        add_indexes(conn, "A", index, "events", has_jsonb)

    if layout == "F":
        for t in all_t:
            safe = t.replace(".", "_")
            q(conn, f"CREATE INDEX IF NOT EXISTS brin_ca_{safe} "
                    f"ON {t} USING brin (created_at)")

    return meta


def add_indexes(conn, layout, index, table, has_jsonb):
    safe = table.replace(".", "_")
    gin = ""
    if layout == "D":
        if index == "I3":
            gin = (f"CREATE INDEX gin_jb_{safe} ON {table} "
                   f"USING gin (payload jsonb_ops) WITH (fastupdate=on);")
        elif index == "I4":
            gin = (f"CREATE INDEX gin_jb_{safe} ON {table} "
                   f"USING gin (payload jsonb_path_ops) WITH (fastupdate=off);")
    if gin:
        q(conn, gin)

    if index in ("I1", "I3", "I4"):
        q(conn, f"CREATE INDEX btree_uc_{safe} ON {table} "
                f"(user_id, created_at DESC)")
    elif index == "I5":
        q(conn, f"CREATE INDEX btree_uk_{safe} ON {table} (user_id, kind)")
    elif index == "I2":
        q(conn, f"CREATE INDEX btree_u_{safe} ON {table} (user_id)")


# --------------------------------------------------------------------------
# shard routing
# --------------------------------------------------------------------------

def shard_expr(layout, n):
    if layout in ("A", "E"):
        return "events"
    if layout == "C":
        return None  # handled by caller (needs numeric shard)
    return "events_{%02d}"  # caller formats


def target_for(layout, n, user_id, rng):
    """Physical target (schema-qualified) for a user_id."""
    if layout in ("A", "E"):
        return "events"
    s = user_id % n
    if layout == "C":
        return f"s{s // 4:02d}.events_{s % 4:02d}"
    return f"events_{s:02d}"


# --------------------------------------------------------------------------
# seeding (server-side generate_series, parallel by id ranges)
# --------------------------------------------------------------------------

def seed_worker(dsn, layout, n, total, start, end):
    """Seed one id-range into a single-table layout (A/E only)."""
    conn = connect(dsn)
    q(conn, "SET synchronous_commit = off")
    q(conn, """
        INSERT INTO events (id, user_id, created_at, note)
        SELECT i, i,
               timestamp '2026-01-01' + (i %% ('30 days')::interval),
               repeat(md5(i::text), 3)
        FROM generate_series(%(s)s, %(e)s) g(i)
    """, {"s": start, "e": end})
    conn.close()


def seed(dsn, layout, n, total, workers):
    t0 = time.time()
    per = total // workers
    ranges = []
    s = 1
    for w in range(workers):
        e = s + per - 1 if w < workers - 1 else total
        ranges.append((s, e))
        s = e + 1

    if layout in ("A", "E"):
        with ThreadPoolExecutor(workers) as ex:
            futs = [ex.submit(seed_worker, dsn, layout, n, total, a, b)
                    for a, b in ranges]
            [f.result() for f in futs]
    else:
        # per-shard parallel for B/C/D/F
        def shard_job(shard):
            c = connect(dsn)
            payload_sel_sql = ""
            if layout == "D":
                payload_sel_sql = (
                    ", jsonb_build_object("
                    "'kind', 'kind_' || lpad((i %% 20)::text, 2, '0'), "
                    "'tags', to_jsonb(array['alpha','beta','gamma']), "
                    "'attrs', jsonb_build_object("
                    "'blob', CASE WHEN i %% 5 = 0 THEN repeat(md5(i::text), 40) "
                    "ELSE repeat(md5(i::text), 16) END))")
            uid = "i"
            tname = target_for(layout, n, shard, None)
            # NOTE: user_id stores i (globally unique); route via i % N
            base_cols = "id, user_id, created_at, note"
            if layout == "D":
                base_cols += ", payload"
            q(c, "SET synchronous_commit = off")
            q(c, f"""
                INSERT INTO {tname} ({base_cols})
                SELECT i, i,
                       timestamp '2026-01-01' + (i %% ('30 days')::interval),
                       repeat(md5(i::text), (1800 / 32) + 1){payload_sel_sql}
                FROM generate_series(1, %(e)s) g(i)
                WHERE i %% {n} = %(sh)s
            """, {"e": total, "sh": shard})
            c.close()

        with ThreadPoolExecutor(workers) as ex:
            futs = [ex.submit(shard_job, s) for s in range(n)]
            [f.result() for f in futs]

    # FK-like integrity check: quick count
    conn = connect(dsn)
    cnt = q(conn, f"SELECT count(*) FROM "
                  f"{'events' if layout in ('A', 'E') else '(' + ' UNION ALL '.join(['SELECT user_id FROM ' + t for t in physical_tables(layout, n)] + ')')}",
            fetch=True)[0][0]
    conn.close()
    return {
        "rows": cnt,
        "seed_seconds": round(time.time() - t0, 1),
    }


# --------------------------------------------------------------------------
# phases
# --------------------------------------------------------------------------

def wal_bytes(dsn):
    c = connect(dsn)
    r = q(c, "SELECT wal_records, wal_bytes FROM pg_stat_wal", fetch=True)
    c.close()
    return r[0][1] if r else 0


def load_phase(dsn, layout, n, duration, workers, out):
    """Insert new rows continuously with random user_ids."""
    stop = threading.Event()
    counters = [0] * workers
    lat = [[] for _ in range(workers)]

    def writer(w):
        c = connect(dsn)
        rnd = random.Random(os.getpid() + w)
        cols = "user_id, created_at, note" + (", payload" if layout == "D" else "")
        while not stop.is_set():
            shard_ids = [rnd.randrange(1, 10**9) for _ in range(200)]
            grouped = {}
            for uid in shard_ids:
                t = target_for(layout, n, uid, rnd)
                grouped.setdefault(t, []).append(uid)
            for t, uids in grouped.items():
                v = ",".join(
                    f"({uid}, now(), repeat(md5({uid}::text), 57)"
                    + (f", jsonb_build_object('kind','kind_' || lpad(({uid % 20})::text,2,'0'), 'tags', to_jsonb(array['alpha','beta']), 'attrs', jsonb_build_object('blob', repeat(md5({uid}::text), 16)))"
                       if layout == "D" else "") + ")"
                    for uid in uids)
                t_begin = time.time()
                q(c, f"INSERT INTO {t} ({cols}) VALUES {v}")
                lat[w].append(time.time() - t_begin)
                counters[w] += len(uids)
        c.close()

    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(writer, w) for w in range(workers)]
        w0 = wal_bytes(dsn)
        t0 = time.time()
        time.sleep(duration)
        stop.set()
        [f.result() for f in futs]
    elapsed = time.time() - t0
    rows = sum(counters)
    w1 = wal_bytes(dsn)
    rows_lats = [x for l in lat for x in l]
    rows_lats.sort()
    result = {
        "phase": "load", "duration": elapsed, "rows": rows,
        "rows_per_sec": round(rows / elapsed, 1),
        "wal_bytes_per_row": round(max((w1 - w0) / max(rows, 1), 0), 1),
        "lat_p50": round(rows_lats[len(rows_lats) // 2] * 1000, 2) if rows_lats else None,
        "lat_p95": round(rows_lats[int(len(rows_lats) * .95) - 1] * 1000, 2) if rows_lats else None,
    }
    out(result)
    return result


def _point_read(c, layout, n, rnd):
    uid = rnd.randrange(1, 10**9)
    t = target_for(layout, n, uid, rnd)
    return q(c, f"""
        SELECT count(*) FROM {t}
        WHERE user_id = %(u)s AND created_at > now() - interval '15 days'
    """, {"u": uid}, fetch=True)[0][0]


def _top_read(c, layout, n, rnd):
    uid = rnd.randrange(1, 10**9)
    t = target_for(layout, n, uid, rnd)
    return q(c, f"""
        SELECT id, created_at FROM {t}
        WHERE user_id = %(u)s ORDER BY created_at DESC LIMIT 20
    """, {"u": uid}, fetch=True)


def hybrid_phase(dsn, layout, n, duration, writers, readers, out):
    stop = threading.Event()
    w_ct, r_ct, r_lat = [0] * writers, [0] * readers, [[]]

    def writer(w):
        c = connect(dsn)
        rnd = random.Random(os.getpid() + w + 100)
        while not stop.is_set():
            uid = rnd.randrange(1, 10**9)
            t = target_for(layout, n, uid, rnd)
            cols = "user_id, created_at, note" + (", payload" if layout == "D" else "")
            v = (f"({uid}, now(), repeat(md5({uid}::text), 57)"
                 + (f", jsonb_build_object('kind','kind_' || lpad(({uid % 20})::text,2,'0'),"
                    f" 'attrs', jsonb_build_object('blob', repeat(md5({uid}::text), 16)))"
                    if layout == "D" else "") + ")")
            q(c, f"INSERT INTO {t} ({cols}) VALUES {v}")
            w_ct[w] += 1
        c.close()

    def reader(r):
        c = connect(dsn)
        rnd = random.Random(os.getpid() + r + 200)
        while not stop.is_set():
            f = _point_read if r % 2 else _top_read
            t0 = time.time()
            f(c, layout, n, rnd)
            r_lat[0].append(time.time() - t0)
            r_ct[r] += 1
        c.close()

    with ThreadPoolExecutor(writers + readers) as ex:
        futs = ([ex.submit(writer, w) for w in range(writers)]
                + [ex.submit(reader, r) for r in range(readers)])
        t0 = time.time()
        time.sleep(duration)
        stop.set()
        [f.result() for f in futs]

    l = r_lat[0]; l.sort()
    result = {
        "phase": "hybrid", "duration": time.time() - t0,
        "inserts": sum(w_ct), "reads": sum(r_ct),
        "read_p50_ms": round(l[len(l) // 2] * 1000, 2) if l else None,
        "read_p95_ms": round(l[int(len(l) * .95) - 1] * 1000, 2) if l else None,
    }
    out(result)
    return result


def readonly_phase(dsn, layout, n, duration, readers, out):
    stop = threading.Event()
    ct = [0] * readers
    lat = [[]]

    def reader(r):
        c = connect(dsn)
        rnd = random.Random(os.getpid() + r)
        while not stop.is_set():
            t0 = time.time()
            _point_read(c, layout, n, rnd)
            lat[0].append(time.time() - t0)
            ct[r] += 1
        c.close()

    with ThreadPoolExecutor(readers) as ex:
        futs = [ex.submit(reader, r) for r in range(readers)]
        t0 = time.time()
        time.sleep(duration)
        stop.set()
        [f.result() for f in futs]

    l = lat[0]; l.sort()
    result = {
        "phase": "readonly", "duration": time.time() - t0,
        "reads": sum(ct), "reads_per_sec": round(sum(ct) / (time.time() - t0), 1),
        "lat_p50_ms": round(l[len(l) // 2] * 1000, 2) if l else None,
        "lat_p95_ms": round(l[int(len(l) * .95) - 1] * 1000, 2) if l else None,
    }
    out(result)
    return result


def cache_stats(dsn, layout, n, out):
    c = connect(dsn)
    tables = physical_tables(layout, n) if layout not in ("A", "E") else ["events"]
    rows = []
    for t in tables:
        rows.extend(q(c, f"""
            SELECT '{t}', heap_blks_hit, heap_blks_read, idx_blks_hit, idx_blks_read
            FROM pg_statio_user_tables WHERE relname = %(r)s
            AND ( schemaname = split_part(%(t)s, '.', 1) OR %(t)s !~ '\\.' )
        """, {"r": t.split(".")[-1], "t": t}, fetch=True))
    hit = sum(r[1] + r[3] for r in rows)
    read = sum(r[2] + r[4] for r in rows)
    result = {"phase": "cache",
              "hit_ratio": round(hit / max(hit + read, 1), 4)}
    out(result)
    return result


def shard_balance(dsn, layout, n, out):
    if layout in ("A", "E"):
        return None
    c = connect(dsn)
    counts = []
    for t in physical_tables(layout, n):
        counts.append(q(c, f"SELECT count(*) FROM {t}", fetch=True)[0][0])
    lo, hi = min(counts), max(counts)
    result = {"phase": "balance", "min": lo, "max": hi,
              "spread_pct": round(100 * (hi - lo) / max(sum(counts), 1), 3)}
    out(result)
    return result


# --------------------------------------------------------------------------
# iostat sidebar
# --------------------------------------------------------------------------

class Iostat(threading.Thread):
    def __init__(self, mount, path):
        super().__init__(daemon=True)
        self.mount = mount
        self.path = path
        self.samples = []
        self.proc = None

    def start(self):
        if not shutil.which("iostat"):
            return
        self.proc = subprocess.Popen(
            ["iostat", "-x", "1"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        super().start()

    def run(self):
        while self.proc and self.proc.poll() is None:
            try:
                r = subprocess.run(["iostat", "-x", "1", "2"],
                                   capture_output=True, text=True, timeout=10)
                self.samples.append(r.stdout[-2000:])
            except Exception:
                pass
            time.sleep(1)

    def stop(self):
        if self.proc:
            self.proc.terminate()
        with open(os.path.join(self.path, "iostat.log"), "w") as f:
            f.write("\n=====\n".join(self.samples[-600:]))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--layout", default="B", choices=list("ABCDEF"))
    ap.add_argument("--index", default="I1", choices=["I1", "I2", "I3", "I4", "I5"])
    ap.add_argument("--shards", type=int, default=32)
    ap.add_argument("--rows", type=int, default=50_000_000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--load-sec", type=int, default=60)
    ap.add_argument("--hybrid-sec", type=int, default=60)
    ap.add_argument("--read-sec", type=int, default=30)
    ap.add_argument("--skip-seed", action="store_true")
    ap.add_argument("--out-dir", default="out")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    tag = f"L{args.layout}_{args.index}_N{args.shards}"
    resf = open(os.path.join(args.out_dir, f"result_{tag}.jsonl"), "a")

    results = []

    def out(rec):
        rec["tag"] = tag
        results.append(rec)
        resf.write(json.dumps(rec) + "\n"); resf.flush()

    meta = {"layout": args.layout, "index": args.index, "shards": args.shards,
            "rows": args.rows, "workers": args.workers, "ts": now_ms(),
            "pg_version": None}

    iost = Iostat("/", args.out_dir)

    conn = connect(args.dsn)
    meta["pg_version"] = q(conn, "SHOW server_version", fetch=True)[0][0]
    try:
        q(conn, "CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    except Exception:
        pass
    conn.close()

    if not args.skip_seed:
        print("seeding...", file=sys.stderr)
        iost.start()
        meta["seed"] = seed(args.dsn, args.layout, args.shards, args.rows,
                            args.workers)
        iost.stop()

    print("LOAD phase", file=sys.stderr)
    iost = Iostat("/", args.out_dir); iost.start()
    load_phase(args.dsn, args.layout, args.shards, args.load_sec,
               args.workers, out)
    iost.stop()

    print("HYBRID phase", file=sys.stderr)
    iost = Iostat("/", args.out_dir); iost.start()
    hybrid_phase(args.dsn, args.layout, args.shards, args.hybrid_sec, 6, 4, out)
    iost.stop()

    print("READ-ONLY phase", file=sys.stderr)
    readonly_phase(args.dsn, args.layout, args.shards, args.read_sec, 8, out)

    cache_stats(args.dsn, args.layout, args.shards, out)
    shard_balance(args.dsn, args.layout, args.shards, out)

    out(meta)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
