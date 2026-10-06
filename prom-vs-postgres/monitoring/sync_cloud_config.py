#!/usr/bin/env python3
"""Regenerate or verify monitoring/alloy/config.cloud.alloy.

The cloud config is config.alloy plus a second remote_write endpoint, plus one
extra receiver on the trim stage. Alloy has no conditionals, so there is no way
to make that switch optional inside a single file, and no way to share the two
halves. Rather than hand-maintain two near-identical files, the cloud one is
generated: edit config.alloy, run this with --write, commit both.

Run without --write to check for drift. `monitoring/check.py` calls it, so a
stale cloud config fails the same verification that catches an empty dashboard.
"""
from __future__ import annotations

import pathlib
import sys

BASE = pathlib.Path(__file__).parent / "alloy" / "config.alloy"
CLOUD = pathlib.Path(__file__).parent / "alloy" / "config.cloud.alloy"

HEADER = '''// Grafana Alloy, variant B: the local monitoring stack PLUS a push to Grafana
// Cloud. This is the one that gives a benchmark run a durable record that
// outlives the machine, the way k6 Cloud keeps results server-side.
//
// Generated from config.alloy by monitoring/sync_cloud_config.py. Do not edit
// by hand: edit config.alloy and re-run that script, or the two drift apart and
// the local stack and the cloud stack quietly stop measuring the same thing.
//
// Credentials come from the environment, never from this file:
//
//   GRAFANA_CLOUD_URL     the metrics push URL of your Grafana Cloud stack
//   GRAFANA_CLOUD_ID      numeric instance (user) ID; the left half of the
//                         basic-auth username
//   GRAFANA_CLOUD_TOKEN   access policy token with metrics:write
//
// Put them in .env.monitoring, which is gitignored. Start Alloy with this file
// via ALLOY_CONFIG=config.cloud.alloy, which `make monitor-cloud` does.

'''

FOOTER = '''
// Grafana Cloud. Same series as the local endpoint, a second copy in a place
// that survives the laptop closing.
//
// This block is the only difference between this file and config.alloy, and it
// is why the credentials are read with sys.env(): a committed config file must
// never be able to carry a token, and an unset variable has to fail loudly at
// startup rather than silently write nowhere.
prometheus.remote_write "cloud" {
  endpoint {
    url = sys.env("GRAFANA_CLOUD_URL")

    basic_auth {
      username = sys.env("GRAFANA_CLOUD_ID")
      password = sys.env("GRAFANA_CLOUD_TOKEN")
    }
  }

  external_labels = {
    source = "benchmark-run",
  }
}
'''

# The single line that has to differ so the series reach the cloud endpoint too.
OLD_RECEIVER = (
    'prometheus.relabel "trim" {\n'
    "  forward_to = [prometheus.remote_write.monitoring.receiver]"
)
NEW_RECEIVER = (
    'prometheus.relabel "trim" {\n'
    "  forward_to = [\n"
    "    prometheus.remote_write.monitoring.receiver,\n"
    "    prometheus.remote_write.cloud.receiver,\n"
    "  ]"
)


def render() -> str:
    base = BASE.read_text()
    if OLD_RECEIVER not in base:
        raise SystemExit(
            f"cannot find the trim forward_to line in {BASE}.\n"
            "The local config changed shape; update OLD_RECEIVER here to match."
        )
    return HEADER + base.replace(OLD_RECEIVER, NEW_RECEIVER) + FOOTER


def main() -> int:
    expected = render()
    if "--write" in sys.argv:
        CLOUD.write_text(expected)
        print(f"wrote {CLOUD}")
        return 0
    if not CLOUD.exists():
        print(f"{CLOUD} is missing. Create it with:\n  python3 {sys.argv[0]} --write")
        return 1
    if CLOUD.read_text() != expected:
        print(
            f"{CLOUD} is out of date with {BASE}.\n"
            f"Regenerate it with:\n  python3 {sys.argv[0]} --write"
        )
        return 1
    print("cloud config in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
