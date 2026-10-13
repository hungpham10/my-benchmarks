"""Load the deterministic dataset into Prometheus and/or PostgreSQL.

Targets run in separate passes, never concurrently, so that the two systems
are never competing for the same CPU and memory while ingest throughput is
being measured.

    TARGETS=prometheus  remote-write only
    TARGETS=postgres    binary COPY into both schemas
    TARGETS=all         both, sequentially

Writes /results/dataset.json with the run parameters, including the
timestamp anchor that later phases reuse so every target sees the same
window. Correctness is checked by bench/parity.py, which queries both
systems rather than comparing against a precomputed manifest.
"""

import argparse
import http.client
import json
import os
import sys
import time
from urllib.parse import urlparse

import psycopg

import copyfmt as c
import dataset as d
import protobuf as p
import values as v

PROM_URL = os.environ.get("PROM_URL", "http://prometheus:9090")
SCRAPES_PER_REQUEST = int(os.environ.get("SCRAPES_PER_REQUEST", "240"))
SERIES_CHUNK = int(os.environ.get("SERIES_CHUNK", "1000"))
RESULTS_DIR = os.environ.get("RESULTS_DIR", "/results")

PG_DSN = (
    "host=%s port=%s user=%s password=%s dbname=%s"
    % (
        os.environ.get("PG_HOST", "postgres"),
        os.environ.get("PG_PORT", "5432"),
        os.environ.get("PG_USER", "bench"),
        os.environ.get("PG_PASSWORD", "bench"),
        os.environ.get("PG_DATABASE", "bench"),
    )
)

SAMPLE_TABLES = ("metrics_jsonb", "metrics_norm_brin")


def prune_other_schemas(cur, keep):
    """Drop the sample tables this leg is not measuring.

    The init script creates both schemas' tables, so a leg that loads only
    jsonb still carries an empty metrics_norm_brin with its indexes -- 57KB
    measured on a smoke run. That is nothing at full scale, but it is a
    confound in principle, and this removes it rather than arguing it away.

    series_dim belongs to the normalised design, so it is loaded with that
    leg and dropped otherwise: each leg then pays for its own metadata.
    """
    for table in SAMPLE_TABLES:
        if table == SCHEMAS[keep]["table"]:
            continue
        cur.execute("DROP TABLE IF EXISTS %s CASCADE" % table)
        log("pruned %s (not part of this leg)" % table)
    if not SCHEMAS[keep]["needs_dim"]:
        cur.execute("DROP TABLE IF EXISTS series_dim CASCADE")
        log("pruned series_dim (belongs to the norm design)")


def log(message):
    print("[generator] %s" % message, flush=True)


# ---------------------------------------------------------------------------
# Prometheus, via the remote-write receiver
# ---------------------------------------------------------------------------

def ingest_prometheus(series, t_start_ms):
    parsed = urlparse(PROM_URL)
    host = parsed.hostname or "prometheus"
    port = parsed.port or 80
    timestamps = v.generate_timestamps(t_start_ms)

    total_samples = len(series) * d.SAMPLES
    sent = 0
    requests = 0
    started = time.monotonic()

    conn = http.client.HTTPConnection(host, port, timeout=300)
    conn.connect()
    log("remote-write: real_compression=%s" % p.real_compression())

    for chunk_start in range(0, len(series), SERIES_CHUNK):
        chunk = series[chunk_start : chunk_start + SERIES_CHUNK]
        block = v.generate_chunk(chunk)

        for offset in range(0, d.SAMPLES, SCRAPES_PER_REQUEST):
            window = timestamps[offset : offset + SCRAPES_PER_REQUEST]
            series_batch = [
                p.encode_timeseries(
                    d.labels_of(desc),
                    [(block[row_index][i], int(window[i])) for i in range(window.size)],
                )
                for row_index, desc in enumerate(chunk)
            ]
            request = p.encode_write_request(series_batch)
            conn.request(
                "POST",
                "/api/v1/write",
                p.snappy_compress(request),
                {
                    "Content-Type": "application/x-protobuf",
                    "Content-Encoding": "snappy",
                    "X-Prometheus-Remote-Write-Version": "0.1.0",
                },
            )
            response = conn.getresponse()
            if response.status >= 300:
                raise RuntimeError(
                    "remote-write %d: %s" % (response.status, response.read()[:400])
                )
            response.read()
            sent += len(chunk) * window.size
            requests += 1

        log(
            "  %d/%d series sent (%.1f%%)"
            % (chunk_start + len(chunk), len(series), 100.0 * sent / total_samples)
        )

    conn.close()
    elapsed = time.monotonic() - started
    return {
        "target": "prometheus",
        "samples": sent,
        "requests": requests,
        "seconds": round(elapsed, 3),
        "samples_per_second": round(sent / elapsed, 1),
    }


# ---------------------------------------------------------------------------
# PostgreSQL, via binary COPY
# ---------------------------------------------------------------------------

