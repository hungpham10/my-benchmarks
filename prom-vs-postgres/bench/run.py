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
        capture_output=kwargs.get("capture_output", False),
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
        self.series = []
        self.started = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        # Sample on a timer. Collecting only at phase start and end would miss
        # the peak entirely, and the peak is the number that shows whether a
        # target fits its envelope. Every sample is also appended to the raw
        # series, which is what makes a CPU/memory/disk curve plottable later.
        disk_every = 3  # du is the expensive call; ~6s at INTERVAL=2
        count = 0
        while not self._stop.is_set():
            got = sample_stats()
            if got:
                self.samples.append(got)
                entry = {"t": round(time.monotonic() - self.started, 2),
                         "containers": got}
                if count % disk_every == 0:
                    entry["prom_disk_bytes"] = _du("bench-prometheus", "/prometheus")
                    entry["pg_disk_bytes"] = _du(
                        "bench-postgres", _pg_data_dir())
                self.series.append(entry)
                _write_textfile(entry)
            count += 1
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

def _reset_textfile():
    try:
        os.makedirs(TEXTFILE_DIR, exist_ok=True)
        open(os.path.join(TEXTFILE_DIR, "bench.prom"), "w").close()
    except OSError:
        pass


# Where the textfile collector reads from. Alloy mounts this directory and
# scrapes it, which is how the numbers written below reach a dashboard.
TEXTFILE_DIR = os.path.join(ROOT, "monitoring", "textfile")


def _write_textfile(entry):
    """Expose this phase's CPU/memory/disk samples to the monitoring stack.

    Why not cAdvisor, which is already scraping: under Colima its
    container_fs_usage_bytes carries no container label and reports the host
    filesystem for every series, and its container CPU is documented as
    untrustworthy. A panel over those draws one 25GB line for both systems and
    it looks entirely plausible. These samples come from `docker stats` and
    `du`, which is the same source the headline resource figures in the report
    come from, so the dashboard and results/report.md cannot disagree.

    Rewritten in place on every sample rather than appended to, and with NO
    timestamp on each line.

    Both details are forced. The textfile collector rejects a file carrying
    client-side timestamps outright -- "contains unsupported client-side
    timestamps, skipping entire file" -- so an appended, timestamped file
    produces nothing at all rather than something slightly wrong. And with no
    timestamp it exposes one sample per series, the latest, stamped at scrape
    time; a curve therefore has to come from the file being rewritten in step
    with the scrape interval (5s in the Alloy config) rather than from keeping
    history in the file. The sample interval here is 2s, so each scrape lands
    on fresh data.
    """
    try:
        os.makedirs(TEXTFILE_DIR, exist_ok=True)
        lines = []
        for system in ("prometheus", "postgres"):
            stats = entry.get("containers", {}).get(system) or {}
            cpu = stats.get("cpu_pct")
            if cpu is not None:
                lines.append('bench_cpu_pct{system="%s"} %s' % (system, cpu))
            mem = stats.get("mem_bytes")
            if mem is not None:
                lines.append('bench_mem_bytes{system="%s"} %d' % (system, int(mem)))
        for system, field in (("prometheus", "prom_disk_bytes"),
                              ("postgres", "pg_disk_bytes")):
            value = entry.get(field)
            if value is not None:
                lines.append('bench_disk_bytes{system="%s"} %d'
                             % (system, int(value)))
        tmp = os.path.join(TEXTFILE_DIR, "bench.prom.tmp")
        with open(tmp, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, os.path.join(TEXTFILE_DIR, "bench.prom"))
    except OSError:
        # The dashboard is a convenience; losing it must never abort a phase.
        pass


# k6's own wording when a remote-write POST fails. Matched against the run's
# combined output: exit code is 0 either way, so this string is the only signal
# that the metrics went anywhere.
PUSH_FAILURE_MARKER = "Failed to send the time series data to the endpoint"


def _container_running(container):
    """True when the named container is up.

    Used to decide whether k6 has a live remote-write target. A remote_write
    output pointed at a dead host does not fail fast; it retries, so the
    scenario appears to hang instead of reporting that nothing was pushed.
    """
    result = docker(["inspect", "--format", "{{.State.Running}}", container])
    return result.returncode == 0 and result.stdout.strip() == "true"


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


def read_env_file(name=".env"):
    """Parse a simple KEY=VALUE file, ignoring comments and blank lines.

    `name` is relative to the benchmark root: `.env` for the scale knobs,
    `monitoring/.env.monitoring` for the Grafana Cloud credentials.
    """
    values = {}
    path = os.path.join(ROOT, name)
    if not os.path.exists(path):
        return values
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                # Docker Compose accepts quoted values and the Grafana Cloud UI
                # hands them out quoted. Strip them, or a derived URL keeps the
                # quotes and the remote_write request fails with a URL that
                # looks correct in the file and is not.
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
    return values


