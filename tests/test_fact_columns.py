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


def test_caps_reach_the_resolver_even_when_the_adapter_omits_them_from_evidence(tmp_path):
    """11 of the 14 adapters set `caps` on the Deployment and emit no caps evidence.

    Without adopting that dict as a claim, step 4's projection never sees it and the
    old last-writer-wins path still decides those capabilities — the fix would look
    landed and change nothing for most of the registry.
    """
    import json

    s = Store(tmp_path / "f11.db")
    s.upsert_weights(Weights("hf:a/m", "m"))
    d = Deployment("p:x", "hf:a/m", "p", "x", caps={"tools": True})
    # Evidence for a *different* field: the sibling whose provenance gets borrowed.
    s.upsert_deployment(d, [_ev("deployment", "p:x", "context_window", 8192, 0.95,
                                via="provider_api")])
    s.commit()

    caps = json.loads(s.conn.execute(
        "SELECT caps FROM deployments WHERE deploy_id='p:x'").fetchone()["caps"])
    assert caps.get("tools") is True, "the adapter's caps never reached the resolver"
    adopted = s.conn.execute(
        "SELECT confidence, observed_via FROM evidence WHERE entity_id='p:x'"
        " AND field='cap.tools'").fetchone()
    assert adopted is not None
    assert adopted["observed_via"] == "provider_api", \
        "provenance must be borrowed from the adapter's own sibling claim"
    s.close()


def test_a_source_that_reports_nothing_does_not_erase_a_confirmation(tmp_path):
    """The defect this whole effort started from, end to end through the write path.

    `upsert_deployment` used to write the adapter's `caps` dict straight through, so
    a source publishing nothing about `tools` erased a confirmation another source
    had made. Now the adapter emits claims and the store resolves the conclusion —
    the seam the price already used.
    """
    import json

    from mininfer.ingest.shared import evidence as claims

    s = Store(tmp_path / "f9.db")
    s.upsert_weights(Weights("hf:a/m", "m"))

    good = Deployment("p:x", "hf:a/m", "p", "x",
                      caps={"tools": True, "structured": True})
    s.upsert_deployment(good, claims("deployment", "p:x", {"caps": good.caps},
                                     "src-a", None, _T, "aggregator_api"))
    s.commit()

    # A second source that says nothing at all about either capability.
    silent = Deployment("p:x", "hf:a/m", "p", "x",
                        caps={"tools": None, "structured": None})
    s.upsert_deployment(silent, claims("deployment", "p:x", {"caps": silent.caps},
                                       "src-b", None, _T, "aggregator_api"))
    s.commit()

    row = s.conn.execute(
        "SELECT caps, caps_state FROM deployments WHERE deploy_id='p:x'").fetchone()
    caps = json.loads(row["caps"])
    assert caps["tools"] is True, "a silent source erased a confirmed capability"
    assert caps["structured"] is True
    assert row["caps_state"] == "{}", "a canonical fact needs no state entry"
    s.close()


def test_a_dispute_is_visible_in_caps_state(tmp_path):
    """`caps` keeps the resolved value; `caps_state` carries only what is not canonical,
    so a reader can see how much to trust it without parsing anything else."""
    import json

    from mininfer.ingest.shared import evidence as claims

    s = Store(tmp_path / "f10.db")
    s.upsert_weights(Weights("hf:a/m", "m"))
    for src in ("src-a", "src-b"):
        d = Deployment("p:x", "hf:a/m", "p", "x", caps={"tools": True})
        s.upsert_deployment(d, claims("deployment", "p:x", {"caps": {"tools": True}},
                                      src, None, _T, "aggregator_api"))
    s.commit()
    # Same confidence band (0.85), conflicting values: a genuine dispute.
    d = Deployment("p:x", "hf:a/m", "p", "x", caps={"tools": False})
    s.upsert_deployment(d, claims("deployment", "p:x", {"caps": {"tools": False}},
                                  "src-c", None, _T, "aggregator_api"))
    s.commit()
    state = json.loads(s.conn.execute(
        "SELECT caps_state FROM deployments WHERE deploy_id='p:x'").fetchone()["caps_state"])
    assert state.get("tools") == "disputed", state
    s.close()


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


