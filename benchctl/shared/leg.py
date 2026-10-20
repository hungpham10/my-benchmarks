"""shared/leg.py — one identifier for one benchmark leg.

A leg is one (system, filesystem) pair: one database, one dataset, one
filesystem under its data directory. Three places have to agree on the name:

  - the driver, which writes results/<kind>-<slug>.json
  - k6, which writes summary-<slug>.json
  - the verifier, which reads both back and compares them

Keeping the rule in one file is cheaper than keeping it in three heads, and
it is the kind of thing that silently breaks when a filesystem dimension is
added and nobody updates the third copy.
"""


def leg_slug(system, fs_type=None):
    """Filename-safe id for one leg.

    'postgres/jsonb' + 'xfs' -> 'postgres-jsonb-xfs'
    'prometheus'              -> 'prometheus'
    """
    slug = system.replace("/", "-")
    if fs_type:
        slug = f"{slug}-{fs_type}"
    return slug