def save_series(name, window):
    """Dump a phase's raw samples so the CPU/memory/disk curve survives.

    results/resource_*.json keeps only the peak, which cannot show the shape
    of a run: a container that ramped to 2 GB and dropped looks identical to
    one that sat at 2 GB. The series file is the evidence for that distinction.
    """
    path = os.path.join(RESULTS, name)
    os.makedirs(RESULTS, exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"seconds": round(window.seconds, 2),
                   "samples": window.series}, fh, indent=1)
    log("wrote %s" % os.path.relpath(path, ROOT))


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
    _reset_textfile()
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
        save_series("resource_ingest_%s_series.json" % target, window)
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
    _reset_textfile()
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
    save_series("resource_query_series.json", window)
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


def write_queries_manifest():
    """Export the query shapes to JSON for the k6 script.

    queries.py stays the single source of truth; k6 only reads this file.
    Placeholders are inlined because the xk6-sql Postgres driver does not
    accept psycopg3's %s parameters: the two `%s` placeholders become literal
    ISO timestamps from the dataset manifest.
    """
    manifest = load("dataset.json") or {}
    start_ms = int(manifest["t_start_ms"])
    end_ms = int(manifest["t_end_ms"])

    def inline(sql_text, name):
        if name == "q5_short_range":
            end_ms_local = start_ms + Q.SHORT_RANGE_MS
        else:
            end_ms_local = end_ms
        text = sql_text
        # Quote every placeholder first, then substitute in order, so the
        # second pass cannot accidentally re-match an ISO literal.
        text = text.replace("%s::timestamptz", "'%s'::timestamptz")
        bounds = (start_ms, end_ms_local)
        for ms in bounds:
            iso = time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime(ms / 1000.0))
            text = text.replace("%s", iso, 1)
        return text

    payload = {
        "start_s": start_ms / 1000.0,
        "end_s": end_ms / 1000.0,
        "step_seconds": Q.STEP_SECONDS,
        "queries": [
            {"name": q["name"], "promql": q["promql"],
             "sql": {name: inline(sql, q["name"])
                     for name, sql in q["sql"].items()}}
            for q in Q.QUERIES
        ],
    }
    path = os.path.join(RESULTS, "queries.json")
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1)
    log("wrote %s" % os.path.relpath(path, ROOT))