# --------------------------------------------------------------------------- #
# the structural gate (ticket 09 / handoff step 8)
# --------------------------------------------------------------------------- #


def test_a_model_that_produces_media_is_rejected_regardless_of_its_name(tmp_path):
    """`kling-v2.6-motion-control`, `seedance-*`, `veo-*`, `recraft-*`, `gpt-image-*`
    and `grok-imagine-image` carry none of the name tokens, so the token list cannot
    see them. What a model *produces* can.

    This test exists because the gate silently did nothing on its first
    implementation: `_json_field` is dict-only and returns `{}` for a JSON array, so
    every arm read an empty output list.
    """
    from mininfer.router import Policy, build_candidates

    s = Store(tmp_path / "gate.db")
    s.upsert_weights(Weights("hf:a/veo", "veo-3", output_modalities=("video",)))
    s.upsert_weights(Weights("hf:a/chat", "chat-9", output_modalities=("text",)))
    s.upsert_weights(Weights("hf:a/unknown", "mystery"))
    for wid in ("hf:a/veo", "hf:a/chat", "hf:a/unknown"):
        s.upsert_deployment(Deployment(wid.split("/")[-1], wid, "p", wid.split("/")[-1],
                                       zero_price=True, context_window=200000,
                                       caps={"tools": True}))
    s.commit()

    policy, tasks = Policy.load("config/policy.yaml")
    policy.require_callable = False
    pool = build_candidates(s, tasks["general_chat"], policy)
    verdicts = {c.deploy_id: c.rejected for c in pool}

    assert "output_modalities" in (verdicts["veo"] or ""), \
        "a video producer was not caught structurally"
    assert verdicts["chat"] is None, "a text producer must be admitted"
    assert verdicts["unknown"] is None, \
        "an arm with no reported output modality is unverified, not refuted"


def test_the_modality_requirement_separates_refuted_from_unreported(tmp_path):
    """An arm that reports modalities but not the required one is *refuted*; an arm
    that reports none at all is *unverified* and follows the policy. Collapsing the
    two is how a schema fix becomes a silent coverage loss."""
    from mininfer.router import Policy, build_candidates

    s = Store(tmp_path / "req.db")
    s.upsert_weights(Weights("hf:a/textonly", "t", modalities=("text",)))
    s.upsert_weights(Weights("hf:a/silent", "s"))
    for wid in ("hf:a/textonly", "hf:a/silent"):
        s.upsert_deployment(Deployment(wid.split("/")[-1], wid, "p", wid.split("/")[-1],
                                       zero_price=True, context_window=200000,
                                       caps={"structured": True}))
    s.commit()

    policy, tasks = Policy.load("config/policy.yaml")
    policy.require_callable = False
    task = tasks["shelf_image_audit"]
    verdicts = {c.deploy_id: c.rejected
                for c in build_candidates(s, task, policy)}
    assert "unsupported input modality" in (verdicts["textonly"] or "")
    assert verdicts["silent"] is None, \
        "an unreported modality is not the same as a refuted one"


# --------------------------------------------------------------------------- #
# the resolver's output must reach the router (the two gaps found after step 8)
# --------------------------------------------------------------------------- #


def test_a_disputed_capability_is_not_silently_treated_as_canonical(tmp_path):
    """Ticket 06 and 13 both decided `disputed` follows `on_unverified_capability`.

    It was recorded and never read: `_reject` consulted only the value, so a fact
    two sources disagreed about was served as though it were settled. A state
    machine nothing consumes is not a mechanism.
    """
    from mininfer.router import Policy, build_candidates

    s = Store(tmp_path / "dis.db")
    s.upsert_weights(Weights("hf:a/m", "m"))
    d = Deployment("p:x", "hf:a/m", "p", "x", zero_price=True, context_window=200000)
    s.upsert_deployment(d)
    # Equal confidence, conflicting values: a genuine dispute, not a loss.
    s._add_evidence([
        _ev("deployment", "p:x", "cap.structured", True, 0.7, source="gateway-a"),
        _ev("deployment", "p:x", "cap.structured", False, 0.7, source="gateway-b"),
    ])
    s.commit()
    s.resolve_facts()
    s.commit()
    assert _facts(s, "deployment", "p:x", "cap.structured")["state"] == "disputed"

    policy, tasks = Policy.load("config/policy.yaml")
    policy.require_callable = False
    # `sql_generation` requires `structured` and rejects an unverified one.
    verdicts = {c.deploy_id: c.rejected for c in build_candidates(s, tasks["sql_generation"], policy)}
    assert verdicts["p:x"] and "disputed" in verdicts["p:x"], \
        f"a disputed capability was served as canonical: {verdicts['p:x']!r}"
    s.close()


