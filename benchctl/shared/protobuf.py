"""Minimal remote-write protobuf encoder.

Only the subset needed for float samples is implemented, so no protoc run and no
generated code are required. Field numbers come from tsdb/prompb/types.proto:

    WriteRequest { repeated TimeSeries timeseries = 1; }
    TimeSeries   { repeated Label labels = 1; repeated Sample samples = 2; }
    Label        { string name = 1; string value = 2; }
    Sample       { double value = 1; int64 timestamp = 2; }

Snappy is mandatory on the wire: the receiver calls snappy.Decode
unconditionally (storage/remote/codec.go). cramjam is used when available; a
literal-only encoder is the fallback, which is valid snappy but does not
compress.
"""

import struct

try:
    import cramjam

    _HAVE_CRAMJAM = True
except ImportError:  # pragma: no cover
    cramjam = None
    _HAVE_CRAMJAM = False


def varint(value):
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(field, wire):
    return varint((field << 3) | wire)


def _bytes_field(field, payload):
    return _tag(field, 2) + varint(len(payload)) + payload


def _double_field(field, value):
    return _tag(field, 1) + struct.pack("<d", value)


def _int64_field(field, value):
    return _tag(field, 0) + varint(value)


def encode_label(name, value):
    return _bytes_field(1, name.encode()) + _bytes_field(2, value.encode())


def encode_sample(value, timestamp_ms):
    return _double_field(1, value) + _int64_field(2, timestamp_ms)


def encode_timeseries(labels, samples):
    """labels: iterable of (name, value). samples: iterable of (value, ts_ms)."""
    out = bytearray()
    for name, value in labels:
        out += _bytes_field(1, encode_label(name, value))
    for value, ts in samples:
        out += _bytes_field(2, encode_sample(value, ts))
    return bytes(out)


def encode_write_request(timeseries):
    """WriteRequest.timeseries is *repeated*, so each TimeSeries is its own
    field-1 entry.

    Wrapping the concatenation in a single field 1 parses as one TimeSeries
    whose label list is the union of every series' labels and whose sample list
    is the union of every series' samples. Prometheus accepts that silently and
    then rejects it as "duplicated_label=__name__", which is why a whole run can
    report 100% sent while the TSDB stays empty.
    """
    return b"".join(_bytes_field(1, ts) for ts in timeseries)


def _snappy_literal(data):
    """Emit data as snappy literals. Valid snappy, but no compression."""
    out = bytearray()
    out += varint(len(data))
    pos = 0
    total = len(data)
    while pos < total:
        length = min(65536, total - pos)
        if length <= 60:
            out.append((length - 1) << 2)
        elif length < 1 << 8:
            out.append(0xFC)
            out += bytes([length - 1])
        elif length < 1 << 16:
            out.append(0xFD)
            out += struct.pack("<H", length - 1)
        else:
            out.append(0xFE)
            out += struct.pack("<I", length - 1)[:3]
        out += data[pos : pos + length]
        pos += length
    return bytes(out)


def snappy_compress(data):
    """Raw-block snappy, which is what the receiver expects.

    `cramjam.snappy.compress` emits the *framed* stream format -- it starts
    with the stream identifier ff060000734e61507059 -- and Go's snappy.Decode,
    which storage/remote/codec.go:78 calls unconditionally, decodes the raw
    block format. Framing the payload makes the receiver fail with
    "s2: corrupt input". compress_raw is the correct entry point; compress is
    only a fallback for cramjam builds that predate it.
    """
    if _HAVE_CRAMJAM:
        raw = getattr(cramjam.snappy, "compress_raw", None)
        if raw is not None:
            return bytes(raw(data))
        return bytes(cramjam.snappy.compress(data))
    return _snappy_literal(data)


def real_compression():
    """False means literals only, i.e. no real compression on the wire."""
    return _HAVE_CRAMJAM
