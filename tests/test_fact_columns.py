"""The fact layer's schema: modality direction and capability trust state.

Two columns, both additive and both invisible to the router at this stage:

* `weights.output_modalities` — modality is directional, and `modalities` has
  always been the *input* half. Five sources publish an output list and every
  adapter discarded it, so an image generator and a vision-capable text model
  were byte-identical in the registry.
* `deployments.caps_state` — `caps` holds the resolved value and keeps its
  three-valued shape; this holds only the entries that are not `canonical`.

These pin the migration, because `CREATE TABLE IF NOT EXISTS` is a no-op against
a registry that already exists — which is the case that matters for a live
deployment.
"""
from __future__ import annotations

from mininfer.schema import Deployment, Weights
from mininfer.store import Store

NEW_COLUMNS = (("deployments", "caps_state"), ("weights", "output_modalities"))


def test_a_fresh_registry_has_the_fact_columns(tmp_path):
    s = Store(tmp_path / "fresh.db")
    for table, column in NEW_COLUMNS:
        assert column in s.conn.columns(table), f"{table}.{column} missing on a fresh registry"
    s.close()


def test_an_existing_registry_gains_them_without_losing_data(tmp_path):
    """The migration path: a real old registry must gain the columns in place.

    `Store` caches initialised paths in `_INITIALIZED_DBS` for the process
    lifetime, so a second `Store()` on the same path skips both the DDL and
    `_migrate` — dropping the cache is what emulates a fresh process opening an
    existing registry, which is the case that actually matters.
    """
    from mininfer.store import _INITIALIZED_DBS

    path = tmp_path / "old.db"
    s = Store(path)
    s.upsert_weights(Weights("hf:a/m", "m", modalities=("text", "image")))
    s.upsert_deployment(Deployment("p:x", "hf:a/m", "p", "x",
                                   caps={"tools": True, "structured": None}))
    s.commit()
    # Simulate the pre-migration shape, then forget the path as a new process would.
    for table, column in NEW_COLUMNS:
        s.conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    s.commit()
    s.close()
    _INITIALIZED_DBS.discard(str(path.resolve()))

    s = Store(path)
    for table, column in NEW_COLUMNS:
        assert column in s.conn.columns(table), f"{table}.{column} was not migrated in"

    # The rows that were already there are intact, and the new columns default
    # to "unreported" rather than to a value nobody observed.
    w = s.conn.execute(
        "SELECT modalities, output_modalities FROM weights WHERE weights_id='hf:a/m'"
    ).fetchone()
    assert "image" in w["modalities"], "the input half survived"
    assert w["output_modalities"] == "[]", "unreported, not assumed text"

    d = s.conn.execute(
        "SELECT caps, caps_state FROM deployments WHERE deploy_id='p:x'").fetchone()
    assert "tools" in d["caps"]
    assert d["caps_state"] == "{}", "no state until something resolves the facts"
    s.close()
