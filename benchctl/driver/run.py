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

try:
    import yaml
except ImportError:
    sys.exit("need pyyaml: pip install pyyaml")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")
OLD_GEN = os.path.join(os.path.dirname(ROOT), "prom-vs-postgres", "generator")

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
    # The queries module is in prom-vs-postgres/bench/, not generator/
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "prom-vs-postgres", "bench"))
    import queries as Q
    manifest = json.load(open(os.path.join(RESULTS, "dataset.json")))
    start_s = manifest["t_start_ms"] / 1000.0
    end_s = manifest["t_end_ms"] / 1000.0
    for q in Q.QUERIES:
        if q["name"] == spec["query"]:
            sql = q["sql"][spec["table"]]
            out = sql.replace("%s", repr(start_s), 1)
            out = out.replace("%s", repr(end_s), 1)
            assert "%s" not in out, "unrendered placeholder in " + spec["query"]
            return " ".join(out.split())
    raise RuntimeError("unknown query: " + spec["query"])


def gen_plan(system, scenario, cfg, mod_cfg):
    return {
        "scenario": scenario["id"],
        "system": system,
        "load": scenario["load"],
        "thresholds": scenario["thresholds"],
        "query": render_query(system, scenario),
        "query_marker": mod_cfg.get("query_marker", ""),
        "run_id": cfg["run"]["run_id"],
    }


def gen_compose(module, cfg, mod_cfg):
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
    for k, v in k6cfg.get("env", {}).items():
        k6env[k] = expand(v)
    k6svc = {
        "profiles": ["k6"],
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
    if k6cfg.get("dockerfile"):
        k6svc["image"] = f"benchctl-k6-{module}"
    else:
        k6svc["image"] = k6cfg.get("image", "grafana/k6:latest")

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


def start_monitoring(module):
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
        "BENCH_MODULE": module,
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


def build_k6_image(module, mod_cfg):
    df = mod_cfg.get("k6", {}).get("dockerfile")
    if not df:
        return
    print(f"[benchctl] building k6 image for {module}", flush=True)
    sh("docker", "build", "-t", f"benchctl-k6-{module}",
       "-f", f"modules/{module}/{df}", f"modules/{module}/k6")


def main():
    scen_path = sys.argv[1]
    os.environ["BENCHCTL_SCENARIO"] = scen_path
    scenario = yaml.safe_load(open(scen_path))
    cfg = load_cfg()
    os.makedirs(RESULTS, exist_ok=True)
    run_id = cfg["run"]["run_id"]
    ds = scenario["dataset"]

    # Start monitoring ONCE for the entire run (shared across all systems)
    monitoring_started = start_monitoring(scenario["systems"][0])

    for system in scenario["systems"]:
        module, variant = split_system(system)
        mod_cfg = yaml.safe_load(
            open(os.path.join(ROOT, "modules", module, "module.yaml")))
        print(f"[benchctl] === {system} ===", flush=True)

        build_k6_image(module, mod_cfg)

        plan = gen_plan(system, scenario, cfg, mod_cfg)
        json.dump(plan, open(os.path.join(ROOT, "plan.json"), "w"), indent=1)
        # The active module, as a symlink: ramp.js imports a fixed path and
        # no JS is ever generated. A symlink is not a generated file.
        active = os.path.join(ROOT, "modules", "active")
        if os.path.islink(active) or os.path.exists(active):
            os.remove(active)
        os.symlink(module, active)
        compose = gen_compose(module, cfg, mod_cfg)
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

        # Write dataset.json after the first leg's init so render_query can read it
        if not os.path.exists(os.path.join(RESULTS, "dataset.json")):
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
                "targets": [system],
            }
            json.dump(manifest, open(os.path.join(RESULTS, "dataset.json"), "w"), indent=1)
            print(f"[benchctl] wrote dataset.json", flush=True)

        disk = disk_bytes_du(mod_cfg["deploy"]["disk_path"])
        slug = system.replace("/", "-")
        json.dump({"module": system, "run_id": run_id, **init_res,
                   "bytes_on_disk": disk,
                   "bytes_per_sample": round(disk / init_res["samples"], 3)},
                  open(os.path.join(RESULTS, f"disk-{slug}.json"), "w"), indent=1)
        print(f"[benchctl] disk: {disk} B "
              f"({disk / init_res['samples']:.2f} B/sample)", flush=True)

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
                [sys.executable, f"modules/{module}/requests.py"],
                cwd=ROOT, env={**os.environ, **init_env},
                capture_output=True, text=True)
            if out.returncode != 0:
                print(out.stderr[-2000:])
                raise RuntimeError("server request count failed")
            return json.loads(out.stdout.strip().splitlines()[-1])["server_requests"]

        before = server_count()
        r = sh("docker", "compose", "-f", "docker-compose.gen.yml",
               "--profile", "k6", "run", "--rm", "k6",
               "run", "--out", "experimental-prometheus-rw",
               "/scripts/ramp.js", check=False)
        print(f"[benchctl] k6 exit: {r.returncode} (99 = threshold broke: "
              "that IS the breaking point)", flush=True)
        server_n = server_count() - before
        print(f"[benchctl] server handled {server_n} queries "
              "during the leg", flush=True)
        payload = json.load(open(os.path.join(RESULTS, f"disk-{slug}.json")))
        payload["server_requests"] = server_n
        json.dump(payload, open(os.path.join(RESULTS, f"disk-{slug}.json"),
                                "w"), indent=1)

        sh("docker", "compose", "-f", "docker-compose.gen.yml", "down", "-v")
        print(f"[benchctl] === {system} done ===", flush=True)

    if monitoring_started:
        stop_monitoring()
        print("[benchctl] monitoring stopped", flush=True)


if __name__ == "__main__":
    main()
