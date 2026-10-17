"""driver/run.py — the ONLY thing that generates text: plan.json + compose.

Usage: python driver/run.py scenarios/counter-rate.yaml
Pipeline per system: up -> init -> du disk -> k6 (ramp) -> verify -> down.

Systems are "module" or "module/variant" (e.g. postgres/jsonb). The module
directory holds everything module-specific; this file only orchestrates.
"""
import io
import json
import os
import subprocess
import sys
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

COMPOSE = None


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
            sql = q["sql"][spec["table"]]
            out = sql.replace("%s", _ts_literal(manifest["t_start_ms"]), 1)
            out = out.replace("%s", _ts_literal(manifest["t_end_ms"]), 1)
            assert "%s" not in out, "unrendered placeholder in " + spec["query"]
            return " ".join(out.split())
    raise RuntimeError("unknown query: " + spec["query"])


def gen_plan(system, scenario, cfg, mod_cfg):
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

    return {
        "scenario": scenario["id"],
        "system": system,
        "load": load,
        "thresholds": scenario["thresholds"],
        "query": render_query(system, scenario),
        "query_marker": mod_cfg.get("query_marker", ""),
        "run_id": cfg["run"]["run_id"],
    }


def gen_compose(module, cfg, mod_cfg, system, scenario):
    env = cfg["envelope"]
    dep = mod_cfg["deploy"]
    volumes = []
    for f in dep.get("config_files", []):
        volumes.append(f"./modules/{module}/{f['src']}:{f['dst']}:ro")
    dv = dep.get("data_volume")
    if dv:
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
    if ports:
        sut["ports"] = ports
    if volumes:
        sut["volumes"] = volumes
    if dep.get("environment"):
        sut["environment"] = {k: expand(v)
                              for k, v in dep["environment"].items()}
    if dep.get("healthcheck"):
        sut["healthcheck"] = dep["healthcheck"]

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
    for k, v in k6cfg.get("env", {}).items():
        k6env[k] = expand(v)
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
    if dv:
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


def start_monitoring(module_name):
    """Static stack, outside the measured envelope — or nothing at all.

    cAdvisor + Alloy pushing straight to Cloud. Skipped entirely when
    GRAFANA_CLOUD_RW_URL is empty: an Alloy with no receiver is a crash loop,
    not monitoring, and the local summary.json stays the record either way.
    """
    if not os.environ.get("GRAFANA_CLOUD_RW_URL"):
        print("[benchctl] monitoring skipped (no Cloud URL)", flush=True)
        return False
    # Network may already exist from a previous leg; ignore "already exists" error.
    sh("docker", "network", "create", "benchctl-mon", check=False)
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
    now_ms = int(time.time() * 1000)
    t_start_ms = now_ms - (window_min - ahead_min) * 60 * 1000
    t_end_ms = t_start_ms + window_min * 60 * 1000
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

    for system in scenario["systems"]:
        module, variant = split_system(system)
        mod_cfg = yaml.safe_load(
            open(os.path.join(ROOT, "modules", module, "module.yaml")))
        print(f"[benchctl] === {system} ===", flush=True)

        plan = gen_plan(system, scenario, cfg, mod_cfg)
        json.dump(plan, open(os.path.join(ROOT, "plan.json"), "w"), indent=1)
        # The active module, as a symlink: ramp.js imports a fixed path and
        # no JS is ever generated. A symlink is not a generated file.
        active = os.path.join(ROOT, "modules", "active")
        if os.path.islink(active) or os.path.exists(active):
            os.remove(active)
        os.symlink(module, active)
        compose = gen_compose(module, cfg, mod_cfg, system, scenario)
        json.dump(compose, open(os.path.join(ROOT, "compose.gen.json"), "w"))
        buf = io.StringIO()
        yaml.safe_dump(compose, buf, default_flow_style=False)
        open(os.path.join(ROOT, "docker-compose.gen.yml"), "w").write(buf.getvalue())

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
        }
        out = subprocess.run(
            [sys.executable, f"modules/{module}/init.py"],
            cwd=ROOT, env={**os.environ, **init_env},
            capture_output=True, text=True)
        print(out.stdout[-2000:])
        if out.returncode != 0:
            print(out.stderr[-3000:])
            raise RuntimeError("init failed")
        init_res = json.loads(out.stdout.strip().splitlines()[-1])
        disk = disk_bytes_du(mod_cfg["deploy"]["disk_path"])
        slug = system.replace("/", "-")

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
        json.dump({"module": system, "run_id": run_id, **stats},
                  open(os.path.join(RESULTS, f"stats-{slug}.json"), "w"), indent=1)
        print(f"[benchctl] parity stats: {stats}", flush=True)

        samples = stats.get("samples", init_res.get("samples", 0))
        disk_samples = samples if samples > 0 else init_res.get("samples", 1)
        json.dump({"module": system, "run_id": run_id, **init_res, **stats,
                   "bytes_on_disk": disk,
                   "bytes_per_sample": round(disk / disk_samples, 3) if disk_samples > 0 else 0},
                  open(os.path.join(RESULTS, f"disk-{slug}.json"), "w"), indent=1)
        print(f"[benchctl] disk: {disk} B "
              f"({disk / disk_samples:.2f} B/sample)", flush=True)

        print("[benchctl] k6 ramp", flush=True)
        # The k6 image runs as its own uid, which is not the runner's uid on
        # Linux (Docker Desktop on macOS masks this). Without world-writable
        # results/, handleSummary dies with permission denied and verify
        # fails on a run that otherwise measured correctly — which is exactly
        # what happened the first time this ran on CI.
        os.chmod(RESULTS, 0o777)

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
            """Latest (cpu_ns, mem_bytes) for the SUT from cAdvisor JSON API.

            Lists containers to discover the SUT name, then reads its latest
            stats. cpu.usage.total is in nanoseconds; memory.working_set in
            bytes. Returns (None, None) if cAdvisor is unreachable — run
            continues, resource data simply absent.
            """
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
        json.dump({"module": system, "run_id": run_id,
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

        sh("docker", "compose", "-f", "docker-compose.gen.yml", "down", "-v")
        print(f"[benchctl] === {system} done ===", flush=True)

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
