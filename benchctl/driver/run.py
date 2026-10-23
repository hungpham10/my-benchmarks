"""driver/run.py — the ONLY thing that generates text: plan.json + compose.

Usage: python driver/run.py scenarios/prom-load.yaml
Pipeline per system: up -> init -> du disk -> k6 (ramp) -> verify -> down.

Systems are "module" or "module/variant" (e.g. postgres/jsonb). The module
directory holds everything module-specific; this file only orchestrates.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

try:
    import yaml
except ImportError:
    sys.exit("need pyyaml: pip install pyyaml")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")
SHARED = os.path.join(ROOT, "shared")

sys.path.insert(0, SHARED)
import fssetup  # noqa: E402 — provision the filesystem under the data dir
from leg import leg_slug, extra_label  # noqa: E402 — one name for a leg, shared with k6

COMPOSE = None

# Set in main() from dataset.json so k6 queries the future-dated window
# instead of wall-clock now() (which returns empty for Prometheus data).
_K6_QUERY_TIME = None


def compose_bin():
    """This machine only has hyphenated docker-compose; CI may have either."""
    global COMPOSE
    if COMPOSE is None:
        r = subprocess.run(["docker", "compose", "version"],
                           capture_output=True)
        COMPOSE = (["docker", "compose"] if r.returncode == 0
                   else ["docker-compose"])
    return COMPOSE


def sh(*args, env=None, check=True):
    e = dict(os.environ)
    if env:
        e.update(env)
    if args[:2] == ("docker", "compose"):
        args = tuple(compose_bin()) + tuple(args[2:])
    return subprocess.run(args, cwd=ROOT, env=e, check=check)


def _ts_literal(ms):
    """A quoted timestamptz literal for SQL.

    xk6-sql exposes conn.query(sql) with no bind parameters, so the driver has
    to substitute values into the SQL text. Emitting a bare float instead gives
    a numeric literal, and PostgreSQL removed the numeric->timestamptz cast,
    so `%s::timestamptz` fails with 42846. Returning an ISO8601 string wrapped
    in single quotes makes `time BETWEEN '...'::timestamptz AND '...'::timestamptz`
    which is valid.
    """
    return "'" + datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat() + "'"


def load_cfg():
    import re
    text = open(os.path.join(ROOT, "benchctl.yaml")).read()

    def sub(m):
        var, _, default = m.group(1).partition(":-")
        return os.environ.get(var, default)
    return yaml.safe_load(re.sub(r"\$\{([^}]+)\}", sub, text))


def expand(text):
    """{VAR} in module.yaml resolves against the process environment."""
    import re

    def sub(m):
        return os.environ.get(m.group(1), "")
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, text)


def split_system(entry):
    """'postgres/jsonb' -> ('postgres', 'jsonb'); 'prometheus' -> ('prometheus', None)."""
    if "/" in entry:
        return tuple(entry.split("/", 1))
    return entry, None


# Read-contract aliases. A variant whose read side keys off a VIEW instead
# of a catalog table maps here: the catalog SQL is reused with the base
# table's name swapped for the alias.
TABLE_ALIASES = {"events_all": "metrics_norm_brin"}


def render_query(system, scenario):
    """The query for this leg. A plain string is used as-is (promql); a
    {query, table} mapping is rendered from the old query catalog with the
    time window this leg's data was loaded into (sql).

    If the scenario has no 'queries' key (e.g. smoke scenario), return the
    system name as a marker string — the verify check will still compare
    client vs server counts, just without SQL filtering.
    """
    queries = scenario.get("queries")
    if not queries:
        return system  # no SQL filtering; verify will just compare totals
    spec = queries[system]
    if isinstance(spec, str):
        return spec
    # The queries module is in shared/, not generator/
    sys.path.insert(0, SHARED)
    import queries as Q
    manifest = json.load(open(os.path.join(RESULTS, "dataset.json")))
    for q in Q.QUERIES:
        if q["name"] == spec["query"]:
            tbl = spec["table"]
            sql = q["sql"].get(tbl)
            if sql is None:
                base = TABLE_ALIASES.get(tbl)
                if base and base in q["sql"]:
                    sql = q["sql"][base].replace(base, tbl)
            if sql is None:
                raise RuntimeError(
                    f"query {spec['query']} has no SQL for table {tbl}")
            out = sql.replace("%s", _ts_literal(manifest["t_start_ms"]), 1)
            out = out.replace("%s", _ts_literal(manifest["t_end_ms"]), 1)
            assert "%s" not in out, "unrendered placeholder in " + spec["query"]
            return " ".join(out.split())
    raise RuntimeError("unknown query: " + spec["query"])


def gen_plan(system, scenario, cfg, mod_cfg, fs_type=None, extra=None):
    # maxVUs bounds client-side concurrency. It is a resource-envelope decision
    # (benchctl.yaml k6_max_vus), not a module's business. A module may lower it
    # via k6.max_vus. Framework passes the final value to plan.load.max_vus.
    load = dict(scenario["load"])
    env_max_vus = cfg["envelope"].get("k6_max_vus", 500)
    mod_max_vus = mod_cfg.get("k6", {}).get("max_vus")
    if mod_max_vus is not None:
        load["max_vus"] = min(env_max_vus, mod_max_vus)
    else:
        load["max_vus"] = env_max_vus
    # Pool sizing legs (conns) cap the VU budget to P * conns for pool
    # legs (P schemas), and to 1 * conns for table-suffix legs. The cap
    # IS the measurement: saturation happens at the pool, not arrivals.
    if extra and extra.get("conns"):
        cap = int(extra.get("pools", 1)) * int(extra["conns"])
        load["max_vus"] = min(load["max_vus"], cap)

    return {
        "scenario": scenario["id"],
        "system": system,
        "fs": fs_type or "",
                   "extra": extra_label(extra),
        "extra": extra_label(extra),
        "load": load,
        "thresholds": scenario["thresholds"],
        "query": render_query(system, scenario),
        "query_marker": mod_cfg.get("query_marker", ""),
        "run_id": cfg["run"]["run_id"],
    }


def gen_compose(module, cfg, mod_cfg, system, scenario, fs_state=None, extra=None):
    env = cfg["envelope"]
    dep = mod_cfg["deploy"]
    volumes = []
    for f in dep.get("config_files", []):
        volumes.append(f"./modules/{module}/{f['src']}:{f['dst']}:ro")
    dv = dep.get("data_volume")
    if dv:
        if fs_state:
            # The filesystem under test lives on the daemon's machine. The
            # container bind-mounts a directory inside it, so the container
            # sees the filesystem's behaviour and not a volume on the host's
            # own filesystem.
            volumes.append(f"{fs_state['data_dir']}:{dv['dst']}")
        else:
            volumes.append(f"{dv['name']}:{dv['dst']}")
    ports = []
    for p in dep.get("ports", []):
        host = os.environ.get(p["env"], p["default"])
        ports.append(f"{host}:{p['container']}")
    sut = {
        "image": mod_cfg["image"],
        "container_name": "benchctl-sut",
        "deploy": {"resources": {"limits": {
            "cpus": env["cpus"], "memory": env["memory"]}}},
        "networks": ["mon"],
    }
    if dep.get("command"):
        sut["command"] = dep["command"]
    # A setting whose context is postmaster (wal_buffers) cannot be changed
    # with ALTER SYSTEM + reload: the server ignores the new value until a
    # restart, and the sweep would record "16MB" while believing it measured
    # 64MB. Such a leg has to arrive on the command line, and the driver
    # recreates the container per leg, so this is where it goes.
    if extra and extra.get("arg_wb"):
        sut["command"] = list(sut.get("command") or ["postgres"]) + [
            "-c", f"wal_buffers={extra['arg_wb']}"]
    if ports:
        sut["ports"] = ports
    if volumes:
        sut["volumes"] = volumes
    if dep.get("environment"):
        sut["environment"] = {k: expand(v)
                              for k, v in dep["environment"].items()}
    if dep.get("healthcheck"):
        sut["healthcheck"] = dep["healthcheck"]
    if dep.get("shm_size"):
        # Docker's /dev/shm is 64 MB by default and counts against the
        # container's memory limit.
        sut["shm_size"] = dep["shm_size"]

    k6cfg = mod_cfg.get("k6", {})
    k6env = {
        "K6_PROMETHEUS_RW_SERVER_URL": cfg["cloud"]["remote_write_url"],
        "K6_PROMETHEUS_RW_USERNAME": cfg["cloud"]["username"],
        "K6_PROMETHEUS_RW_PASSWORD": cfg["cloud"]["password"],
        "K6_PROMETHEUS_RW_FLUSH_INTERVAL": "3s",
    }
    # Pool size for postgres modules (env var consumed by step.js)
    if module == "postgres":
        k6env["PG_MAX_CONN"] = "32"
    # Pass the rendered query as an environment variable for reliability
    query = render_query(system, scenario)
    if isinstance(query, str):
        k6env["K6_QUERY"] = query
    # Pass the query time so k6 hits the future-dated window, not wall-clock now()
    if _K6_QUERY_TIME:
        k6env["K6_QUERY_TIME"] = _K6_QUERY_TIME
    for k, v in k6cfg.get("env", {}).items():
        k6env[k] = expand(v)
    # Leg dimensions flow to the k6 side too: the userdb insert step routes
    # every transaction by LAYOUT/MODE/SHARDS, which are leg keys, not
    # module constants.
    for k, v in (extra or {}).items():
        k6env[str(k).upper()] = str(v)
    k6svc = {
        "profiles": ["k6"],
        "image": "benchctl-k6",
        "deploy": {"resources": {"limits": {
            "cpus": env["k6_cpus"], "memory": env["k6_memory"]}}},
        "environment": k6env,
        "volumes": ["./results:/results", "./load:/scripts:ro",
                    "./plan.json:/plan.json:ro",
                    "./modules:/modules:ro"],
        # Same network as the SUT. Without this, k6 lands on the compose
        # default network while the SUT is only on benchctl-mon, and every
        # request dies with status 0 — fast enough (connection refused) to
        # look like a real latency curve. The server_requests check caught
        # this: 69 sent, 0 handled.
        "networks": ["mon"],
        "depends_on": ["sut"],
    }

    vols = {}
    if dv and not fs_state:
        vols[dv["name"]] = {}
    return {
        "services": {"sut": sut, "k6": k6svc},
        "volumes": vols,
        "networks": {"mon": {"name": "benchctl-mon", "external": True}},
    }


def disk_bytes_du(path):
    """du inside the SUT container: the honest on-disk figure.

    Metric-based sizes lie by omission (blocks-bytes reads 0 on a fresh
    head). du sees everything.
    """
    e = dict(os.environ)
    p = subprocess.run([*compose_bin(), "-f", "docker-compose.gen.yml",
                        "exec", "-T", "sut", "du", "-sb", path],
                       cwd=ROOT, env=e, capture_output=True, text=True)
    # output looks like "123456\t/path"
    return int(p.stdout.strip().split()[0])


def fsync_bench(disk_path, seconds=3):
    """Measure fsync latency on the filesystem under the data directory.

    pg_test_fsync is PostgreSQL's own fsync benchmark and ships with the
    image. It writes and fsyncs a file inside the data directory, so it
    measures the filesystem the database actually lives on — not a scratch
    file somewhere else on the host.

    The rows that matter for a database:
      - fdatasync, one 8kB write — what PostgreSQL does on Linux by default
        (wal_sync_method=fdatasync), so this is the number its WAL pays.
      - fsync, one 8kB write — the full barrier.
      - "write, fsync, close" — the shape a database commit has.

    Everything else in the output is kept in the results file, because the
    differences between the rows are the interesting part.
    """
    import re
    target = f"{disk_path}/pg_test_fsync.tmp"
    p = subprocess.run(
        [*compose_bin(), "-f", "docker-compose.gen.yml",
         "exec", "-T", "sut", "pg_test_fsync", "-f", target, "-s", str(seconds)],
        cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        return {"available": False,
                "error": (p.stderr or p.stdout).strip()[-300:]}

    # Rows look like:
    #   fdatasync          5247.465 ops/sec     191 usecs/op
    row_re = re.compile(
        r"^(?P<label>.+?)\s+(?P<ops>[0-9.]+) ops/sec\s+(?P<us>[0-9.]+) usecs/op")
    section = ""
    rows = []
    for line in p.stdout.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.endswith(":") and "ops/sec" not in s:
            section = s.rstrip(":")
            continue
        m = row_re.match(s)
        if m:
            rows.append({"label": m.group("label").strip(),
                         "section": section,
                         "ops_per_s": float(m.group("ops")),
                         "us_per_op": float(m.group("us"))})
    if not rows:
        return {"available": False, "error": "unparseable pg_test_fsync output",
                "raw": p.stdout.strip()[-300:]}

    def pick(label, section_hint=None):
        for r in rows:
            if r["label"] != label:
                continue
            if section_hint and section_hint not in r["section"]:
                continue
            return r
        return None

    one_write = "one 8kB write"
    fdatasync = pick("fdatasync", one_write)
    fsync = pick("fsync", one_write)
    wfc = pick("write, fsync, close")

    out = {
        "available": True,
        "seconds": seconds,
        "o_direct": "O_DIRECT supported" in p.stdout,
        "rows": rows,
    }
    if fdatasync:
        out["fdatasync_us"] = fdatasync["us_per_op"]
        out["fdatasync_ops_per_s"] = fdatasync["ops_per_s"]
    if fsync:
        out["fsync_us"] = fsync["us_per_op"]
        out["fsync_ops_per_s"] = fsync["ops_per_s"]
    if wfc:
        out["write_fsync_close_us"] = wfc["us_per_op"]
    return out


def collect_io(module, env):
    """pg_stat_io snapshot from the SUT.

    Non-fatal on purpose: a PostgreSQL without pg_stat_io, or without
    fsync_time (which arrived in PG 17), must not take the whole run down.
    The report then says "not available" instead of quietly omitting the
    file, which is the difference between a gap and a lie.
    """
    out = subprocess.run(
        [sys.executable, f"modules/{module}/observe.py", "io"],
        cwd=ROOT, env={**os.environ, **env},
        capture_output=True, text=True)
    if out.returncode != 0:
        return {"available": False, "error": out.stderr.strip()[-500:]}
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"available": False, "error": "unparseable io output"}


def start_monitoring(module_name):
    """Static stack, outside the measured envelope — or nothing at all.

    cAdvisor + Alloy pushing straight to Cloud. Skipped entirely when
    GRAFANA_CLOUD_RW_URL is empty: an Alloy with no receiver is a crash loop,
    not monitoring, and the local summary.json stays the record either way.

    The network is created either way. The compose file declares it as
    external, so without it the SUT never starts — and a fork pull request
    has no Cloud URL, which is exactly when the run must still work.
    """
    sh("docker", "network", "create", "benchctl-mon", check=False)
    if not os.environ.get("GRAFANA_CLOUD_RW_URL"):
        print("[benchctl] monitoring skipped (no Cloud URL)", flush=True)
        return False
    env = {
        "BENCH_RUN_ID": os.environ.get("BENCH_RUN_ID", "local"),
        "BENCH_MODULE": module_name,
    }
    print("[benchctl] monitoring up (Cloud push)", flush=True)
    sh("docker", "compose", "-f", "monitoring/compose.monitoring.yml",
       "up", "-d", "--wait", "--wait-timeout", "60",
       env={**os.environ, **env})
    return True


def stop_monitoring():
    sh("docker", "compose", "-f", "monitoring/compose.monitoring.yml",
       "down", "-v", check=False)
    print("[benchctl] monitoring down", flush=True)


_K6_IMAGE_BUILT = False


def build_k6_image():
    """Build the single k6 image with xk6-sql + postgres driver.

    Called once before the first system that needs it.
    """
    global _K6_IMAGE_BUILT
    if _K6_IMAGE_BUILT:
        return
    print("[benchctl] building k6 image (with xk6-sql + postgres driver)", flush=True)
    sh("docker", "build", "-t", "benchctl-k6",
       "-f", "load/Dockerfile.k6", "load")
    _K6_IMAGE_BUILT = True


def main():
    scen_path = sys.argv[1]
    os.environ["BENCHCTL_SCENARIO"] = scen_path
    scenario = yaml.safe_load(open(scen_path))
    cfg = load_cfg()
    # A scenario may widen the envelope. The sharding scenario does: the
    # question it asks — how many parallel table scans are worth it for one
    # instance — has no answer on a single CPU. The override is recorded in
    # the results, because "same envelope" is a claim the report has to be
    # able to check.
    if scenario.get("envelope"):
        cfg["envelope"] = {**cfg["envelope"], **scenario["envelope"]}
    os.makedirs(RESULTS, exist_ok=True)
    run_id = cfg["run"]["run_id"]
    ds = scenario["dataset"]

    # ---- Generate dataset.json ONCE with shared time anchor ----
    # This ensures ALL modules use identical timestamps for parity.
    # The window is centred slightly ahead of wall clock so both Prometheus
    # bounds (floor: -1h, ceiling: +10m) have slack.
    import time
    interval_s = ds.get("interval_seconds", 15)
    window_min = ds["window_minutes"]
    ahead_min = ds.get("ahead_minutes", 8)
    global _K6_QUERY_TIME
    now_ms = int(time.time() * 1000)
    t_start_ms = now_ms - (window_min - ahead_min) * 60 * 1000
    t_end_ms = t_start_ms + window_min * 60 * 1000
    _K6_QUERY_TIME = str(t_end_ms // 1000)
    total_series = ds["jobs"] * ds["instances_per_job"] * 2
    total_samples = total_series * (window_min * 60 // interval_s)
    manifest = {
        "t_start_ms": t_start_ms,
        "t_end_ms": t_end_ms,
        "total_samples": total_samples,
        "interval_seconds": interval_s,
        "window_minutes": window_min,
        "ahead_minutes": ahead_min,
        "seed": ds.get("seed", 20240101),
        "targets": scenario["systems"],
    }
    json.dump(manifest, open(os.path.join(RESULTS, "dataset.json"), "w"), indent=1)
    print(f"[benchctl] wrote dataset.json (t_start={t_start_ms}, t_end={t_end_ms}, samples={total_samples})", flush=True)

    # Report manifest: provenance for render_manifest(). dataset.json keeps the
    # time anchor; manifest.json keeps scenario-level metadata for the report.
    report_manifest = {
        "scenario": os.path.basename(scen_path),
        "run_id": run_id,
        "total_samples": total_samples,
        "interval_seconds": interval_s,
        "window_minutes": window_min,
    }
    json.dump(report_manifest, open(os.path.join(RESULTS, "manifest.json"), "w"), indent=1)

    # Build k6 image ONCE for all systems (shared image with xk6-sql)
    build_k6_image()

    # Start monitoring ONCE before any SUT (creates benchctl-mon network)
    monitoring_started = start_monitoring(scenario["systems"][0])

    # A leg is one (system, filesystem) pair. Without a `filesystems` key in
    # the scenario this is exactly the old behaviour: one leg per system.
    filesystems = scenario.get("filesystems") or []
    if filesystems:
        print(f"[benchctl] filesystem matrix: {', '.join(filesystems)}", flush=True)
        avail = fssetup.availability(filesystems)
        usable = []
        for fs_type in filesystems:
            info = avail.get(fs_type, {"available": False, "reason": "not probed"})
            if info["available"]:
                usable.append(fs_type)
            else:
                print(f"[benchctl] SKIP {fs_type}: {info['reason']}", flush=True)
        # A filesystem that could not be measured has to show up in the
        # results as a gap. Silently dropping it would read as "we measured
        # them all" in the report.
        json.dump(avail, open(os.path.join(RESULTS, "fs-availability.json"), "w"),
                  indent=1)
        filesystems = usable
        if not filesystems:
            # A filesystem matrix with no filesystem in it is not a
            # degraded result, it is a wrong result: the legs would run on
            # the host's own filesystem and the report would look like a
            # successful matrix.
            raise RuntimeError(
                "no filesystem could be provisioned on this host: "
                + json.dumps(avail))

    legs = [(system, fs_type, extra)
            for fs_type in (filesystems or [None])
            for extra in (scenario.get("legs") or [{}])
            for system in scenario["systems"]]

    fs_state = None
    current_fs = None
    for system, fs_type, extra in legs:
        # Provision the filesystem when the leg changes to a new one. It is
        # torn down after its last leg, not after each leg: re-formatting
        # between systems of the same filesystem would measure the format
        # instead of the filesystem.
        if fs_type != current_fs:
            if fs_state:
                fssetup.teardown(fs_state)
                fs_state = None
            current_fs = fs_type
            if fs_type:
                print(f"[benchctl] === filesystem {fs_type} ===", flush=True)
                fs_state = fssetup.provision(
                    fs_type,
                    # Per-scenario override: scale scenarios fill 20-40 GB
                    # of the sparse image, the default ceiling is 12.
                    size_gb=(scenario.get("size_gb")
                             or cfg.get("fs", {}).get("size_gb",
                                                      fssetup.DEFAULT_SIZE_GB)),
                    arc_max_bytes=fssetup.parse_size(cfg["envelope"].get("memory", "")),
                )
                probe = fssetup.probe(fs_state)
                json.dump({"run_id": run_id, **probe},
                          open(os.path.join(RESULTS, f"fs-{fs_type}.json"), "w"),
                          indent=1)
                print(f"[benchctl] fs {fs_type}: {fs_state['mount']} "
                      f"on {fs_state['device']}", flush=True)

        # Per-leg ZFS ARC cap. The pool is created once with the envelope
        # cap, but the ARC lives in host memory outside the container's
        # cgroup, so a leg can shrink it to test whether "zfs is fast" is
        # the filesystem or the cache. Reset on every leg: a cap that
        # outlives its leg would silently change the next measurement.
        if fs_type == "zfs":
            mb = extra.get("arc_max_mb")
            want = (fssetup.parse_size(f"{mb}M") if mb
                    else fssetup.parse_size(cfg["envelope"].get("memory", "")))
            if fssetup.set_arc_max(want):
                print(f"[benchctl] zfs arc_max={want // 1024 ** 2} MB", flush=True)

        module, variant = split_system(system)
        mod_cfg = yaml.safe_load(
            open(os.path.join(ROOT, "modules", module, "module.yaml")))
        print(f"[benchctl] === {system}"
              f"{' on ' + fs_type if fs_type else ''}"
              f"{' ' + extra_label(extra) if extra_label(extra) else ''} ===",
              flush=True)

        plan = gen_plan(system, scenario, cfg, mod_cfg, fs_type, extra)
        json.dump(plan, open(os.path.join(ROOT, "plan.json"), "w"), indent=1)
        # The active module, as a symlink: ramp.js imports a fixed path and
        # no JS is ever generated. A symlink is not a generated file.
        active = os.path.join(ROOT, "modules", "active")
        if os.path.islink(active) or os.path.exists(active):
            os.remove(active)
        os.symlink(module, active)
        compose = gen_compose(module, cfg, mod_cfg, system, scenario, fs_state,
                              extra)
        json.dump(compose, open(os.path.join(ROOT, "compose.gen.json"), "w"))
        buf = io.StringIO()
        yaml.safe_dump(compose, buf, default_flow_style=False)
        open(os.path.join(ROOT, "docker-compose.gen.yml"), "w").write(buf.getvalue())

        # Bring the SUT up from nothing. A container that survived from a
        # previous leg keeps its bind mount — the path string is the same,
        # so compose sees no change and reuses it, data and all. Tearing
        # the container down first makes every leg hermetic.
        sh("docker", "compose", "-f", "docker-compose.gen.yml",
           "down", "-v", check=False)
        sh("docker", "compose", "-f", "docker-compose.gen.yml",
           "up", "-d", "--wait", "--wait-timeout", "180", "sut")
        # Port env vars default from module.yaml before expand(): expand()
        # substitutes "" for a missing var, which turns the endpoint
        # "http://localhost:{SUT_PORT}" into "http://localhost:" and the
        # init then dials port 80 instead of 19090 — Connection refused.
        # CI does not set SUT_PORT/PG_PORT, so the default is the only
        # value it can ever see.
        for p in mod_cfg.get("deploy", {}).get("ports", []):
            os.environ.setdefault(p["env"], p["default"])
        base = expand(mod_cfg["deploy"]["endpoint"])

        print("[benchctl] init data", flush=True)
        init_env = {
            "SUT_URL": base,
            "PG_DSN": mod_cfg.get("k6", {}).get("env", {}).get("PG_DSN",
                              "postgres://bench:bench@postgres:5432/bench?sslmode=disable"),
            "PG_PORT": str(mod_cfg.get("deploy", {}).get("ports", [{}])[0].get("default", "15432")),
            "JOBS": str(ds["jobs"]),
            "INSTANCES_PER_JOB": str(ds["instances_per_job"]),
            "INTERVAL_SECONDS": str(ds.get("interval_seconds", 15)),
            "WINDOW_MINUTES": str(ds["window_minutes"]),
            "VARIANT": variant or "",
            "QUERY_MARKER": mod_cfg.get("query_marker", ""),
            "RESULTS_DIR": RESULTS,
            # Which filesystem the data directory sits on, so the module can
            # label what it measured and the report can prove it.
            "FS_TYPE": fs_type or "",
            "FS_MOUNT": fs_state["mount"] if fs_state else "",
        }
        # Scenario-level environment for the module: a scenario with many
        # legs (a filesystem matrix) needs a shorter query sweep than one
        # with a single leg.
        init_env.update({k: str(v) for k, v in (scenario.get("env") or {}).items()})
        # The leg's own dimension, if it has one: the sharding scenario
        # varies the number of shards, and the module needs to know.
        for key, value in (extra or {}).items():
            init_env[str(key).upper()] = str(value)
        t_init = time.monotonic()
        out = subprocess.run(
            [sys.executable, f"modules/{module}/init.py"],
            cwd=ROOT, env={**os.environ, **init_env},
            capture_output=True, text=True)
        init_seconds = time.monotonic() - t_init
        print(out.stdout[-2000:])
        if out.returncode != 0:
            print(out.stderr[-3000:])
            raise RuntimeError("init failed")
        init_res = json.loads(out.stdout.strip().splitlines()[-1])
        disk = disk_bytes_du(mod_cfg["deploy"]["disk_path"])
        slug = leg_slug(system, fs_type, extra)

        # Sample stats for parity (collected while SUT is still up),
        # BEFORE writing disk so bytes_per_sample uses the real row count.
        print("[benchctl] collecting sample stats for parity...", flush=True)
        stats_env = {**init_env}
        if module == "postgres":
            stats_env["VARIANT"] = variant
        out = subprocess.run(
            [sys.executable, f"modules/{module}/observe.py", "stats"],
            cwd=ROOT, env={**os.environ, **stats_env},
            capture_output=True, text=True)
        if out.returncode != 0:
            print(out.stderr[-2000:])
            raise RuntimeError("sample stats failed")
        stats = json.loads(out.stdout.strip().splitlines()[-1])
        json.dump({"module": system, "fs": fs_type or "",
                   "extra": extra_label(extra), "run_id": run_id, **stats},
                  open(os.path.join(RESULTS, f"stats-{slug}.json"), "w"), indent=1)
        print(f"[benchctl] parity stats: {stats}", flush=True)

        samples = stats.get("samples", init_res.get("samples", 0))
        disk_samples = samples if samples > 0 else init_res.get("samples", 1)
        json.dump({"module": system, "fs": fs_type or "",
                   "extra": extra_label(extra), "run_id": run_id,
                   **init_res, **stats,
                   "init_seconds": round(init_seconds, 1),
                   "bytes_on_disk": disk,
                   "bytes_per_sample": round(disk / disk_samples, 3) if disk_samples > 0 else 0},
                  open(os.path.join(RESULTS, f"disk-{slug}.json"), "w"), indent=1)
        print(f"[benchctl] disk: {disk} B "
              f"({disk / disk_samples:.2f} B/sample)", flush=True)

        # What the filesystem actually did during the load. pg_stat_io is
        # PostgreSQL's own accounting of the syscalls it made: write_time,
        # fsync_time, read_time. It is the closest thing to a filesystem
        # benchmark that the database can give us, and it comes from the same
        # process that produced the workload.
        io_write = {}
        if fs_state:
            print("[benchctl] settling filesystem (flush to device)", flush=True)
            fssetup.settle(fs_state)
            io_write = collect_io(module, init_env)

            # fsync latency, measured by PostgreSQL's own fsync benchmark on
            # the data directory itself, plus a host-side fio fallback for
            # the filesystems where pg_test_fsync cannot run (ZFS refuses
            # 8 KiB O_DIRECT writes).
            print("[benchctl] fsync latency (pg_test_fsync)", flush=True)
            fsync = fsync_bench(mod_cfg["deploy"]["disk_path"])
            fio = fssetup.fsync_bench_fio(fs_state["mount"])
            fsync["fio"] = fio
            json.dump({"module": system, "fs": fs_type, "run_id": run_id, **fsync},
                      open(os.path.join(RESULTS, f"fsync-{slug}.json"), "w"), indent=1)
            if fsync.get("available"):
                print(f"[benchctl] fsync: fdatasync {fsync.get('fdatasync_us')} us, "
                      f"fsync {fsync.get('fsync_us')} us", flush=True)
            else:
                print(f"[benchctl] fsync benchmark unavailable: "
                      f"{fsync.get('error')}", flush=True)
            if fio.get("available"):
                print(f"[benchctl] fsync (fio): p50 {fio['fdatasync_us_p50']} us, "
                      f"p99 {fio['fdatasync_us_p99']} us", flush=True)
            else:
                print(f"[benchctl] fio fsync benchmark unavailable: "
                      f"{fio.get('reason')}", flush=True)
        else:
            # ext4 is measured on the host's own disk: there is no loop
            # mount to probe. The loop images every other filesystem uses
            # live on that same disk, so the fio probe still produces a
            # comparable hardware fingerprint. Without it, three jobs carry
            # a fingerprint and the fourth does not, and a cross-run
            # comparison cannot be gated on "same disk class".
            print("[benchctl] fsync latency (host disk, no loop mount)", flush=True)
            probe_dir = tempfile.mkdtemp(prefix=".benchctl-fsync-",
                                         dir=os.path.dirname(ROOT))
            try:
                fio = fssetup.fsync_bench_fio(probe_dir)
            except Exception as e:  # noqa: BLE001 — probe must not fail a leg
                fio = {"available": False, "reason": str(e)}
            finally:
                shutil.rmtree(probe_dir, ignore_errors=True)
            json.dump({"module": system, "fs": "host", "run_id": run_id,
                       "available": False,
                       "note": "no loop mount; fio probes the host disk",
                       "fio": fio},
                      open(os.path.join(RESULTS, f"fsync-{slug}.json"), "w"),
                      indent=1)
            if fio.get("available"):
                print(f"[benchctl] fsync (fio, host): p50 "
                      f"{fio['fdatasync_us_p50']} us, p99 "
                      f"{fio['fdatasync_us_p99']} us", flush=True)
            else:
                print(f"[benchctl] fio fsync benchmark unavailable: "
                      f"{fio.get('reason')}", flush=True)

        print("[benchctl] query sweep q1-q5", flush=True)
        qbench_env = {**init_env}
        if module == "postgres":
            qbench_env["VARIANT"] = variant
        qbench_env["RESULTS_DIR"] = RESULTS
        out = subprocess.run(
            [sys.executable, f"modules/{module}/observe.py", "qbench"],
            cwd=ROOT, env={**os.environ, **qbench_env},
            capture_output=True, text=True)
        if out.returncode != 0:
            print(out.stderr[-3000:])
            raise RuntimeError("query bench failed")
        qres = json.loads(out.stdout.strip().splitlines()[-1])
        json.dump({"module": system, "fs": fs_type or "",
                   "extra": extra_label(extra), "run_id": run_id, **qres},
                  open(os.path.join(RESULTS, f"queries-{slug}.json"), "w"), indent=1)
        print(f"[benchctl] query sweep done: {len(qres)} queries", flush=True)

        # Read shapes: the queries that make the filesystem do the work.
        # A module opts in with `capabilities: [readshapes]`.
        if "readshapes" in mod_cfg.get("capabilities", []):
            print("[benchctl] read shapes (warm)", flush=True)
            out = subprocess.run(
                [sys.executable, f"modules/{module}/observe.py", "readshapes"],
                cwd=ROOT, env={**os.environ, **qbench_env},
                capture_output=True, text=True)
            if out.returncode != 0:
                print(out.stderr[-3000:])
                raise RuntimeError("read shape bench failed")
            rshapes = json.loads(out.stdout.strip().splitlines()[-1])
            json.dump({"module": system, "fs": fs_type or "",
                   "extra": extra_label(extra), "run_id": run_id,
                       **rshapes},
                      open(os.path.join(RESULTS, f"readshapes-{slug}.json"), "w"),
                      indent=1)
            print(f"[benchctl] read shapes done: {len(rshapes)} shapes", flush=True)

        # Second io snapshot: the difference between this one and the first is
        # what the read path cost.
        if fs_state:
            io_read = collect_io(module, qbench_env)
            json.dump({"module": system, "fs": fs_type, "run_id": run_id,
                       "mount": fs_state["mount"], "device": fs_state["device"],
                       "after_write": io_write, "after_read": io_read},
                      open(os.path.join(RESULTS, f"io-{slug}.json"), "w"), indent=1)

        print("[benchctl] k6 ramp", flush=True)
        # The k6 image runs as its own uid, which is not the runner's uid on
        # Linux (Docker Desktop on macOS masks this). Without group-writable
        # results/, handleSummary dies with permission denied and verify
        # fails on a run that otherwise measured correctly — which is exactly
        # what happened the first time this ran on CI.
        # Use 0o775 (group-writable) instead of 0o777 (world-writable) for
        # security; k6 container shares the group via Docker user namespace.
        os.chmod(RESULTS, 0o775)

        def server_count():
            """Ask the module what its server handled, right now.

            Called before AND after the k6 leg: the delta is what k6 caused.
            Baselines matter because healthchecks and scrapes also hit the
            server, and counting them as bench traffic would be dishonest.
            """
            out = subprocess.run(
                [sys.executable, f"modules/{module}/observe.py"],
                cwd=ROOT, env={**os.environ, **init_env,
                               "QUERY_MARKER": mod_cfg.get("query_marker", "")},
                capture_output=True, text=True)
            if out.returncode != 0:
                print(out.stderr[-2000:])
                raise RuntimeError("server request count failed")
            return json.loads(out.stdout.strip().splitlines()[-1])["server_requests"]

        def cadvisor_stats():
            """Latest (cpu_ns, mem_bytes) for the SUT.

            Tries cAdvisor JSON API first, then /sys/fs/cgroup (host
            filesystem) as fallback. cpu in nanoseconds, memory in bytes.
            Returns (None, None) if everything fails — run continues,
            resource data simply absent.
            """
            result = _cadvisor_json_stats()
            if result != (None, None):
                return result
            return _cgroup_stats()

        def _cadvisor_json_stats():
            """cAdvisor JSON API: discover SUT via containers list, read stats."""
            try:
                with urllib.request.urlopen(
                        "http://localhost:18080/api/v1.3/containers/", timeout=10) as r:
                    containers = json.load(r)
            except Exception:
                return None, None

            sut_key = None
            for key in containers:
                if "sut" in key.lower():
                    sut_key = key
                    break
            if not sut_key:
                return None, None

            for path in (
                f"/api/v1.3/containers/{sut_key}",
                f"/api/v1.3/containers/{sut_key.lstrip('/')}",
            ):
                try:
                    with urllib.request.urlopen(
                            f"http://localhost:18080{path}", timeout=10) as r:
                        data = json.load(r)
                    stats = data.get("stats", [])
                    if stats:
                        latest = stats[0]  # newest first
                        cpu_ns = latest.get("cpu", {}).get("usage", {}).get("total")
                        mem_bytes = latest.get("memory", {}).get("working_set")
                        if cpu_ns is not None and mem_bytes is not None:
                            return cpu_ns, mem_bytes
                except Exception:
                    continue
            return None, None

        def _cgroup_stats():
            """Read SUT stats from /sys/fs/cgroup (host filesystem, cgroup v1/v2)."""
            mem_bytes = None
            for pattern in (
                f"/sys/fs/cgroup/docker/{system}/memory.current",
                f"/sys/fs/cgroup/memory/docker/{system}/memory.usage_in_bytes",
                f"/sys/fs/cgroup/system.slice/docker-{system}.slice/memory.current",
            ):
                try:
                    with open(pattern) as f:
                        mem_bytes = int(f.read().strip())
                        break
                except Exception:
                    continue

            cpu_ns = None
            for pattern in (
                f"/sys/fs/cgroup/docker/{system}/cpu.stat",
                f"/sys/fs/cgroup/cpuacct/docker/{system}/cpuacct.usage",
                f"/sys/fs/cgroup/system.slice/docker-{system}.slice/cpu.stat",
            ):
                try:
                    with open(pattern) as f:
                        content = f.read().strip()
                        if "usage_usec" in content:
                            for line in content.splitlines():
                                if line.startswith("usage_usec"):
                                    cpu_ns = int(line.split()[1]) * 1000  # us→ns
                                    break
                        else:
                            cpu_ns = int(content)
                        break
                except Exception:
                    continue

            if cpu_ns is not None or mem_bytes is not None:
                return cpu_ns, mem_bytes
            return None, None

        def sample_resources():
            return cadvisor_stats()

        before = server_count()
        cpu_start, mem_start = sample_resources()
        t_leg_start = time.monotonic()
        r = sh("docker", "compose", "-f", "docker-compose.gen.yml",
               "--profile", "k6", "run", "--rm", "k6",
               "run", "--out", "experimental-prometheus-rw",
               "/scripts/ramp.js", check=False)
        leg_seconds = time.monotonic() - t_leg_start
        cpu_end, mem_end = sample_resources()
        print(f"[benchctl] k6 exit: {r.returncode} (99 = threshold broke: "
              "that IS the breaking point)", flush=True)
        # Check SUT container for OOM/exit info
        try:
            inspect = subprocess.run(
                ["docker", "inspect", "benchctl-sut"],
                capture_output=True, text=True, check=False)
            if inspect.returncode == 0:
                import json as _json
                info = _json.loads(inspect.stdout)[0]
                state = info.get("State", {})
                payload["oom_killed"] = state.get("OOMKilled", False)
                payload["exit_code"] = state.get("ExitCode", None)
                if state.get("OOMKilled"):
                    print("[benchctl] WARNING: SUT was OOM-killed!", flush=True)
                if state.get("ExitCode") not in (0, None):
                    print(f"[benchctl] WARNING: SUT exited with code {state.get('ExitCode')}", flush=True)
        except Exception:
            pass

        # Resource usage during the k6 leg, from cAdvisor (mapped to host:18080).
        # CPU is a rate over the leg (fraction of one core); memory is the max
        # observed at start/end of the leg. Honest lower bound: we sample only
        # twice, so peaks between samples are not captured.
        cpu_rate = None
        if cpu_start is not None and cpu_end is not None and leg_seconds > 0:
            cpu_rate = round((cpu_end - cpu_start) / (leg_seconds * 1e9), 4)
        mem_max = None
        mem_max_mb = None
        candidates = [m for m in (mem_start, mem_end) if m is not None]
        if candidates:
            mem_max = max(candidates)
            mem_max_mb = round(mem_max / (1024 * 1024), 1)
        json.dump({"module": system, "fs": fs_type or "",
                   "extra": extra_label(extra), "run_id": run_id,
                   "leg_seconds": round(leg_seconds, 1),
                   "cpu_core_avg": cpu_rate,
                   "mem_bytes_max": mem_max,
                   "mem_max_mb": mem_max_mb},
                  open(os.path.join(RESULTS, f"resource-{slug}.json"), "w"), indent=1)
        print(f"[benchctl] resource: cpu={cpu_rate} core, mem={mem_max_mb} MB", flush=True)

        server_n = server_count() - before
        print(f"[benchctl] server handled {server_n} queries "
              "during the leg", flush=True)
        payload = json.load(open(os.path.join(RESULTS, f"disk-{slug}.json")))
        payload["server_requests"] = server_n
        payload["k6_exit_code"] = int(r.returncode)
        json.dump(payload, open(os.path.join(RESULTS, f"disk-{slug}.json"),
                                "w"), indent=1)

        # Cold read phase. Restarting the container empties PostgreSQL's
        # shared_buffers and dropping the page cache empties the host's.
        # After that the read path has to go to the filesystem, which is the
        # only measurement in this matrix where the filesystem is on the
        # critical path: every other number here is served from RAM.
        #
        # It runs after the k6 leg on purpose. Restarting the SUT in the
        # middle of the load phase would change what the load phase
        # measures, and the server-request count has to be taken before
        # these queries, or the k6 check would count them.
        if fs_state:
            def cold_reset():
                """Empty both caches: the container's shared_buffers and the
                host's page cache."""
                sh("docker", "compose", "-f", "docker-compose.gen.yml",
                   "restart", "-t", "60", "sut")
                sh("docker", "compose", "-f", "docker-compose.gen.yml",
                   "up", "-d", "--wait", "--wait-timeout", "180", "sut")
                return fssetup.drop_caches()

            print("[benchctl] cold phase: restarting the SUT, dropping caches",
                  flush=True)
            dropped = cold_reset()
            print(f"[benchctl] page cache dropped: {dropped}", flush=True)

            # The read shapes go first, and they get their own reset. A
            # sequential scan warms the whole table, so measuring the query
            # suite after it would measure a warm cache and call it cold.
            if "readshapes" in mod_cfg.get("capabilities", []):
                print("[benchctl] read shapes (cold)", flush=True)
                out = subprocess.run(
                    [sys.executable, f"modules/{module}/observe.py", "readshapes"],
                    cwd=ROOT, env={**os.environ, **qbench_env,
                                   "BENCHCTL_READSHAPES_COLD": "1"},
                    capture_output=True, text=True)
                if out.returncode != 0:
                    print(out.stderr[-3000:])
                    raise RuntimeError("cold read shape bench failed")
                cold_r = json.loads(out.stdout.strip().splitlines()[-1])
                json.dump({"module": system, "fs": fs_type, "run_id": run_id,
                           "cache_dropped": dropped, **cold_r},
                          open(os.path.join(RESULTS,
                                            f"readshapes-cold-{slug}.json"), "w"),
                          indent=1)

            print("[benchctl] cold phase: resetting the caches again",
                  flush=True)
            dropped = cold_reset()
            cold_env = {**qbench_env, "QBENCH_WARMUP": "0", "QBENCH_MEASURE": "1"}
            print("[benchctl] query sweep (cold)", flush=True)
            out = subprocess.run(
                [sys.executable, f"modules/{module}/observe.py", "qbench"],
                cwd=ROOT, env={**os.environ, **cold_env},
                capture_output=True, text=True)
            if out.returncode != 0:
                print(out.stderr[-3000:])
                raise RuntimeError("cold query bench failed")
            cold_q = json.loads(out.stdout.strip().splitlines()[-1])
            json.dump({"module": system, "fs": fs_type, "run_id": run_id,
                       "cache_dropped": dropped, **cold_q},
                      open(os.path.join(RESULTS, f"queries-cold-{slug}.json"), "w"),
                      indent=1)

            io_cold = collect_io(module, qbench_env)
            io_path = os.path.join(RESULTS, f"io-{slug}.json")
            io_payload = json.load(open(io_path))
            io_payload["after_cold"] = io_cold
            json.dump(io_payload, open(io_path, "w"), indent=1)
            print("[benchctl] cold phase done", flush=True)

        sh("docker", "compose", "-f", "docker-compose.gen.yml", "down", "-v")
        if fs_state:
            # `down -v` deletes named volumes; it cannot empty a bind mount.
            # The next leg on this filesystem has to start from an empty data
            # directory, or PostgreSQL would not run initdb and would attach
            # to the previous leg's cluster.
            fssetup.reset(fs_state)
        print(f"[benchctl] === {system}"
              f"{' on ' + fs_type if fs_type else ''} done ===", flush=True)

    if fs_state:
        fssetup.teardown(fs_state)
        print(f"[benchctl] filesystem {current_fs} torn down", flush=True)

    if monitoring_started:
        stop_monitoring()
        print("[benchctl] monitoring stopped", flush=True)

    # Contract check
    print("[benchctl] checking module contracts...", flush=True)
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "driver"))
    from contract import main as contract_main
    contract_main()
    print("[benchctl] contracts PASS", flush=True)

    # Render report
    print("[benchctl] rendering report...", flush=True)
    sys.path.insert(0, ROOT)  # benchctl/
    from report.render import main as report_main
    report_main()
    print("[benchctl] report written", flush=True)


if __name__ == "__main__":
    main()
