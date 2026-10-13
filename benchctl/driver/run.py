"""driver/run.py — the ONLY thing that generates text: plan.json + compose.

Usage: python driver/run.py scenarios/counter-rate.yaml
Pipeline per system: up -> init -> snapshot disk -> k6 (ramp) -> verify -> down.
"""
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
    # minimal ${VAR:-default} expansion, no dependency
    def sub(m):
        var, _, default = m.group(1).partition(":-")
        return os.environ.get(var, default)
    return yaml.safe_load(re.sub(r"\$\{([^}]+)\}", sub, text))


def gen_plan(scenario, cfg):
    return {
        "scenario": scenario["id"],
        "systems": scenario["systems"],
        "load": scenario["load"],
        "thresholds": scenario["thresholds"],
        "query": scenario["dataset"]["query"],
        "run_id": cfg["run"]["run_id"],
    }


def gen_compose(module, cfg, mod_cfg):
    env = cfg["envelope"]
    dep = mod_cfg["deploy"]
    # Default port avoids 9090: a prom-vs-postgres stack may be running.
    host_port = os.environ.get("SUT_PORT", "19090")
    return {
        "services": {
            "sut": {
                "image": mod_cfg["image"],
                "container_name": "benchctl-sut",
                "deploy": {"resources": {"limits": {
                    "cpus": env["cpus"], "memory": env["memory"]}}},
                "command": dep["command"],
                "ports": [f"{host_port}:9090"],
                "volumes": [
                    f"./modules/{module}/{dep['config']}:/etc/prometheus/{dep['config']}:ro",
                    "sut-data:/prometheus",
                ],
                "healthcheck": {"test": ["CMD-SHELL", dep["healthcheck"]]},
            },
            "k6": {
                "build": {"context": ".", "dockerfile": "load/Dockerfile.k6"},
                "profiles": ["k6"],
                "deploy": {"resources": {"limits": {
                    "cpus": env["k6_cpus"], "memory": env["k6_memory"]}}},
                "environment": {
                    "PROM_URL": "http://sut:9090",
                    "K6_PROMETHEUS_RW_SERVER_URL": cfg["cloud"]["remote_write_url"],
                },
                "volumes": ["./results:/results", "./load:/scripts:ro",
                            "./plan.json:/plan.json:ro",
                            "./modules:/modules:ro"],
                "depends_on": ["sut"],
            },
        },
        "volumes": {"sut-data": {}},
    }


def wait_healthy(url, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url + "/-/ready", timeout=5) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(3)
    raise RuntimeError("SUT never became healthy: " + url)


def disk_bytes_du():
    """du inside the SUT container: the honest on-disk figure.

    The blocks-bytes metric only counts compacted blocks; a fresh ingest
    lives in the head and reads as 0. du sees everything.
    """
    e = dict(os.environ)
    p = subprocess.run([*compose_bin(), "-f", "docker-compose.gen.yml",
                        "exec", "-T", "sut", "du", "-sb", "/prometheus"],
                       cwd=ROOT, env=e, capture_output=True, text=True)
    # output looks like "123456\t/prometheus"
    return int(p.stdout.strip().split()[0])


def main():
    scen_path = sys.argv[1]
    scenario = yaml.safe_load(open(scen_path))
    cfg = load_cfg()
    os.makedirs(RESULTS, exist_ok=True)
    run_id = cfg["run"]["run_id"]

    for module in scenario["systems"]:
        mod_cfg = yaml.safe_load(
            open(os.path.join(ROOT, "modules", module, "module.yaml")))
        print(f"[benchctl] === {module} ===", flush=True)

        plan = gen_plan(scenario, cfg)
        json.dump(plan, open(os.path.join(ROOT, "plan.json"), "w"), indent=1)
        compose = gen_compose(module, cfg, mod_cfg)
        json.dump(compose, open(os.path.join(ROOT, "compose.gen.json"), "w"))
        # compose file is generated as YAML via python (no extra dep):
        import io
        buf = io.StringIO()
        yaml.safe_dump(compose, buf, default_flow_style=False)
        open(os.path.join(ROOT, "docker-compose.gen.yml"), "w").write(buf.getvalue())

        sh("docker", "compose", "-f", "docker-compose.gen.yml",
           "up", "-d", "--wait", "--wait-timeout", "120", "sut")
        base = f"http://localhost:{os.environ.get('SUT_PORT', '19090')}"
        wait_healthy(base)

        print("[benchctl] init data", flush=True)
        init_env = {"PROM_URL": base,
                    "JOBS": str(scenario["dataset"]["jobs"]),
                    "INSTANCES_PER_JOB": str(scenario["dataset"]["instances_per_job"]),
                    "WINDOW_MINUTES": str(scenario["dataset"]["window_minutes"])}
        out = subprocess.run(
            [sys.executable, f"modules/{module}/init.py"],
            cwd=ROOT, env={**os.environ, **init_env},
            capture_output=True, text=True)
        print(out.stdout[-2000:])
        if out.returncode != 0:
            print(out.stderr[-3000:])
            raise RuntimeError("init failed")
        init_res = json.loads(out.stdout.strip().splitlines()[-1])

        disk = disk_bytes_du()
        json.dump({"module": module, "run_id": run_id, **init_res,
                   "bytes_on_disk": disk,
                   "bytes_per_sample": round(disk / init_res["samples"], 3)},
                  open(os.path.join(RESULTS, f"disk-{module}.json"), "w"), indent=1)
        print(f"[benchctl] disk: {disk} B "
              f"({disk / init_res['samples']:.2f} B/sample)", flush=True)

        print("[benchctl] k6 ramp", flush=True)
        r = sh("docker", "compose", "-f", "docker-compose.gen.yml",
               "--profile", "k6", "run", "--rm", "k6",
               "run", "--out", "experimental-prometheus-rw",
               "/scripts/ramp.js", check=False)
        print(f"[benchctl] k6 exit: {r.returncode} (99 = threshold broke: "
              "that IS the breaking point)", flush=True)

        sh("docker", "compose", "-f", "docker-compose.gen.yml", "down", "-v")
        print(f"[benchctl] === {module} done ===", flush=True)


if __name__ == "__main__":
    main()
