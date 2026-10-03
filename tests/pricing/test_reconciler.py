"""P2 — the reconciler: a peer's price is a signal, never a price.

`test_price_units.py` pins the unit. `test_pricing_evidence_store.py` pins that
observations are appended and stored. This file pins the *decision*:

* the resolved value is always one of the deployment's **own** observations;
* an independent-source comparison may refuse the newest observation
  (`quarantined`), and the deployment keeps its own previous value;
* it never adopts the market's number, however confident the market is.

The last point is the whole design. Four honest deployments can produce a median
that exists nowhere, so it can detect an outlier and must never become a price.
"""
from __future__ import annotations

import datetime as dt

import pytest

from mininfer import store as store_module
from mininfer.pricing.providers import read_prices
from mininfer.schema import Deployment, Weights
from mininfer.store import Store

T0 = "2026-10-01T00:00:00+00:00"
T1 = "2026-10-02T00:00:00+00:00"


def _add(s: Store, deploy_id: str, *, price_in: float, price_out: float | None = None,
         source: str, observed_at: str = T1, weights: str = "w") -> None:
    """Insert one deployment and one price observation from `source`.

    `price_in` is dollars per Mtok; Novita's contract is 1e-4 USD/Mtok, so the raw
    value is `dollars * 10_000` rounded to an exact integer — which is what keeps
    the stored float exactly 0.075 rather than 0.07500000000000001.
    """
    price_out = price_in if price_out is None else price_out
    provider, model = deploy_id.split(":", 1)
    s.upsert_weights(Weights(weights, weights))
    s.upsert_deployment(
        Deployment(deploy_id, weights, provider, model),
        prices=read_prices(
            "novita",
            {"input_token_price_per_m": round(price_in * 10_000),
             "output_token_price_per_m": round(price_out * 10_000)},
            source=source,
            observed_at=observed_at,
        ),
    )


def _resolution(s: Store, deploy_id: str, kind: str = "input") -> dict:
    return dict(s.conn.execute(
        "SELECT * FROM price_resolution WHERE deploy_id=? AND kind=?",
        (deploy_id, kind),
    ).fetchone())


def _deployment(s: Store, deploy_id: str) -> dict:
    return dict(s.conn.execute(
        "SELECT price_in, price_out, zero_price, status, status_reason"
        " FROM deployments WHERE deploy_id=?", (deploy_id,),
    ).fetchone())


def _three_source_market(s: Store, *, bad: float = 0.75, good: float = 0.075) -> None:
    """`novita` is 10x high; `openrouter` and `deepinfra` agree."""
    _add(s, "novita:ling", price_in=bad, source="novita")
    _add(s, "openrouter:ling", price_in=good, source="openrouter")
    _add(s, "deepinfra:ling", price_in=good, source="deepinfra")
    s.commit()


# ------------------------------------------------- the Novita regression


def test_a_ten_x_source_is_quarantined(tmp_path):
    """The unit-error scale. A source 10x the market is refused, not believed."""
    s = Store(tmp_path / "t.db")
    _three_source_market(s)
    s.reconcile_prices()
    s.commit()

    r = _resolution(s, "novita:ling")
    assert r["state"] == "quarantined"
    assert r["factor"] == pytest.approx(10.0)
    assert r["source_count"] == 3
    s.close()


def test_the_quarantine_never_adopts_the_market_price(tmp_path):
    """The central rule, asserted directly.

    With no earlier trusted value the resolution is NULL — the arm becomes
    *unpriced*. It must not become 0.075 just because three other deployments
    publish it: that number belongs to them.
    """
    s = Store(tmp_path / "t.db")
    _three_source_market(s)
    s.reconcile_prices()
    s.commit()

    r = _resolution(s, "novita:ling")
    assert r["usd_per_mtok"] is None
    assert r["usd_per_mtok"] != pytest.approx(0.075)
    assert _deployment(s, "novita:ling")["price_in"] is None
    s.close()


def test_quarantine_falls_back_to_the_arm_own_previous_value(tmp_path):
    """Not the market's number — this deployment's own last trusted observation."""
    s = Store(tmp_path / "t.db")
    _add(s, "novita:ling", price_in=0.09, source="novita", observed_at=T0)
    _add(s, "novita:ling", price_in=0.75, source="novita", observed_at=T1)  # bad
    _add(s, "openrouter:ling", price_in=0.075, source="openrouter")
    _add(s, "deepinfra:ling", price_in=0.075, source="deepinfra")
    s.commit()
    s.reconcile_prices()
    s.commit()

    r = _resolution(s, "novita:ling")
    assert r["state"] == "quarantined"
    assert r["usd_per_mtok"] == pytest.approx(0.09)      # its own previous value
    assert r["usd_per_mtok"] != pytest.approx(0.075)     # NOT the market median
    s.close()