def ensure_partitions(cur, t_start_ms, t_end_ms):
    """Create the daily partitions the future-dated window lands in."""
    first_day = int(t_start_ms) // 1000 // 86400
    last_day = int(t_end_ms) // 1000 // 86400
    for day_index in range(last_day - first_day + 1):
        day = _iso((first_day + day_index) * 86400)
        for table in SAMPLE_TABLES:
            cur.execute(
                "SELECT bench_ensure_partitions(%s::regclass, %s::date)", (table, day)
            )


def _iso(unix_seconds):
    return time.strftime("%Y-%m-%d", time.gmtime(unix_seconds))


def populate_series_dim(cur, series):
    """Insert every series once and read back its surrogate id."""
    ids = []
    for desc in series:
        cur.execute(
            "INSERT INTO series_dim (metric, job, instance, region, service) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (desc["metric"], desc["job"], desc["instance"], desc["region"], desc["service"]),
        )
        ids.append(cur.fetchone()[0])
    return ids


def load_schema_jsonb(cur, series, timestamps):
    for chunk_start in range(0, len(series), SERIES_CHUNK):
        chunk = series[chunk_start : chunk_start + SERIES_CHUNK]
        block = v.generate_chunk(chunk)
        rows = []
        for row_index, desc in enumerate(chunk):
            labels = c.jsonb(d.labels_json(desc))
            values = block[row_index]
            for i in range(d.SAMPLES):
                rows.append(c.row(c.ts(int(timestamps[i])), labels, c.float8(values[i])))
        with cur.copy(
            "COPY metrics_jsonb (time, labels, value) FROM STDIN (FORMAT binary)"
        ) as cp:
            cp.write(c.blob(rows))
        log("  metrics_jsonb %d/%d series" % (chunk_start + len(chunk), len(series)))


def load_schema_norm(cur, series, series_ids, timestamps):
    for chunk_start in range(0, len(series), SERIES_CHUNK):
        chunk = series[chunk_start : chunk_start + SERIES_CHUNK]
        block = v.generate_chunk(chunk)
        rows = []
        for row_index, desc in enumerate(chunk):
            sid = c.int4(series_ids[desc["id"]])
            values = block[row_index]
            for i in range(d.SAMPLES):
                rows.append(c.row(c.ts(int(timestamps[i])), sid, c.float8(values[i])))
        with cur.copy(
            "COPY metrics_norm_brin (time, series_id, value) FROM STDIN (FORMAT binary)"
        ) as cp:
            cp.write(c.blob(rows))
        log("  metrics_norm_brin %d/%d series" % (chunk_start + len(chunk), len(series)))


SCHEMAS = {
    "jsonb": {
        "table": "metrics_jsonb",
        "needs_dim": False,
    },
    "norm": {
        "table": "metrics_norm_brin",
        # series_dim belongs to the normalised design: the sample tables key on
        # its id rather than repeating labels. It is loaded with this schema so
        # that disk measured for the "norm" leg includes it.
        "needs_dim": True,
    },
}


def load_schema(cur, series, series_ids, timestamps, schema):
    """Load one schema's sample table, and series_dim when it owns one."""
    if schema == "jsonb":
        load_schema_jsonb(cur, series, timestamps)
    else:
        load_schema_norm(cur, series, series_ids, timestamps)


def ingest_postgres(series, t_start_ms, schemas):
    """Load `schemas` into an otherwise empty database.

    Loaded one schema per leg, never both into the same instance. Both in the
    same database makes disk unattributable: metrics_jsonb and metrics_norm_brin
    plus a shared series_dim arrive together, so a measurement for either leg
    is really a measurement of both. It also breaks the memory comparison, since
    shared_buffers stays warm from the first leg into the second.
    """
    timestamps = v.generate_timestamps(t_start_ms)
    results = []
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            log("ensuring partitions")
            ensure_partitions(cur, t_start_ms, d.t_end_ms(t_start_ms))

            series_ids = None
            prune_other_schemas(cur, schemas[0])
            if any(SCHEMAS[s]["needs_dim"] for s in schemas):
                log("populating series_dim")
                with conn.transaction():
                    series_ids = populate_series_dim(cur, series)
            for schema in schemas:
                spec = SCHEMAS[schema]
                log("loading %s" % spec["table"])
                started = time.monotonic()
                with conn.transaction():
                    load_schema(cur, series, series_ids, timestamps, schema)

            for schema in schemas:
                spec = SCHEMAS[schema]
                log("loading %s" % spec["table"])
                started = time.monotonic()
                with conn.transaction():
                    load_schema(cur, series, series_ids, timestamps, schema)
                elapsed = time.monotonic() - started
                total = len(series) * d.SAMPLES
                results.append(
                    {
                        "target": schema,
                        "table": spec["table"],
                        "samples": total,
                        "seconds": round(elapsed, 3),
                        "samples_per_second": round(total / elapsed, 1),
                    }
                )
                log(
                    "  %s: %d rows in %.1fs (%.0f rows/s)"
                    % (spec["table"], total, elapsed, total / elapsed)
                )
    return results


# ---------------------------------------------------------------------------