def phase_k6():
    _reset_textfile()
    """Concurrency load, one scenario at a time, CPU/memory/disk logged.

    The scenario matrix is query shape x backend x VUS level. Each cell is a
    separate k6 run with its own --duration, so its Trend series lands in its
    own summary JSON and its own resource series file. VUS above 1 is what the
    serial Python loop could never measure: queueing under concurrency.
    """
    write_queries_manifest()
    duration = os.environ.get("K6_DURATION", "180s")
    vus_levels = os.environ.get("K6_VUS", "1,10").split(",")
    out_dir = os.path.join(RESULTS, "k6")
    os.makedirs(out_dir, exist_ok=True)

    # `or ""` -- never None. With no credentials and no K6_CLOUD_RW_URL this
    # ends up None, and None goes into the environment dict handed to
    # subprocess, which rejects it: "expected str, bytes or os.PathLike object,
    # not NoneType". That is the pull_request run, which is the one case with
    # no secrets, so it is the case that reached CI first.
    cloud_url = os.environ.get("K6_CLOUD_RW_URL") or ""
    if not cloud_url:
        # Derive from the same Grafana Cloud credentials alloy uses, so there
        # is one place to fill in. K6_CLOUD_RW_URL overrides the derivation.
        values = read_env_file("monitoring/.env.monitoring")
        url = os.environ.get("GRAFANA_CLOUD_URL") or values.get("GRAFANA_CLOUD_URL")
        cid = os.environ.get("GRAFANA_CLOUD_ID") or values.get("GRAFANA_CLOUD_ID")
        token = os.environ.get("GRAFANA_CLOUD_TOKEN") or values.get("GRAFANA_CLOUD_TOKEN")
        cloud_url = values.get("K6_CLOUD_RW_URL") or ""
        if not cloud_url and url and cid and token:
            cloud_url = url.replace("https://", "https://%s:%s@" % (cid, token), 1)

    # k6's remote-write output takes the destination from
    # K6_PROMETHEUS_RW_SERVER_URL and ignores a URL appended to the --out flag.
    # It supports exactly one such output, so a run has to choose.
    #
    # Grafana Cloud wins when credentials exist, because it is the only
    # destination that outlives the run. Alloy forwards the operational metrics
    # it scrapes to both local and Cloud, but nothing re-reads the local
    # monitoring TSDB and forwards it on: series that k6 writes into
    # monitoring-prometheus stay there, so a Cloud-only run that prefers local
    # produces load-test numbers nobody can see once the runner is gone.
    #
    # Never leave the destination unset: with the output enabled and no env,
    # k6 falls back to http://localhost:9090/api/v1/write -- which from the
    # runner is the Prometheus UNDER TEST, the very TSDB whose bytes/sample
    # this benchmark reports. Writing k6's own metrics into it would move that
    # headline number. So: only enable the output when there is a real
    # destination, and always set the env explicitly.
    push_url = cloud_url
    if push_url:
        log("k6 metrics will be pushed to Grafana Cloud")
    elif _container_running("bench-monitoring-prometheus"):
        push_url = "http://monitoring-prometheus:9090/api/v1/write"
        log("k6 metrics will be pushed to the local monitoring TSDB")
    else:
        log("no Grafana Cloud credentials and no local monitoring TSDB; "
            "k6 will not push its metrics anywhere")
    outs = ["--out", "experimental-prometheus-rw"] if push_url else []

    results = []
    for query in Q.QUERIES:
        for backend in Q.BACKENDS:
            for vus in vus_levels:
                name = "%s_%s_vus%s" % (query["name"], backend, vus)
                log("k6 scenario %s, %s" % (name, duration))
                args = (["run", "--vus", vus, "--duration", duration,
                         "--summary-export", os.path.join("/results", "k6",
                                                        "summary_%s.json" % name),
                         *outs, "/scripts/main.js"])
                with ResourceWindow() as window:
                    env = dict(os.environ, K6_BACKEND=backend,
                               K6_QUERY=query["name"],
                               K6_PROMETHEUS_RW_SERVER_URL=push_url)
                    result = compose("--profile", "k6", "run", "--rm", "--no-deps",
                                     "k6", *args, check=False, env=env,
                                     capture_output=True)
                # k6 exits 0 whether or not the remote-write flush succeeded,
                # and says so only on stderr. A failed push is invisible in the
                # exit code, so it has to be read out of the log or the load
                # numbers are simply lost. The Grafana Cloud token here has
                # push scope only and cannot be queried to confirm, so this log
                # line is the only evidence available.
                output = (result.stdout or "") + (result.stderr or "")
                push_failed = PUSH_FAILURE_MARKER in output
                if push_failed and push_url:
                    log("  PUSH FAILED: k6 could not write to %s" % push_url)
                save_series("k6_%s_series.json" % name, window)
                results.append({"scenario": name, "exit_code": result.returncode,
                                "push_failed": push_failed,
                                "push_url": push_url if push_url else None,
                                "seconds": round(window.seconds, 2)})
                log("  exit %d in %.1fs" % (result.returncode, window.seconds))
    failed_pushes = [r for r in results if r["push_failed"]]
    if failed_pushes:
        # Loud, because every scenario still exits 0 and the report renders
        # percentiles that look complete. They are complete -- locally.
        log("WARNING: %d/%d scenarios could not push their metrics to %s"
            % (len(failed_pushes), len(results), push_url))
    save("k6_scenarios.json", results)
    return results


def _k6_concurrent_env(push_url):
    """The env a concurrent k6 run needs, with the same push rules as phase_k6."""
    return dict(os.environ, K6_PROMETHEUS_RW_SERVER_URL=push_url)


def _parse_k6_summary(relative_path):
    """Pull the per-target TPS and latency out of one k6 summary export.

    `relative_path` is relative to the benchmark root, not to RESULTS: the
    file is written into the results/ directory, which the container sees
    mounted at /results.
    """
    path = os.path.join(ROOT, relative_path)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        metrics = json.load(fh).get("metrics", {})

    # k6 writes counters as {"rate": .., "count": ..} and trends as
    # {"min":..,"med":..,"p(90)":..,"p(95)":..,"max":..}, both at the top level
    # of the entry. Older exports wrapped the same fields in "values"; accept
    # that too so a file written by a different k6 still parses.
    def entry(name):
        raw = metrics.get(name)
        if raw is None:
            return {}
        return raw.get("values", raw) or {}

    def counter(name):
        return float(entry(name).get("rate", 0))

    def trend(name):
        values = entry(name)
        return {k: values.get(k) for k in ("p(90)", "p(95)", "max")}

    return {
        "prometheus_tps": counter("prom_requests"),
        "postgres_tps": counter("pg_requests"),
        "prometheus": trend("prom_query_duration_ms"),
        "postgres": trend("pg_query_duration_ms"),
    }


