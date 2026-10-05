"""Vectorised value and timestamp generation.

Everything is generated for a chunk of series at once, as a (chunk, SAMPLES)
matrix. The gauge process is an AR(1) advanced with a Python loop over the time
axis; the loop runs SAMPLES times on a vectorised column, so it is a few
milliseconds rather than the hours a per-sample Python loop or an O(n^2) np.convolve
would cost.
"""

import numpy as np

from dataset import (
    INTERVAL_SECONDS,
    JITTER_MS,
    SAMPLES,
    SEED,
    has_reset,
    is_counter,
)

# AR(1) persistence. The pull towards the level is therefore 1 - PERSISTENCE.
PERSISTENCE = 0.85


def generate_timestamps(t_start_ms):
    base = t_start_ms + np.arange(SAMPLES, dtype=np.int64) * INTERVAL_SECONDS * 1000
    if JITTER_MS <= 0:
        return base
    rng = np.random.default_rng(SEED + 9973)
    return base + rng.integers(0, JITTER_MS + 1, SAMPLES, dtype=np.int64)


def generate_chunk(descs):
    """Return float64 array of shape (len(descs), SAMPLES), in the given order."""
    count = len(descs)
    values = np.empty((count, SAMPLES), dtype=np.float64)

    counter_idx = [i for i, d in enumerate(descs) if is_counter(d)]
    gauge_idx = [i for i, d in enumerate(descs) if not is_counter(d)]

    if counter_idx:
        rng = np.random.default_rng(SEED + descs[counter_idx[0]]["id"])
        rows = np.array([descs[i]["id"] for i in counter_idx], dtype=np.int64)
        seeds = np.random.SeedSequence(SEED + rows)
        gen = np.random.default_rng(seeds)
        rate = gen.uniform(20.0, 400.0, size=rows.size)
        noise = gen.normal(0.0, 0.08, size=(rows.size, SAMPLES))
        step = np.maximum(0.0, rate[:, None] * INTERVAL_SECONDS * (1.0 + noise))
        block = np.cumsum(step, axis=1)
        for row, series_idx in enumerate(counter_idx):
            if has_reset(descs[series_idx]):
                reset_rng = np.random.default_rng(SEED + descs[series_idx]["id"] + 5)
                at = int(reset_rng.integers(SAMPLES // 4, 3 * SAMPLES // 4))
                block[row, at:] *= float(reset_rng.uniform(0.1, 0.9))
        values[counter_idx] = block
        del rng

    if gauge_idx:
        rows = np.array([descs[i]["id"] for i in gauge_idx], dtype=np.int64)
        gen = np.random.default_rng(np.random.SeedSequence(SEED + rows))
        level = gen.uniform(5.0, 200.0, size=rows.size)
        current = gen.uniform(5.0, 200.0, size=rows.size)
        shock = gen.normal(0.0, 1.2, size=(rows.size, SAMPLES))
        pull = 1.0 - PERSISTENCE
        block = np.empty((rows.size, SAMPLES), dtype=np.float64)
        block[:, 0] = current
        for t in range(1, SAMPLES):
            current = PERSISTENCE * current + pull * level + shock[:, t]
            block[:, t] = current
        values[gauge_idx] = np.maximum(0.0, block)

    return values