MANIFEST_PATH = None
previous_targets = set()


def scale_key():
    """Identifies a dataset shape. Two passes may share a time anchor only if
    they are generating the same shape."""
    return [d.JOBS, d.INSTANCES_PER_JOB, d.WINDOW_MINUTES,
            d.INTERVAL_SECONDS, d.SEED, d.JITTER_MS, d.AHEAD_MINUTES]


def fresh_anchor_ms():
    """Start of a window that sits AHEAD_MINUTES into the future.

    Future-dating is not a convenience, it is the only way in: the head refuses
    samples older than MaxTime - chunkRange/2, so a fresh TSDB has no history to
    append to. See dataset.py for the two bounds this has to fit between.
    """
    now = int(time.time() * 1000)
    return ((now + int(d.AHEAD_MINUTES * 60000))
            // (d.INTERVAL_SECONDS * 1000) * (d.INTERVAL_SECONDS * 1000)
            - (d.SAMPLES - 1) * d.INTERVAL_SECONDS * 1000)


def resolve_start_ms():
    """Anchor the window once, then reuse it for every later pass.

    The two targets are ingested by separate container invocations, so `now`
    moves between them. With a fresh anchor per pass, Prometheus and
    PostgreSQL hold the same samples at *different* timestamps -- the windows
    do not even overlap -- and parity, footprint and latency all compare
    nothing. Hence: read the anchor back from the manifest and reuse it.

    Returns (t_start_ms, reused).
    """
    global MANIFEST_PATH
    MANIFEST_PATH = os.path.join(RESULTS_DIR, "dataset.json")
    fresh = fresh_anchor_ms()
    if not os.path.exists(MANIFEST_PATH):
        return fresh, False
    try:
        with open(MANIFEST_PATH) as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return fresh, False
    if manifest.get("scale") != scale_key():
        log("existing dataset.json is a different shape; taking a new anchor")
        return fresh, False
    anchor = int(manifest["t_start_ms"])
    previous_targets.update(manifest.get("targets") or [])
    age_h = (time.time() * 1000 - anchor) / 3600000.0
    log("reusing anchor %d from dataset.json (%.2fh old)" % (anchor, age_h))
    if age_h > 1.0:
        log("WARNING: the anchor is more than an hour old. Prometheus only "
            "accepts samples newer than MaxTime - chunkRange/2 (~1h), so this "
            "pass will be rejected. Re-run with a fresh results/ directory.")
    return anchor, True


def write_manifest(series, t_start_ms, targets):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    manifest = {
        "t_start_ms": t_start_ms,
        "t_end_ms": d.t_end_ms(t_start_ms),
        "samples_per_series": d.SAMPLES,
        "samples": d.SAMPLES,
        "total_series": len(series),
        "series_gauge": len(series) // 2,
        "total_samples": len(series) * d.SAMPLES,
        "interval_seconds": d.INTERVAL_SECONDS,
        "jobs": d.JOBS,
        "instances_per_job": d.INSTANCES_PER_JOB,
        "seed": d.SEED,
        "jitter_ms": d.JITTER_MS,
        "scale": scale_key(),
        "targets": targets,
    }
    with open(MANIFEST_PATH or os.path.join(RESULTS_DIR, "dataset.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    log("wrote %s/dataset.json" % RESULTS_DIR)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=os.environ.get("TARGETS", "all"))
    # Which PostgreSQL schema to load. One per leg: both into the same instance
    # makes disk and memory unattributable to either leg.
    parser.add_argument("--schemas",
                        default=os.environ.get("PG_SCHEMAS", "jsonb,norm"),
                        help="comma-separated subset of: jsonb, norm")
    args = parser.parse_args()

    schemas = [s.strip() for s in args.schemas.split(",") if s.strip()]
    unknown = [s for s in schemas if s not in SCHEMAS]
    if unknown:
        parser.error("unknown schema(s): %s (known: %s)"
                     % (", ".join(unknown), ", ".join(SCHEMAS)))

    series = d.build_series()
    t_start_ms, reused = resolve_start_ms()

    log(
        "%d series x %d samples = %d samples, interval %ds, window %dh"
        % (len(series), d.SAMPLES, len(series) * d.SAMPLES, d.INTERVAL_SECONDS, d.WINDOW_MINUTES)
    )
    log("timestamps %d .. %d (future-dated to satisfy appendableMinValidTime)"
        % (t_start_ms, d.t_end_ms(t_start_ms)))

    summary = []
    if args.target in ("prometheus", "all"):
        summary.append(ingest_prometheus(series, t_start_ms))
    if args.target in ("postgres", "all"):
        summary.extend(ingest_postgres(series, t_start_ms, schemas))

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "ingest.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    write_manifest(series, t_start_ms,
                   sorted({entry["target"] for entry in summary}
                          | (previous_targets if reused else set())))

    for entry in summary:
        log("%s: %s samples in %ss = %s samples/s"
            % (entry["target"], entry["samples"], entry["seconds"], entry["samples_per_second"]))


if __name__ == "__main__":
    sys.exit(main())
