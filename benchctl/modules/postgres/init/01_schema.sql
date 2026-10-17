-- Two PostgreSQL schemas for the same metric dataset, each time partitioned
-- so that partitioning is a constant rather than a variable. They bracket the
-- design space: what most people write first (labels as a jsonb document) and
-- the best Postgres can do (identity in its own table, BRIN on time).
--
-- Partitions are created by the loader at run time, because the dataset is
-- timestamped in the future and init only runs against an empty PGDATA.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Schema 1: generic. Labels in a jsonb document, GIN for containment lookups.
-- This is what most people write first.
CREATE TABLE IF NOT EXISTS metrics_jsonb (
    time    timestamptz      NOT NULL,
    labels  jsonb            NOT NULL,
    value   double precision NOT NULL
) PARTITION BY RANGE (time);

CREATE INDEX IF NOT EXISTS metrics_jsonb_labels_idx
    ON metrics_jsonb USING gin (labels jsonb_path_ops);

CREATE INDEX IF NOT EXISTS metrics_jsonb_time_idx
    ON metrics_jsonb (time);

-- Schema 2: normalised. Series identity lives in its own dimension table,
-- exactly as Prometheus keeps identity separate from samples. BRIN on time is
-- the Postgres-native answer to append-only time-ordered data: the index
-- stores min/max per page and costs kilobytes instead of megabytes. It also
-- degrades badly here, because samples are written per series rather than in
-- time order; that is a result worth measuring, not a bug in the index.
CREATE TABLE IF NOT EXISTS series_dim (
    id       int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    metric   text NOT NULL,
    job      text NOT NULL,
    instance text NOT NULL,
    region   text NOT NULL,
    service  text NOT NULL,
    UNIQUE (metric, job, instance)
);

CREATE TABLE IF NOT EXISTS metrics_norm_brin (
    time      timestamptz      NOT NULL,
    series_id int             NOT NULL,
    value     double precision NOT NULL
) PARTITION BY RANGE (time);

CREATE INDEX IF NOT EXISTS metrics_norm_brin_time_brin_idx
    ON metrics_norm_brin USING brin (time) WITH (pages_per_range = 32);

CREATE INDEX IF NOT EXISTS metrics_norm_brin_series_time_idx
    ON metrics_norm_brin (series_id, time);

-- Helper used by the loader to create daily partitions on demand.
CREATE OR REPLACE FUNCTION bench_ensure_partitions(
    parent regclass,
    day    date
) RETURNS void AS $fn$
DECLARE
    lo   timestamptz := day::timestamp;
    hi   timestamptz := (day + 1)::timestamp;
    name text        := parent::text || '_' || to_char(day, 'YYYYMMDD');
BEGIN
    IF to_regclass(name) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF %s FOR VALUES FROM (%L) TO (%L)',
            name, parent::text, lo, hi);
    END IF;
END;
$fn$ LANGUAGE plpgsql;
