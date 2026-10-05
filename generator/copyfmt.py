"""PostgreSQL binary COPY encoding.

Binary, not text: COPY's text parser would charge Postgres a parsing cost that
Prometheus's protobuf decoder does not pay, which would distort the ingest
comparison.

Header is b"PGCOPY\\n\\xff\\r\\n\\x00" + int32 flags + int32 header extension
length; trailer is int16 -1. Every integer is big-endian.
"""

import struct

PG_EPOCH_MS = 946684800000  # 2000-01-01T00:00:00Z as unix milliseconds

COPY_HEADER = b"PGCOPY\n\xff\r\n\x00" + struct.pack(">ii", 0, 0)
COPY_TRAILER = struct.pack(">h", -1)


def ts(ms):
    """timestamptz -> int64 microseconds since the Postgres epoch."""
    return struct.pack(">q", int(ms) * 1000 - PG_EPOCH_MS * 1000)


def int4(v):
    return struct.pack(">i", int(v))


def float8(v):
    return struct.pack(">d", float(v))


def text(s):
    return s.encode("utf-8")


def jsonb(s):
    """jsonb binary form: a single version byte followed by the JSON text.

    This is NOT the bare JSON text -- Postgres reads the first byte as a
    version number and rejects '{' (123) with "unsupported jsonb version
    number".

    Do NOT put an int32 length in here. `row` already emits the field length,
    and Postgres derives the payload size from it: the field length is
    1 + len(json), i.e. version byte plus text. Emitting a second length here
    makes Postgres read those four bytes as the leading characters of the JSON
    and fail with "invalid byte sequence for encoding UTF8: 0x00".

    Verified against `COPY ... TO STDOUT (FORMAT binary)` on PostgreSQL 18.6.
    """
    return b"\x01" + s.encode("utf-8")


def row(*fields):
    out = bytearray()
    out += struct.pack(">h", len(fields))
    for field in fields:
        out += struct.pack(">i", len(field)) + field
    return bytes(out)


def blob(rows):
    return COPY_HEADER + b"".join(rows) + COPY_TRAILER