def test_resolve_facts_projects_into_the_column_the_router_reads(tmp_path):
    """Writing `fact_resolution` without projecting leaves the router reading a
    column the resolver does not maintain, so a re-run changes nothing anyone uses."""
    import json as _json

    s = Store(tmp_path / "proj.db")
    s.upsert_weights(Weights("hf:a/m", "m"))
    s.upsert_deployment(Deployment("p:x", "hf:a/m", "p", "x"))
    s._add_evidence([_ev("deployment", "p:x", "cap.tools", True, 0.95, via="provider_api")])
    s.commit()

    rep = s.resolve_facts()
    s.commit()
    assert rep["projected"] == 1

    row = s.conn.execute(
        "SELECT caps, caps_state FROM deployments WHERE deploy_id='p:x'").fetchone()
    assert _json.loads(row["caps"])["tools"] is True
    assert row["caps_state"] == "{}"

    # And a change in evidence is reflected on a re-run, which is the point.
    s._add_evidence([_ev("deployment", "p:x", "cap.tools", False, 0.95, when="2026-11-01T00:00:00+00:00")])
    s.commit()
    s.resolve_facts()
    s.commit()
    row = s.conn.execute(
        "SELECT caps FROM deployments WHERE deploy_id='p:x'").fetchone()
    assert _json.loads(row["caps"])["tools"] is False, \
        "a re-run did not reach the column the router reads"
    s.close()


def test_the_resolver_can_pool_modality_across_the_deployments_of_an_artifact(tmp_path):
    """Weights-scoped pooling had no input until modality became evidence.

    The adapters set modality on the `Weights` object and nothing emitted it as a
    claim, so the scope that is *supposed* to pool across every deployment of an
    artifact had nothing to pool — tested, and inert against real data.
    """
    from mininfer.schema import Evidence

    s = Store(tmp_path / "pool.db")
    anchor = Evidence("weights", "hf:a/omni", "display_name", "omni", "src", "u", _T,
                      0.95, "provider_api")
    s.upsert_weights(Weights("hf:a/omni", "omni", modalities=("text", "image"),
                             output_modalities=("text", "audio")), [anchor])
    s.commit()

    fields = {r["field"]: r["value"] for r in s.conn.execute(
        "SELECT field, value FROM evidence WHERE entity_kind='weights'"
        " AND entity_id='hf:a/omni'").fetchall()}
    assert "modality.in" in fields and "audio" not in fields["modality.in"]
    assert "modality.out" in fields and "audio" in fields["modality.out"]

    # And the resolver now pools them onto the artifact.
    s.upsert_deployment(Deployment("p:x", "hf:a/omni", "p", "x", zero_price=True))
    s.commit()
    s.resolve_facts()
    s.commit()
    pooled = _facts(s, "weights", "hf:a/omni", "modality.out")
    assert pooled is not None and "audio" in pooled["value"]
    s.close()


def test_modality_evidence_is_not_attributed_to_nobody(tmp_path):
    """With no sibling claim to borrow provenance from, nothing is recorded — a fact
    attributed to a source that said nothing is worse than an unrecorded one."""
    s = Store(tmp_path / "noanchor.db")
    s.upsert_weights(Weights("hf:a/m", "m", modalities=("text", "image")))
    s.commit()
    n = s.conn.execute(
        "SELECT COUNT(*) c FROM evidence WHERE entity_kind='weights'"
        " AND field LIKE 'modality.%'").fetchone()["c"]
    assert n == 0
    s.close()
