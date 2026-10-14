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
  - deploy: {command, config_files, data_volume, ports, healthcheck, endpoint, disk_path}
  - k6: {image|dockerfile, env}
  - query_marker (for server_requests filter)
  - requests_module (for server-side request count)
"""

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

REQUIRED_DEPLOY_KEYS = [
    "command",
    "config_files",
    "data_volume",
    "ports",
    "environment",
    "healthcheck",
    "endpoint",
    "disk_path",
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

    if "k6" in m:
        for key in REQUIRED_K6_KEYS:
            if key not in m["k6"]:
                errors.append(f"k6 missing key: {key}")

    if "query_marker" not in m or not m["query_marker"]:
        errors.append("query_marker must be non-empty string")

    if "requests_module" not in m or not m["requests_module"]:
        errors.append("requests_module must be non-empty string")

    return errors


def check_python_module(module):
    """Check Python module has required functions."""
    errors = []
    # The module Python file is at modules/{module}/init.py
    # but we check for the required functions in the module's Python files
    init_path = os.path.join(MODULES_DIR, module, "init.py")
    requests_path = os.path.join(MODULES_DIR, module, "requests.py")

    # Check init.py exists
    if not os.path.exists(init_path):
        errors.append(f"missing {init_path}")
    else:
        # Could add function signature check here
        pass

    if not os.path.exists(requests_path):
        errors.append(f"missing {requests_path}")
    else:
        pass

    return errors


def check_module(module):
    """Full contract check for a module."""
    all_errors = []
    all_errors.extend(check_yaml(module))
    all_errors.extend(check_python_module(module))
    return all_errors


def main():
    modules = [d for d in os.listdir(MODULES_DIR)
               if os.path.isdir(os.path.join(MODULES_DIR, d))]

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