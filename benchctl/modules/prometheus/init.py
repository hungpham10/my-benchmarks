"""modules/prometheus/init.py — init(scale, shape): load the dataset.

Same wire path as prom-vs-postgres/generator/generate.py::ingest_prometheus
(chunked series x scrape windows, raw-block snappy, one WriteRequest per
batch). Returns {samples, ...} as JSON on the last stdout line.
stop_load() guarantee: read back the series count before returning, so
nothing the k6 leg queries was never made durable.
"""
import http.client
import json
import os
import sys
import time
from urllib.parse import urlparse

SHARED = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "shared")
sys.path.insert(0, SHARED)
import dataset as d
import values as v
import protobuf as p

PROM_URL = os.environ.get("SUT_URL", "http://localhost:19090")
SERIES_CHUNK = int(os.environ.get("SERIES_CHUNK", "1000"))
SCRAPES_PER_REQUEST = int(os.environ.get("SCRAPES_PER_REQUEST", "240"))


def _read_dataset_timestamps():
    """Read t_start_ms from dataset.json (written by driver before any init)."""
    results_dir = os.environ.get(
        "RESULTS_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "results"))
    ds_path = os.path.join(results_dir, "dataset.json")
    if not os.path.exists(ds_path):
        raise RuntimeError(f"dataset.json not found at {ds_path} - driver must write it before init")
    with open(ds_path) as f:
        m = json.load(f)
    return m["t_start_ms"], m["t_end_ms"]


def init(ahead_minutes=8):
    series = d.build_series()
    t_start_ms, t_end_ms = _read_dataset_timestamps()
    timestamps = v.generate_timestamps(t_start_ms)
    parsed = urlparse(PROM_URL)
    conn = http.client.HTTPConnection(
        parsed.hostname or "localhost", parsed.port or 80, timeout=300)
    conn.connect()
    sent, reqs = 0, 0
    t0 = time.monotonic()
    for cs in range(0, len(series), SERIES_CHUNK):
        chunk = series[cs:cs + SERIES_CHUNK]
        block = v.generate_chunk(chunk)
        for offset in range(0, d.SAMPLES, SCRAPES_PER_REQUEST):
            window = timestamps[offset:offset + SCRAPES_PER_REQUEST]
            batch = [
                p.encode_timeseries(
                    d.labels_of(desc),
                    [(block[r][i], int(window[i])) for i in range(window.size)])
                for r, desc in enumerate(chunk)]
            payload = p.snappy_compress(p.encode_write_request(batch))
            # Retry on 429 (rate limited) with exponential backoff
            max_retries = 5
            base_delay = 0.5
            for attempt in range(max_retries + 1):
                conn.request("POST", "/api/v1/write",
                             payload,
                             {"Content-Type": "application/x-protobuf",
                              "Content-Encoding": "snappy",
                              "X-Prometheus-Remote-Write-Version": "0.1.0"})
                resp = conn.getresponse()
                body = resp.read()
                if resp.status == 429 and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    print(f"[init] remote-write 429, retrying in {delay}s (attempt {attempt+1}/{max_retries})", file=sys.stderr)
                    time.sleep(delay)
                    continue
                if resp.status >= 300:
                    raise RuntimeError(f"remote-write {resp.status}: {body[:200]}")
                break
            sent += len(chunk) * window.size
            reqs += 1
    conn.close()
    elapsed = time.monotonic() - t0
    return {"samples": sent, "seconds": round(elapsed, 1),
            "samples_per_second": round(sent / elapsed, 1),
            "requests": reqs}


def stop_load():
    import urllib.request
    # Use window_minutes from dataset.json for the query
    results_dir = os.environ.get(
        "RESULTS_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "results"))
    ds_path = os.path.join(results_dir, "dataset.json")
    window_min = 45
    if os.path.exists(ds_path):
        with open(ds_path) as f:
            window_min = json.load(f).get("window_minutes", 45)
    with urllib.request.urlopen(
            PROM_URL + f"/api/v1/query?query=count(count_over_time(http_requests_total[{window_min}m]))", timeout=60) as r:
        data = json.load(r)
    return {"series_present": int(data["data"]["result"][0]["value"][1])}


if __name__ == "__main__":
    ahead = int(float(os.environ.get("AHEAD_MINUTES", "8")))
    print(json.dumps(init(ahead_minutes=ahead)))
