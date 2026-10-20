#!/usr/bin/env bash
# Set up PostgreSQL 18 on a given data dir + FS, tuned for this benchmark.
# Usage: setup-pg.sh <datadir> <waldir> [port] [io_method]
set -euo pipefail

DATA_DIR="${1:?data dir}"
WAL_DIR="${2:?wal dir}"
PORT="${3:-5433}"
IO_METHOD="${4:-sync}"   # PG18: sync | worker | io_uring

SOCKET_DIR="$(mktemp -d /tmp/pgsock.XXXXXX)"
PG_BIN="$(ls -d /usr/lib/postgresql/18/bin 2>/dev/null || echo /usr/local/bin)"

export PATH="$PG_BIN:$PATH"

initdb -D "$DATA_DIR" --encoding=UTF8 --no-locale --no-instructions \
    --auth=trust --username=postgres

# WAL lives on the second mount (symlinked, the standard post-initdb trick)
mv "$DATA_DIR/pg_wal" "$WAL_DIR/pg_wal"
ln -s "$WAL_DIR/pg_wal" "$DATA_DIR/pg_wal"

cat >> "$DATA_DIR/postgresql.conf" <<EOF
port = $PORT
listen_addresses = 'localhost'
unix_socket_directories = '$SOCKET_DIR'
shared_buffers = 512MB
max_wal_size = 2GB
min_wal_size = 512MB
wal_compression = off
checkpoint_timeout = 5min
synchronous_commit = on
io_method = $IO_METHOD
shared_preload_libraries = 'pg_stat_statements'
max_connections = 100
autovacuum_vacuum_cost_delay = 0
logging_collector = off
EOF

pg_ctl -D "$DATA_DIR" -l "$DATA_DIR/server.log" -w start

createdb -p "$PORT" -h "$SOCKET_DIR" -U postgres bench
psql -p "$PORT" -h "$SOCKET_DIR" -U postgres -d bench \
    -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"

echo "SOCKET=$SOCKET_DIR"
echo "PORT=$PORT"
echo "DSN=postgresql://postgres@/bench?host=$SOCKET_DIR&port=$PORT"
