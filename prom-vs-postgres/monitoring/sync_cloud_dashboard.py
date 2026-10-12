#!/usr/bin/env python3
"""Generate the Grafana Cloud copy of the operations dashboard.

    python3 monitoring/sync_cloud_dashboard.py --write
    python3 monitoring/sync_cloud_dashboard.py            # check for drift

Why a generated copy rather than importing the provisioned one
---------------------------------------------------------------
Every panel in monitoring/grafana/dashboards/benchmark-operations.json points
at `uid: bench-monitoring`, which is the uid this project gives the datasource
it provisions into the LOCAL Grafana. A Grafana Cloud stack has its own
Prometheus datasource with an unrelated uid. Import that file into Cloud
unchanged and all eighteen panels read "Datasource not found" -- which looks
like a broken dashboard rather than a wrong uid, and costs a while to
diagnose.

The Cloud copy therefore uses Grafana's own `__inputs` substitution instead of
a hardcoded uid. On import through the UI, Grafana notices the input and asks
which datasource to bind, so nobody has to go and read a uid out of a settings
page. That is deliberate: the alternative is asking for a random-looking string
that only exists inside someone else's account, and a wrong value reproduces
exactly the silent failure this file exists to prevent.

Alloy's config needs the same treatment for the same reason and already has
sync_cloud_config.py; this keeps the two consistent.

The check
---------
`--check` (the default) fails if any hardcoded local uid survives, or if the
cloud copy has drifted from its source. That is the one way this file can
silently be wrong again.
"""

import argparse
import json
import pathlib
import sys

BASE = pathlib.Path(__file__).parent / "grafana" / "dashboards"
SOURCE = BASE / "benchmark-operations.json"
CLOUD = BASE / "benchmark-operations.cloud.json"

LOCAL_UID = "bench-monitoring"
INPUT_NAME = "DS_PROMETHEUS"
INPUT_REF = "${%s}" % INPUT_NAME


def substitute(node):
    """Swap the local datasource uid for the import-time input reference.

    Recursive rather than a string replace: the uid appears inside panel
    datasource blocks AND inside each templating variable, and a blind text
    substitution would also hit a legend string that happens to contain it.
    """
    if isinstance(node, dict):
        return {k: substitute(v) for k, v in node.items()}
    if isinstance(node, list):
        return [substitute(v) for v in node]
    if isinstance(node, str) and node == LOCAL_UID:
        return INPUT_REF
    return node


def count_str(node, value):
    if isinstance(node, dict):
        return sum(count_str(v, value) for v in node.values())
    if isinstance(node, list):
        return sum(count_str(v, value) for v in node)
    return 1 if node == value else 0


def render():
    dashboard = substitute(json.loads(SOURCE.read_text()))
    # Grafana resolves this input when the file is imported through the UI.
    # The source export carries an empty list, which this replaces.
    dashboard["__inputs"] = [{
        "name": INPUT_NAME,
        "label": "Prometheus (Grafana Cloud)",
        "description": "The Cloud stack that receives this benchmark's metrics.",
        "type": "datasource",
        "pluginId": "prometheus",
        "pluginName": "Prometheus",
    }]
    leftover = count_str(dashboard, LOCAL_UID)
    if leftover:
        raise SystemExit(
            "%d occurrence(s) of the local uid %r survived; panels would bind "
            "to the local datasource" % (leftover, LOCAL_UID))
    if not count_str(dashboard, INPUT_REF):
        raise SystemExit("no datasource reference produced; nothing to substitute")
    return json.dumps(dashboard, indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true",
                        help="write the cloud copy instead of checking it")
    args = parser.parse_args()

    if args.write:
        CLOUD.write_text(render())
        print("wrote %s" % CLOUD)
        print("  import it, and pick the Cloud Prometheus when Grafana asks")
        return 0

    if not CLOUD.exists():
        print("%s missing; run with --write" % CLOUD.name)
        return 1
    if CLOUD.read_text() != render():
        print("%s is stale; re-run --write" % CLOUD.name)
        return 1
    print("  ok     %s matches the source" % CLOUD.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())