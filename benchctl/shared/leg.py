"""shared/leg.py — one identifier for one benchmark leg.

A leg is one (system, filesystem, extra) tuple: one database, one dataset,
one filesystem under its data directory, and whatever else the scenario
varies. Three places have to agree on the name:

  - the driver, which writes results/<kind>-<slug>.json
  - k6, which writes summary-<slug>.json
  - the verifier, which reads both back and compares them

Keeping the rule in one file is cheaper than keeping it in three heads, and
it is the kind of thing that silently breaks when a dimension is added and
nobody updates the third copy.

The extra part is what a scenario varies besides the filesystem: the number
of shards in the sharding scenario, for instance. It is a dict in the
scenario and a short string everywhere else.
"""


def extra_label(extra):
    """'shards: 8' -> 'shards8'. Empty for a leg with no extra dimension."""
    if not extra:
        return ""
    parts = []
    for key in sorted(extra):
        parts.append(f"{key}{extra[key]}")
    return "-".join(parts)


def leg_slug(system, fs_type=None, extra=None):
    """Filename-safe id for one leg.

    'postgres/jsonb' + 'xfs'                  -> 'postgres-jsonb-xfs'
    'postgres/sharded' + 'xfs' + shards=8     -> 'postgres-sharded-xfs-shards8'
    'prometheus'                              -> 'prometheus'
    """
    slug = system.replace("/", "-")
    if fs_type:
        slug = f"{slug}-{fs_type}"
    label = extra_label(extra)
    if label:
        slug = f"{slug}-{label}"
    return slug
