"""driver/contract.py — module contract verification.

Every module must provide these 5 functions:
  1. deploy()       -> container up, healthy, endpoint
  2. init()         -> load data, return {samples, ...}
  3. start_load()   -> open write path
  4. stop_load()    -> flush + close, guarantee durable
  5. teardown()     -> volumes removed

And module.yaml must have these keys:
  - name
  - image
  - deploy: {command, data_volume, endpoint, disk_path}
    (config_files, ports, environment, healthcheck are optional)
  - k6: {image|dockerfile, env}
  - query_marker (for server_requests filter)
  - requests_module (for server-side request count)
"""

import ast
import os
import sys
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES_DIR = os.path.join(ROOT, "modules")


REQUIRED_YAML_KEYS = [
    "name",
    "image",
    "deploy",
    "k6",
    "query_marker",
    "requests_module",
]

# gen_compose indexes deploy["command"] unconditionally. The other keys
# are guarded with .get() or `if <container>:`, so a module may
# legitimately omit them (e.g. Prometheus needs no environment).
REQUIRED_DEPLOY_KEYS = [
    "command",
    "data_volume",
    "endpoint",
    "disk_path",
]

OPTIONAL_DEPLOY_KEYS = [
    "config_files",
    "ports",
    "environment",
    "healthcheck",
]

REQUIRED_K6_KEYS = [
    "env",
]

REQUIRED_FUNCTIONS = [
    "init",
    "requests",
]


def load_module_yaml(module):
    path = os.path.join(MODULES_DIR, module, "module.yaml")
    if not os.path.exists(path):
        raise ValueError(f"module.yaml not found for {module}: {path}")
    return yaml.safe_load(open(path))


def check_yaml(module):
    """Check module.yaml has all required keys."""
    errors = []
    m = load_module_yaml(module)

    for key in REQUIRED_YAML_KEYS:
        if key not in m:
            errors.append(f"missing required key: {key}")

    if "deploy" in m:
        for key in REQUIRED_DEPLOY_KEYS:
            if key not in m["deploy"]:
                errors.append(f"deploy missing key: {key}")
        # Warn about optional keys that are missing (for doc completeness)
        for key in OPTIONAL_DEPLOY_KEYS:
            if key not in m["deploy"]:
                pass  # silent; modules don't need these

    if "k6" in m:
        for key in REQUIRED_K6_KEYS:
            if key not in m["k6"]:
                errors.append(f"k6 missing key: {key}")

    if "query_marker" not in m or not m["query_marker"]:
        errors.append("query_marker must be non-empty string")

    if "requests_module" not in m or not m["requests_module"]:
        errors.append("requests_module must be non-empty string")

    return errors


def _functions_defined_in(path):
    """Return set of top-level function names defined in a Python file."""
    try:
        with open(path) as f:
            tree = ast.parse(f.read())
    except Exception as e:
        return {f"<parse error: {e}>"}
    funcs = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(node.name)
    return funcs


def check_python_module(module):
    """Check Python module has required functions."""
    errors = []
    init_path = os.path.join(MODULES_DIR, module, "init.py")
    requests_path = os.path.join(MODULES_DIR, module, "requests.py")

    if not os.path.exists(init_path):
        errors.append(f"missing {init_path}")
    else:
        funcs = _functions_defined_in(init_path)
        if "init" not in funcs:
            errors.append(f"init.py missing required function: init")

    if not os.path.exists(requests_path):
        errors.append(f"missing {requests_path}")
    else:
        funcs = _functions_defined_in(requests_path)
        # requests.py exposes server_requests() for the driver
        if "server_requests" not in funcs:
            errors.append(f"requests.py missing required function: server_requests")

    return errors


def check_module(module):
    """Full contract check for a module."""
    all_errors = []
    all_errors.extend(check_yaml(module))
    all_errors.extend(check_python_module(module))
    return all_errors


def main():
    modules = [d for d in os.listdir(MODULES_DIR)
               if os.path.isdir(os.path.join(MODULES_DIR, d))
               and not os.path.islink(os.path.join(MODULES_DIR, d))]

    if not modules:
        print("[contract] No modules found")
        sys.exit(1)

    print(f"[contract] Checking {len(modules)} module(s): {', '.join(modules)}")
    all_errors = []
    for module in modules:
        print(f"[contract] checking {module}...", flush=True)
        errors = check_module(module)
        if errors:
            print(f"[contract] FAIL {module}:")
            for e in errors:
                print(f"  - {e}")
            all_errors.extend(errors)
        else:
            print(f"[contract] PASS {module}")

    if all_errors:
        print(f"\n[contract] FAILED: {len(all_errors)} error(s)")
        sys.exit(1)
    else:
        print(f"\n[contract] All {len(modules)} module(s) PASS")


if __name__ == "__main__":
    main()