def phase_saturation():
    _reset_textfile()
    """Walk both systems up together until throughput stops responding to load.

    A TPS number is meaningless without the load that produced it. The point of
    this phase is the knee: the last step where adding virtual users still buys
    throughput, after which the extra users only add latency. That is where the
    two systems can be compared on capacity rather than on a single unstated
    load, and it is the only reason TPS is worth reporting at all.

    Both systems are driven in ONE k6 run, two scenarios, same VU level on
    both arms. Running them in sequence would mean comparing different instants
    and letting the second one start against a warmer or colder system than the
    first.

    This measures something different from phase_query, and the two must not be
    compared with each other. phase_query asks which system is faster with
    nothing else running; this asks what each does when both are loaded. The
    numbers are only valid as long as the host can serve both: the envelope in
    .env gives 1.5 CPU each, and a 4-core host has one core left for the
    runner and the OS. If that remainder is ever the bottleneck, these figures
    are measuring the host, not the databases.
    """
    write_queries_manifest()
    steps = [int(v) for v in os.environ.get(
        "SATURATION_VUS", "1,2,4,8,16,32").split(",") if v.strip()]
    duration = os.environ.get("SATURATION_DURATION", "30s")
    queries = [q["name"] for q in Q.QUERIES
               if q["name"] in os.environ.get("SATURATION_QUERIES", "").split(",")] \
        or [Q.QUERIES[0]["name"]]
    backend = os.environ.get("SATURATION_PG_BACKEND", "metrics_norm_brin")
    cloud_url = os.environ.get("K6_CLOUD_RW_URL") or ""
    if not cloud_url:
        values = read_env_file("monitoring/.env.monitoring")
        url = values.get("GRAFANA_CLOUD_URL")
        cid = values.get("GRAFANA_CLOUD_ID")
        token = values.get("GRAFANA_CLOUD_TOKEN")
        cloud_url = values.get("K6_CLOUD_RW_URL") or ""
        if not cloud_url and url and cid and token:
            cloud_url = url.replace("https://", "https://%s:%s@" % (cid, token), 1)
    push_url = cloud_url or (
        "http://monitoring-prometheus:9090/api/v1/write"
        if _container_running("bench-monitoring-prometheus") else "")
    outs = ["--out", "experimental-prometheus-rw"] if push_url else []

    out_dir = os.path.join(RESULTS, "k6")
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for query_name in queries:
        curve = []
        for vus in steps:
            label = "%s_vus%d" % (query_name, vus)
            log("saturation %s" % label)
            summary = os.path.join("/results", "k6", "saturation_%s.json" % label)
            with ResourceWindow() as window:
                env = _k6_concurrent_env(push_url)
                env.update({
                    "BACKEND": backend, "QUERY_NAME": query_name,
                    "CONCURRENT_VUS": str(vus), "CONCURRENT_DURATION": duration,
                    "BENCH_RUN_ID": os.environ.get("BENCH_RUN_ID", ""),
                    "BENCH_COMMIT": os.environ.get("BENCH_COMMIT", ""),
                })
                compose("--profile", "k6", "run", "--rm", "--no-deps", "k6",
                        "run", "--summary-export", summary, *outs,
                        "/scripts/concurrent.js", check=False, env=env)
            entry = _parse_k6_summary(os.path.join("results", "k6",
                                                   os.path.basename(summary)))
            save_series("k6_saturation_%s_series.json" % label, window)
            if entry:
                entry.update({"query": query_name, "vus": vus,
                              "wall_seconds": round(window.seconds, 2)})
                curve.append(entry)
                log("  prom %.0f req/s (p95 %s ms) | pg %.0f req/s (p95 %s ms)"
                    % (entry["prometheus_tps"],
                       entry["prometheus"]["p(95)"],
                       entry["postgres_tps"],
                       entry["postgres"]["p(95)"]))
        results.append({"query": query_name, "backend": backend,
                        "duration": duration, "curve": curve,
                        "knee": _saturation_knee(curve)})
    save("k6_saturation.json", results)
    return results


def _saturation_knee(curve):
    """The last step where extra VUs still bought throughput.

    Found from the Prometheus arm, which is the faster of the two and so
    reaches its ceiling first; reporting one knee rather than two keeps the
    question simple -- after which point does adding load stop helping.

    Returns None when the curve never turns over, which is a real answer at
    small scale: if throughput is still climbing at the last step, the knee is
    above the range measured and the honest report is "not reached".
    """
    if len(curve) < 2:
        return None
    for earlier, later in zip(curve, curve[1:]):
        if later["prometheus_tps"] <= earlier["prometheus_tps"]:
            return earlier["vus"]
    return None


PHASES = {
    "up": phase_up,
    "ingest": phase_ingest,
    "footprint": phase_footprint,
    "query": phase_query,
    "k6": phase_k6,
    "saturation": phase_saturation,
    "parity": phase_parity,
    "report": phase_report,
}

ORDER = ["up", "ingest", "footprint", "query", "k6", "saturation",
         "parity", "report"]


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
