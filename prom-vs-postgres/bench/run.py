"""Orchestrate the benchmark: ingest, footprint, latency, resource.

    python3 bench/run.py up         start Prometheus and PostgreSQL
    python3 bench/run.py ingest     load both targets, sequentially
    python3 bench/run.py footprint  settled on-disk bytes, normalised per sample
    python3 bench/run.py query      latency percentiles for every shape
    python3 bench/run.py parity     both targets hold the same data
    python3 bench/run.py resource   CPU and memory sampled during the phases above
    python3 bench/run.py report     render results/report.md
    python3 bench/run.py all        everything, in order

Three measurements, each in its own phase, each writing results/ so a failure
in one does not discard the others:

  footprint  bytes/sample on disk, which is the invariant across scales. Raw
             byte totals are meaningless without also stating the sample count.
  latency    p50/p75/p95/p99 per query shape, warm, in bench/bench.py.
  resource   CPU and memory from `docker stats`, sampled around ingest and query.

Targets are never loaded concurrently: a shared 4-core VM would make the two
ingest numbers a measurement of the scheduler, not of either database.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import queries as Q  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")
PROM = os.environ.get("PROM_URL", "http://localhost:9090")

STACK = ["prometheus", "postgres"]

_COMPOSE_BINARY = None


def compose_binary():
    """`docker compose` (plugin) or `docker-compose` (standalone).

    Both spellings exist and not every machine has the plugin, so probe once.
    """
    global _COMPOSE_BINARY
    if _COMPOSE_BINARY is None:
        probe = subprocess.run(["docker", "compose", "version"],
                               capture_output=True, text=True)
        _COMPOSE_BINARY = ["docker", "compose"] if probe.returncode == 0 else ["docker-compose"]
    return _COMPOSE_BINARY


def compose(*args, **kwargs):
    """Thin docker compose wrapper. env= is forwarded so callers can override
    the interpolations declared in a service's environment block."""
    return subprocess.run(
        [*compose_binary(), *args],
        cwd=ROOT,
        check=kwargs.get("check", True),
        text=True,
        stdout=kwargs.get("stdout"),
        stderr=kwargs.get("stderr"),
        env=kwargs.get("env"),
    )


def log(message):
    print("[bench] %s" % message, flush=True)


def save(name, payload):
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, name)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1)
    log("wrote %s" % os.path.relpath(path, ROOT))
    return path


def load(name):
    path = os.path.join(RESULTS, name)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read()


def docker(args, timeout=120):
    result = subprocess.run(["docker", *args], capture_output=True, text=True,
                            timeout=timeout)
    return result


# --- resource sampling ----------------------------------------------------

def _parse_mem(text):
    """'1.234GiB / 3.9GiB' -> bytes."""
    raw = text.split("/")[0].strip()
    units = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3,
             "kB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}
    for suffix, mult in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if raw.endswith(suffix):
            try:
                return int(float(raw[: -len(suffix)]) * mult)
            except ValueError:
                return None
    return None


# Exact container names of the systems under test. Matched exactly, never by
# substring: the optional monitoring stack runs a container called
# `monitoring-prometheus`, and a substring match would silently report its CPU
# as Prometheus-under-test's. That would corrupt the headline resource numbers
# without failing anything.
TARGET_CONTAINERS = {"bench-prometheus": "prometheus", "bench-postgres": "postgres"}


def sample_stats():
    """Instantaneous CPU% and memory for the two systems under test."""
    result = docker(["stats", "--no-stream", "--format",
                     "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"])
    if result.returncode != 0:
        return {}
    stats = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, cpu_pct, mem = parts
        try:
            cpu = float(cpu_pct.rstrip("%"))
        except ValueError:
            continue
        key = TARGET_CONTAINERS.get(name)
        if key:
            stats[key] = {"cpu_pct": cpu, "mem_bytes": _parse_mem(mem)}
    return stats


