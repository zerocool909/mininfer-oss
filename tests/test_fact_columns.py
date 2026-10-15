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


# --------------------------------------------------------------------------- #
# the fact resolver
# --------------------------------------------------------------------------- #

_T = "2026-10-03T20:31:20+00:00"


def _ev(kind, eid, field, value, confidence, *, via="aggregator_api",
        source="s", when=_T, url=None):
    from mininfer.schema import Evidence
    return Evidence(kind, eid, field, value, source, url, when, confidence, via)


def _facts(s, entity_kind, entity_id, field):
    return s.conn.execute(
        "SELECT value, state, confidence, voters FROM fact_resolution"
        " WHERE entity_kind=? AND entity_id=? AND field=?",
        (entity_kind, entity_id, field)).fetchone()


def test_an_unreported_capability_is_not_a_claim(tmp_path):
    """The original defect. A `null` inside a legacy caps blob used to be recorded
    as a claim and overwrite a confirmation from a source that had reported it."""
    s = Store(tmp_path / "f1.db")
    s.upsert_deployment(Deployment("p:x", "hf:a/m", "p", "x"))
    s._add_evidence([
        _ev("deployment", "p:x", "caps", {"tools": True, "structured": None}, 0.7),
    ])
    s.commit()
    s.resolve_facts()
    s.commit()
    assert _facts(s, "deployment", "p:x", "cap.tools")["value"] == "true"
    assert _facts(s, "deployment", "p:x", "cap.structured") is None, \
        "an unreported capability became a resolved fact"
    s.close()


def test_no_evidence_means_no_row_not_a_null_value(tmp_path):
    """Unverified is the *absence* of a resolution. Storing NULL would make it
    indistinguishable from a fact we resolved to nothing."""
    s = Store(tmp_path / "f2.db")
    s.upsert_deployment(Deployment("p:x", "hf:a/m", "p", "x"))
    s.commit()
    rep = s.resolve_facts()
    s.commit()
    assert rep["facts"] == 0
    assert s.conn.execute("SELECT COUNT(*) c FROM fact_resolution").fetchone()["c"] == 0
    s.close()


def test_refuted_is_a_value_not_a_state(tmp_path):
    s = Store(tmp_path / "f3.db")
    s.upsert_deployment(Deployment("p:x", "hf:a/m", "p", "x"))
    s._add_evidence([_ev("deployment", "p:x", "cap.tools", False, 0.85)])
    s.commit()
    s.resolve_facts()
    s.commit()
    got = _facts(s, "deployment", "p:x", "cap.tools")
    assert got["value"] == "false" and got["state"] == "canonical"
    s.close()


def test_a_weaker_source_cannot_demote_a_stronger_one(tmp_path):
    """No margin: higher confidence wins outright, because a boolean has no
    magnitude to compare against."""
    s = Store(tmp_path / "f4.db")
    s.upsert_deployment(Deployment("p:x", "hf:a/m", "p", "x"))
    s._add_evidence([
        _ev("deployment", "p:x", "cap.tools", False, 0.4, via="llm_extraction"),
        _ev("deployment", "p:x", "cap.tools", True, 0.95, via="provider_api"),
    ])
    s.commit()
    s.resolve_facts()
    s.commit()
    got = _facts(s, "deployment", "p:x", "cap.tools")
    assert got["value"] == "true" and got["state"] == "canonical"
    s.close()


def test_a_replayed_older_snapshot_does_not_win_by_arrival_order(tmp_path):
    """Ordering is `(confidence, fetched_at, id)`, so a row inserted last but stamped
    earlier loses — it carries its original observation time."""
    s = Store(tmp_path / "f5.db")
    s.upsert_deployment(Deployment("p:x", "hf:a/m", "p", "x"))
    s._add_evidence([
        _ev("deployment", "p:x", "cap.tools", True, 0.85, when="2026-10-01T00:00:00+00:00"),
        _ev("deployment", "p:x", "cap.tools", False, 0.85, when="2026-09-01T00:00:00+00:00"),
    ])
    s.commit()
    s.resolve_facts()
    s.commit()
    assert _facts(s, "deployment", "p:x", "cap.tools")["value"] == "true"
    s.close()


def test_equals_disagreeing_is_disputed_not_an_arbitrary_winner(tmp_path):
    s = Store(tmp_path / "f6.db")
    s.upsert_deployment(Deployment("p:x", "hf:a/m", "p", "x"))
    s._add_evidence([
        _ev("deployment", "p:x", "cap.structured", True, 0.7, source="gateway-a"),
        _ev("deployment", "p:x", "cap.structured", False, 0.7, source="gateway-b"),
    ])
    s.commit()
    s.resolve_facts()
    s.commit()
    got = _facts(s, "deployment", "p:x", "cap.structured")
    assert got["state"] == "disputed"
    assert got["voters"] == 2, "voters counts upstreams, and both must be counted"
    s.close()


def test_a_rerun_records_no_history_when_nothing_changed(tmp_path):
    """Change-only, like `price_history`: a re-confirmation is not a new belief."""
    s = Store(tmp_path / "f7.db")
    s.upsert_deployment(Deployment("p:x", "hf:a/m", "p", "x"))
    s._add_evidence([_ev("deployment", "p:x", "cap.tools", True, 0.85)])
    s.commit()
    first = s.resolve_facts()
    s.commit()
    second = s.resolve_facts()
    s.commit()
    assert first["history"] == 1 and second["history"] == 0
    s.close()


def test_a_weights_scoped_fact_pools_peers_and_a_gateway_scoped_one_does_not(tmp_path):
    """A modality is a property of the artifact, so every deployment of the same
    weights is a peer. A capability is a property of the gateway, so one gateway
    cannot answer for another.

    This is the asymmetry ticket 06 established: the pricing rule — "a peer never
    supplies a value" — is right for `cap.*` and wrong for `modality.*`.
    """
    s = Store(tmp_path / "f8.db")
    # The weights row must exist: `deployments()` reads the `weights_resolved` view,
    # so a deployment whose artifact is unknown is not visible at all.
    s.upsert_weights(Weights("hf:a/m", "m"))
    for prov in ("gateway-a", "gateway-b"):
        s.upsert_deployment(Deployment(f"{prov}:m", "hf:a/m", prov, "m"))
    s._add_evidence([
        _ev("deployment", "gateway-a:m", "modality.out", ["text", "audio"], 0.85,
            source="gateway-a"),
        _ev("deployment", "gateway-a:m", "cap.tools", True, 0.85, source="gateway-a"),
    ])
    s.commit()
    s.resolve_facts()
    s.commit()

    pooled = _facts(s, "weights", "hf:a/m", "modality.out")
    assert pooled is not None and "audio" in pooled["value"], \
        "a weights-scoped fact did not pool onto the artifact"
    assert _facts(s, "deployment", "gateway-b:m", "cap.tools") is None, \
        "a gateway-scoped fact leaked across gateways"
    s.close()