def test_the_honest_arms_are_not_flagged_by_one_bad_source(tmp_path):
    """Why the reference is the market median *including* self.

    A peer-only median over the two good arms would be dragged halfway to the bad
    one, flagging them as 5.5x deviant. Including self gives [0.075, 0.075, 0.75],
    whose median is 0.075 — so the good arms sit at factor 1.0 and only the bad
    source is refused.
    """
    s = Store(tmp_path / "t.db")
    _three_source_market(s)
    s.reconcile_prices()
    s.commit()

    for did in ("openrouter:ling", "deepinfra:ling"):
        r = _resolution(s, did)
        assert r["state"] == "canonical", did
        assert r["factor"] == pytest.approx(1.0), did
        assert _deployment(s, did)["price_in"] == pytest.approx(0.075)
    s.close()


def test_a_false_hibernation_is_undone_by_reconcile(tmp_path):
    """The incident that started this: free -> a bogus 10x price -> "was free, now
    $0.7500" and the arm leaves the ranking. Reconcile recognises the anomaly,
    falls back to the free observation, and puts the arm back."""
    s = Store(tmp_path / "t.db")
    _add(s, "openrouter:ling", price_in=0.075, source="openrouter")
    _add(s, "deepinfra:ling", price_in=0.075, source="deepinfra")
    _add(s, "novita:ling", price_in=0.0, price_out=0.0, source="novita", observed_at=T0)
    s.commit()
    assert _deployment(s, "novita:ling")["zero_price"] == 1

    # The buggy adapter read: 10x too expensive. The provisional write hibernates.
    _add(s, "novita:ling", price_in=0.75, source="novita", observed_at=T1)
    s.commit()
    mid = _deployment(s, "novita:ling")
    assert mid["status"] == "hibernated" and "$0.7500" in mid["status_reason"]

    rep = s.reconcile_prices()
    s.commit()
    after = _deployment(s, "novita:ling")
    assert after["status"] == "live"
    assert after["zero_price"] == 1
    assert after["price_in"] == pytest.approx(0.0)
    assert rep["states"].get("quarantined") == 2   # input and output both refused
    s.close()


# ------------------------------------------------------- real changes


def test_a_repricing_confirmed_across_sources_is_canonical(tmp_path):
    """A big change the whole market agrees with is a repricing, not a unit bug."""
    s = Store(tmp_path / "t.db")
    for name in ("novita", "openrouter", "deepinfra"):
        _add(s, f"{name}:m", price_in=0.075, source=name, observed_at=T0)
        _add(s, f"{name}:m", price_in=0.75, source=name, observed_at=T1)
    s.commit()
    rep = s.reconcile_prices()
    s.commit()

    r = _resolution(s, "novita:m")
    assert r["state"] == "canonical"
    assert r["usd_per_mtok"] == pytest.approx(0.75)
    assert "repricing confirmed" in r["reason"]
    assert rep["states"]["canonical"] == 6
    s.close()


def test_a_sudden_jump_with_too_few_sources_is_suspect(tmp_path):
    """One source cannot form a market. The value is kept, but flagged."""
    s = Store(tmp_path / "t.db")
    _add(s, "novita:m", price_in=0.075, source="novita", observed_at=T0)
    _add(s, "novita:m", price_in=0.75, source="novita", observed_at=T1)
    s.commit()
    s.reconcile_prices()
    s.commit()

    r = _resolution(s, "novita:m")
    assert r["state"] == "suspect"
    assert r["source_count"] == 1
    assert r["usd_per_mtok"] == pytest.approx(0.75)   # kept, not refused
    s.close()


def test_a_stale_observation_is_expired_but_kept(tmp_path):
    """A stale price ranks better than none, but must not read as fresh."""
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(days=store_module.PRICE_FRESHNESS_DAYS + 10)).isoformat(timespec="seconds")
    s = Store(tmp_path / "t.db")
    for name in ("novita", "openrouter", "deepinfra"):
        _add(s, f"{name}:m", price_in=0.075, source=name, observed_at=old)
    s.commit()
    s.reconcile_prices()
    s.commit()

    r = _resolution(s, "novita:m")
    assert r["state"] == "expired"
    assert r["usd_per_mtok"] == pytest.approx(0.075)
    assert "old" in r["reason"]
    s.close()


# ------------------------------------------------------- source independence