class ResourceWindow:
    """Sample `docker stats` while a phase runs and keep the peak.

    CPU here is utilisation, not cumulative time: docker stats reports percent
    of one host CPU. Pair it with the wall time the phase already recorded to
    get CPU-seconds.
    """

    INTERVAL = 2.0

    def __enter__(self):
        self.samples = []
        self.started = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        # Sample on a timer. Collecting only at phase start and end would miss
        # the peak entirely, and the peak is the number that shows whether a
        # target fits its envelope.
        while not self._stop.is_set():
            got = sample_stats()
            if got:
                self.samples.append(got)
            self._stop.wait(self.INTERVAL)

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        self._sample_final = time.monotonic()
        self.seconds = self._sample_final - self.started
        self.peak = {}
        for backend in ("prometheus", "postgres"):
            cpus = [s[backend]["cpu_pct"] for s in self.samples if backend in s]
            mems = [s[backend]["mem_bytes"] for s in self.samples if backend in s]
            if not cpus:
                continue
            self.peak[backend] = {
                "cpu_pct_avg": round(sum(cpus) / len(cpus), 1),
                "cpu_pct_peak": round(max(cpus), 1),
                "mem_bytes_peak": max(m for m in mems if m) if any(mems) else None,
            }
        self.final = self._sample_final
        return False


# --- phases ---------------------------------------------------------------

def _image_digest(container):
    """Tag and image ID for a running container.

    `RepoDigests` is a field on the *image*, not the container, so indexing it
    here fails with "map has no entry for key". The container's `.Image` is the
    image ID, which is the part that actually pins the bytes. With `:latest`
    tags this is the only thing that makes a run reproducible.
    """
    result = docker(["inspect", "--format", "{{.Config.Image}} {{.Image}}",
                     container])
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    if len(parts) < 2:
        return None
    return {"tag": parts[0], "id": parts[1]}


def read_env_file():
    values = {}
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return values
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def phase_up():
    log("starting Prometheus and PostgreSQL")
    compose("up", "-d", "--wait", "--wait-timeout", "180", *STACK)
    log("Prometheus at %s" % PROM)

    env = read_env_file()
    save("environment.json", {
        "envelope": {
            "cpus_per_target": env.get("TARGET_CPUS"),
            "memory_per_target": env.get("TARGET_MEMORY"),
            "note": "identical budget for both systems; see docker-compose.yml",
        },
        "scale": {k: env.get(k) for k in
                  ("JOBS", "INSTANCES_PER_JOB", "INTERVAL_SECONDS",
                   "WINDOW_MINUTES", "AHEAD_MINUTES", "SEED")},
        "query": {k: env.get(k) for k in
                  ("QUERY_ITERATIONS", "QUERY_WARMUP", "QUERY_TIMEOUT_S")},
        "images": {
            "prometheus": _image_digest("bench-prometheus"),
            "postgres": _image_digest("bench-postgres"),
        },
    })
    return {"ready": True}


def phase_ingest():
    manifest = load("dataset.json") or {}
    per_target = []
    resource = {}
    for target in ("prometheus", "postgres"):
        log("ingesting into %s (nothing else running)" % target)
        with ResourceWindow() as window:
            env = dict(os.environ, INGEST_TARGETS=target)
            compose("--profile", "ingest", "run", "--rm", "bench",
                    "python", "/work/generator/generate.py", check=True, env=env)
        per_target = load("ingest.json") or []
        resource[target] = {"seconds": round(window.seconds, 2), **window.peak}
        log("  %s done in %.1fs" % (target, window.seconds))
    if manifest:
        log("window %s .. %s ms, %s samples"
            % (manifest.get("t_start_ms"), manifest.get("t_end_ms"),
               manifest.get("total_samples")))
    save("resource_ingest.json", resource)
    return {"per_target": per_target}


def _du(container, path):
    result = docker(["exec", container, "du", "-sb", path])
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def _tsdb_blocks():
    """How many blocks the head has actually been compacted into.

    Without this the bytes-per-sample figure is uninterpretable. A short window
    never crosses the 3h compactable() threshold, so Prometheus holds
    everything as head chunks and the number is head-only -- a real state, but
    not the settled steady state a long-running server would show.
    """
    try:
        _, body = http_get(PROM + "/api/v1/status/tsdb", timeout=30)
        return len(json.loads(body).get("data", {}).get("blocks", []))
    except Exception:
        return None


def _psql(sql):
    result = docker(["exec", "bench-postgres", "psql", "-U", "bench",
                     "-d", "bench", "-tAc", sql])
    return result.stdout.strip() if result.returncode == 0 else None


def _pg_data_dir():
    """Ask the server where its data directory is.

    Hardcoding /var/lib/postgresql/data is wrong on PostgreSQL 18+, which
    nests it under a version-specific subdirectory. Asking costs one query and
    survives the next layout change.
    """
    path = _psql("SHOW data_directory")
    return path if path else "/var/lib/postgresql/data"


