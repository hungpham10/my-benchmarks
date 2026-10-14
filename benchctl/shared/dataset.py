"""Deterministic dataset definition.

10000 instances x 2 metrics = 20000 series, 45 minutes at 15s = 180 samples
per series, 3.6M samples in total. Generation is deterministic so the
Prometheus and the Postgres passes see byte-identical rows even though they run
separately, and both passes share one time anchor read back from
results/dataset.json -- otherwise `now` moves between them and the two targets
end up holding disjoint time windows.

Timestamps start at `now` and extend into the future. That is deliberate:
Prometheus rejects any sample older than MaxTime - chunkRange/2, which with the
default 2h block range is only about one hour of history. Writing forward in
time keeps the whole window inside the appendable range without changing any
compaction-relevant setting.
"""

import os

JOBS = int(os.environ.get("JOBS", "10"))
INSTANCES_PER_JOB = int(os.environ.get("INSTANCES_PER_JOB", "500"))
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "15"))
# The window is in MINUTES, not hours, and that is not a style choice. Two hard
# limits bracket how far ahead a Prometheus TSDB will accept samples:
#
#   * remote-write rejects anything more than maxAheadTime = 10 minutes ahead of
#     wall clock (storage/remote/write_handler.go:53)
#   * the head rejects anything older than MaxTime - chunkRange/2, which is one
#     hour at the default 2h block range (head_append.go:212)
#
# So the usable envelope is roughly [now - 50m, now + 10m] and the window must
# be under an hour. A 24h or 6h dataset cannot be backfilled at all through the
# real write path. Scale cardinality instead of retention.
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "45"))

# Centre the window slightly ahead of wall clock so both bounds have slack.
AHEAD_MINUTES = float(os.environ.get("AHEAD_MINUTES", "8"))
SEED = int(os.environ.get("SEED", "20200101"))
JITTER_MS = int(os.environ.get("JITTER_MS", "0"))

SAMPLES = WINDOW_MINUTES * 60 // INTERVAL_SECONDS
REGIONS = ["us-east", "us-west", "eu-central"]
SERVICES = ["api", "worker", "scheduler", "gateway"]

COUNTER_METRIC = "http_requests_total"
GAUGE_METRIC = "queue_depth"

# Share of counter series that see one reset inside the window. Enough to
# exercise the counter-reset branch of rate()/increase() without dominating.
RESET_EVERY = 50

TOTAL_SERIES = JOBS * INSTANCES_PER_JOB * 2
TOTAL_SAMPLES = TOTAL_SERIES * SAMPLES


def build_series():
    """Ordered series descriptors. This order is canonical for every target."""
    series = []
    for job_index in range(JOBS):
        job = "job-%02d" % job_index
        service = SERVICES[job_index % len(SERVICES)]
        for instance_index in range(INSTANCES_PER_JOB):
            instance = "host-%04d" % instance_index
            region = REGIONS[(job_index + instance_index) % len(REGIONS)]
            base = {
                "job": job,
                "instance": instance,
                "region": region,
                "service": service,
            }
            series.append(dict(base, id=len(series), metric=COUNTER_METRIC))
            series.append(dict(base, id=len(series), metric=GAUGE_METRIC))
    return series


def is_counter(desc):
    return desc["metric"] == COUNTER_METRIC


def has_reset(desc):
    return is_counter(desc) and desc["id"] % RESET_EVERY == 0


def t_end_ms(t_start_ms):
    return t_start_ms + (SAMPLES - 1) * INTERVAL_SECONDS * 1000


def labels_of(desc):
    """Prometheus label set, including __name__ which is a normal label there."""
    return [
        ("__name__", desc["metric"]),
        ("instance", desc["instance"]),
        ("job", desc["job"]),
        ("region", desc["region"]),
        ("service", desc["service"]),
    ]


def labels_json(desc):
    return (
        '{"__name__":"%s","instance":"%s","job":"%s","region":"%s","service":"%s"}'
        % (desc["metric"], desc["instance"], desc["job"], desc["region"], desc["service"])
    )