def test_a_gateway_gets_one_vote_not_one_per_endpoint(tmp_path):
    """Two endpoints of one gateway share a unit risk, so they are one data point.

    `openrouter/a` must not be able to vouch for `openrouter/b`: with one vote per
    source the market is {openrouter, deepinfra, novita} and the outlier endpoint is
    refused rather than confirmed by its own twin.
    """
    s = Store(tmp_path / "t.db")
    _add(s, "openrouter/a:m", price_in=0.075, source="openrouter")
    _add(s, "openrouter/b:m", price_in=9.0, source="openrouter")
    _add(s, "deepinfra:c:m", price_in=0.075, source="deepinfra")
    _add(s, "novita:d:m", price_in=0.075, source="novita")
    s.commit()
    s.reconcile_prices()
    s.commit()

    assert _resolution(s, "novita:d:m")["source_count"] == 3   # not 4
    assert _resolution(s, "openrouter/b:m")["state"] == "quarantined"
    assert _resolution(s, "openrouter/a:m")["state"] == "canonical"
    s.close()


# ------------------------------------------------------- the mechanics


def test_reconcile_is_idempotent(tmp_path):
    s = Store(tmp_path / "t.db")
    _three_source_market(s)
    s.reconcile_prices()
    s.commit()
    first = _resolution(s, "novita:ling")
    s.reconcile_prices()
    s.commit()
    assert _resolution(s, "novita:ling") == first
    s.close()


def test_thresholds_are_re_derivable(tmp_path, monkeypatch):
    """The decision is derived from stored evidence, so changing a threshold is a
    re-run — not a migration. This is what `mi reconcile` exists for."""
    s = Store(tmp_path / "t.db")
    _three_source_market(s)
    s.reconcile_prices()
    s.commit()
    assert _resolution(s, "novita:ling")["state"] == "quarantined"

    monkeypatch.setattr(store_module, "PRICE_PEER_QUARANTINE_FACTOR", 1_000.0)
    monkeypatch.setattr(store_module, "PRICE_PEER_SUSPECT_FACTOR", 1_000.0)
    s.reconcile_prices()
    s.commit()
    assert _resolution(s, "novita:ling")["state"] == "canonical"
    s.close()


def test_deployments_without_evidence_are_untouched(tmp_path):
    """A seed or an agent-ingested row keeps the price its caller supplied."""
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "w"))
    s.upsert_deployment(Deployment("seed:m", "w", "seed", "m", price_in=1.0, price_out=2.0))
    s.commit()
    rep = s.reconcile_prices()
    s.commit()

    assert rep["deployments"] == 0
    assert _deployment(s, "seed:m")["price_in"] == pytest.approx(1.0)
    s.close()


def test_reconcile_can_narrow_to_given_deployments(tmp_path):
    s = Store(tmp_path / "t.db")
    _three_source_market(s)
    s.reconcile_prices(deploy_ids=["novita:ling"])
    s.commit()
    assert _resolution(s, "novita:ling")["state"] == "quarantined"
    assert s.conn.execute(
        "SELECT COUNT(*) c FROM price_resolution WHERE deploy_id='openrouter:ling'"
    ).fetchone()["c"] == 0
    s.close()


def test_the_resolution_is_visible_through_the_view(tmp_path):
    s = Store(tmp_path / "t.db")
    _three_source_market(s)
    s.reconcile_prices()
    s.commit()
    row = dict(s.conn.execute(
        "SELECT price_in_canonical, price_in_state, price_in_source"
        " FROM deployments_priced WHERE deploy_id='novita:ling'"
    ).fetchone())
    assert row["price_in_canonical"] is None
    assert row["price_in_state"] == "quarantined"
    assert row["price_in_source"] is None
    s.close()


# ------------------------------------------------- the ingest wiring