def _pg_sizes():
    """Heap and index bytes per table, straight from the catalogue."""
    sql = (
        "SELECT c.relname,"
        " pg_table_size(c.oid), pg_indexes_size(c.oid)"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relkind = 'r'"
        " AND c.relispartition ORDER BY c.relname"
    )
    result = docker(["exec", "bench-postgres", "psql", "-U", "bench",
                     "-d", "bench", "-tAc", sql])
    if result.returncode != 0:
        log("pg_sizes failed: %s" % result.stderr.strip()[:200])
        return {}
    sizes = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        sizes[parts[0]] = {"heap_bytes": int(parts[1]), "index_bytes": int(parts[2])}
    return sizes


def phase_footprint():
    """Settled on-disk bytes, and the invariant: bytes per sample."""
    manifest = load("dataset.json") or {}
    samples = manifest.get("total_samples") or 0
    if not samples:
        log("no dataset.json; run ingest first")
        return {}

    pg_dir = _pg_data_dir()
    prom_total = _du("bench-prometheus", "/prometheus")
    prom_wal = _du("bench-prometheus", "/prometheus/wal")
    pg_total = _du("bench-postgres", pg_dir)
    pg_wal = _du("bench-postgres", os.path.join(pg_dir, "pg_wal"))

    def usable(total, wal):
        if total is None:
            return None
        return total - (wal or 0)

    pg_data = usable(pg_total, pg_wal)
    blocks = _tsdb_blocks()
    payload = {
        "total_samples": samples,
        "prometheus_blocks": blocks,
        "prometheus": {"bytes": usable(prom_total, prom_wal),
                       "wal_bytes": prom_wal, "raw_bytes": prom_total},
        "postgres": {"bytes": pg_data, "wal_bytes": pg_wal, "raw_bytes": pg_total},
        "tables": _pg_sizes(),
    }

    if payload["prometheus"]["bytes"]:
        payload["prometheus"]["bytes_per_sample"] = round(
            payload["prometheus"]["bytes"] / samples, 2)
    if pg_data:
        payload["postgres"]["bytes_per_sample"] = round(pg_data / samples, 2)
        if payload["prometheus"]["bytes"]:
            payload["ratio_prometheus_smaller"] = round(
                pg_data / payload["prometheus"]["bytes"], 1)

    save("footprint.json", payload)
    if payload["prometheus"].get("bytes_per_sample"):
        log("Prometheus %.2f B/sample, Postgres %.2f B/sample"
            % (payload["prometheus"]["bytes_per_sample"],
               payload["postgres"].get("bytes_per_sample", 0)))
    return payload


def phase_query():
    manifest = load("dataset.json") or {}
    log("running %d iterations x %d shapes x %d backends"
        % (int(os.environ.get("QUERY_ITERATIONS", "300")),
           len(Q.QUERIES), len(Q.BACKENDS)))
    with ResourceWindow() as window:
        result = compose("--profile", "bench", "run", "--rm", "bench",
                         "python", "/work/bench/bench.py", check=False)
    payload = {
        "seconds": round(window.seconds, 2),
        "peak": window.peak,
        "exit_code": result.returncode,
    }
    save("resource_query.json", payload)
    if result.returncode != 0:
        log("bench exited %d; partial results may exist" % result.returncode)
    return load("query.json") or payload


def phase_parity():
    log("checking both targets hold the same data")
    result = compose("--profile", "bench", "run", "--rm", "bench",
                     "python", "/work/bench/parity.py", check=False)
    if result.returncode != 0:
        log("parity exited %d" % result.returncode)
    return load("parity.json")


def phase_report():
    subprocess.run([sys.executable, os.path.join(ROOT, "bench", "report.py")],
                   check=False)
    return {"rendered": True}


PHASES = {
    "up": phase_up,
    "ingest": phase_ingest,
    "footprint": phase_footprint,
    "query": phase_query,
    "parity": phase_parity,
    "report": phase_report,
}

ORDER = ["up", "ingest", "footprint", "query", "parity", "report"]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    requested = sys.argv[1:]
    if requested == ["all"]:
        requested = ORDER
    for name in requested:
        if name not in PHASES:
            print("unknown phase %r; known: %s" % (name, ", ".join(ORDER)))
            return 1
        log("=== %s ===" % name)
        started = time.monotonic()
        PHASES[name]()
        log("=== %s done in %.1fs ===" % (name, time.monotonic() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