def test_ingest_reconciles_once_after_every_source(tmp_path, monkeypatch):
    """`mi ingest` must reconcile after all sources, with the full market.

    Reconciliation is what turns evidence into a price; if the CLI never called
    it, every deployment would keep its provisional newest-wins value and the
    anomaly would reach the router exactly as before P2.
    """
    import mininfer.ingest as ing
    from mininfer import cli
    from mininfer.fetch import Snapshot
    from mininfer.ingest.shared import ADAPTERS

    shared_model = "inclusionai/ling-3.0-flash-fin"
    payloads = {
        # 10x high: the unit-error scale.
        "novita": {"data": [{"id": shared_model,
                             "input_token_price_per_m": 7_500,
                             "output_token_price_per_m": 7_500}]},
        "openrouter": {"data": [{"id": shared_model, "name": shared_model,
                                  "hugging_face_id": shared_model,
                                  "pricing": {"prompt": "0.000000075",
                                              "completion": "0.000000075"}}]},
        "deepinfra": {"data": [{"id": shared_model,
                                  "metadata": {"pricing": {"input_tokens": 0.075,
                                                             "output_tokens": 0.075}}}]},
    }

    def fake_run_source(name, *, force=False, **kw):
        snap = Snapshot(source=name, url=f"https://example.test/{name}",
                        fetched_at=T1, sha256=name, nbytes=0, storage_uri="",
                        payload=payloads[name], observed_via="provider_api")
        return ADAPTERS[name](snap, **{k: v for k, v in kw.items()
                                       if k in ("with_endpoints", "max_endpoints")})

    monkeypatch.setattr(ing, "run_source", fake_run_source)
    db = tmp_path / "ingest.db"
    rc = cli.main(["--db", str(db), "--policy", "config/policy.yaml",
                   "ingest", "--no-endpoints", "novita", "openrouter", "deepinfra"])
    assert rc == 0

    s = Store(db)
    # All three adapters normalized onto one artifact, so the market has 3 sources.
    assert s.conn.execute(
        "SELECT COUNT(DISTINCT weights_id) c FROM pricing_evidence").fetchone()["c"] == 1
    r = _resolution(s, f"novita:{shared_model}")
    assert r["state"] == "quarantined"
    assert r["source_count"] == 3
    assert _deployment(s, f"novita:{shared_model}")["price_in"] is None
    assert _deployment(s, f"openrouter:{shared_model}")["price_in"] == pytest.approx(0.075)
    s.close()


# ------------------------------------------------------- free vs paid scale


def test_a_free_market_does_not_quarantine_a_paid_deployment(tmp_path):
    """Scale is undefined relative to zero: `paid / 0` is infinity, so if free
    peers joined the reference, one free tier would quarantine every paid
    deployment of the same artifact. Free is a category, not a scale."""
    s = Store(tmp_path / "t.db")
    _add(s, "novita:m", price_in=0.075, source="novita")
    _add(s, "openrouter:m", price_in=0.0, price_out=0.0, source="openrouter")
    _add(s, "deepinfra:m", price_in=0.0, price_out=0.0, source="deepinfra")
    s.commit()
    s.reconcile_prices()
    s.commit()

    r = _resolution(s, "novita:m")
    assert r["state"] == "canonical"
    assert r["usd_per_mtok"] == pytest.approx(0.075)
    assert _deployment(s, "novita:m")["price_in"] == pytest.approx(0.075)
    s.close()


def test_a_free_tier_among_paid_peers_is_not_an_anomaly(tmp_path):
    """The mirror case: a genuinely free arm beside paid ones is a free tier,
    which is the product's whole point, not a deviation."""
    s = Store(tmp_path / "t.db")
    _add(s, "openrouter:m", price_in=0.075, source="openrouter")
    _add(s, "deepinfra:m", price_in=0.075, source="deepinfra")
    _add(s, "novita:m", price_in=0.0, price_out=0.0, source="novita")
    s.commit()
    s.reconcile_prices()
    s.commit()

    r = _resolution(s, "novita:m")
    assert r["state"] == "canonical"
    assert r["usd_per_mtok"] == pytest.approx(0.0)
    assert _deployment(s, "novita:m")["zero_price"] == 1
    s.close()


def test_a_genuine_free_to_paid_change_is_kept_and_hibernated(tmp_path):
    """The end-to-end shape of the free-market fix.

    An arm that was free and is now genuinely paid, while its peers stay free,
    must reach the price it was actually observed at and hibernate — not be
    quarantined back to free by a zero-valued market.
    """
    s = Store(tmp_path / "t.db")
    _add(s, "openrouter:m", price_in=0.0, price_out=0.0, source="openrouter")
    _add(s, "deepinfra:m", price_in=0.0, price_out=0.0, source="deepinfra")
    _add(s, "novita:m", price_in=0.0, price_out=0.0, source="novita", observed_at=T0)
    s.commit()
    assert _deployment(s, "novita:m")["zero_price"] == 1

    _add(s, "novita:m", price_in=0.075, source="novita", observed_at=T1)
    s.commit()
    s.reconcile_prices()
    s.commit()

    r = _resolution(s, "novita:m")
    assert r["state"] != "quarantined"
    assert r["usd_per_mtok"] == pytest.approx(0.075)
    after = _deployment(s, "novita:m")
    assert after["status"] == "hibernated"
    assert after["zero_price"] == 0
    s.close()
