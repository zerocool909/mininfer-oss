"""P4 — the pricing state machine.

`deployments.price_*` says what a call costs. The pricing *state* says what kind
of economic object the deployment is, which is the question a
`"was free, now $0.075/$0.22 per Mtok"` string was being made to answer badly.

Two of the states exist specifically because conflating them with `free` is how a
router spends money it believed was zero:

* `trial` — a balance that *depletes*; the marginal call is not free, it is
  prepaid and finite.
* `subscription` — prepaid, not free.

The state is derived by `_sync_pricing_states` and every change is logged, so
"when did this stop being free" is a query rather than a formatted string.
"""
from __future__ import annotations

import json

import pytest

from mininfer.schema import Deployment, Weights
from mininfer.store import (
    PRICING_DEPRECATED,
    PRICING_FREE,
    PRICING_FREE_WITH_QUOTA,
    PRICING_HIBERNATED,
    PRICING_PAID,
    PRICING_PAID_AFTER_QUOTA,
    PRICING_SUBSCRIPTION,
    PRICING_STATES,
    PRICING_TRIAL,
    PRICING_UNKNOWN,
    Store,
)


def _dep(s: Store, deploy_id: str, *, pin=None, pout=None, zero=False,
         free_variant=False, subscription=False, trial=False, status="live",
         weights="w") -> None:
    provider, model = deploy_id.split(":", 1)
    s.upsert_weights(Weights(weights, weights))
    s.upsert_deployment(Deployment(
        deploy_id, weights, provider, model, price_in=pin, price_out=pout,
        zero_price=zero, free_variant=free_variant, subscription=subscription,
        trial_credits=trial, status=status,
    ))


def _state(s: Store, deploy_id: str) -> str:
    return s.conn.execute(
        "SELECT pricing_state FROM deployments WHERE deploy_id=?", (deploy_id,)
    ).fetchone()["pricing_state"]


# --------------------------------------------------------------- the states


def test_a_zero_priced_listing_with_no_bucket_is_free(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m", pin=0.0, pout=0.0, zero=True)
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "p:m") == PRICING_FREE
    s.close()


def test_a_quota_limited_free_listing_is_free_with_quota(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m", pin=0.0, pout=0.0, zero=True)
    s.set_limit("p:m", "day", 1000)
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "p:m") == PRICING_FREE_WITH_QUOTA
    s.close()


def test_a_drained_bucket_becomes_paid_after_quota(tmp_path):
    """The state the router's own headroom rule describes, said out loud."""
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m", pin=0.0, pout=0.0, zero=True)
    s.set_limit("p:m", "day", 1000)
    s.record_usage("p:m", n=1000)
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "p:m") == PRICING_PAID_AFTER_QUOTA
    s.close()


def test_a_partly_used_bucket_is_still_free_with_quota(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m", pin=0.0, pout=0.0, zero=True)
    s.set_limit("p:m", "day", 1000)
    s.record_usage("p:m", n=400)
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "p:m") == PRICING_FREE_WITH_QUOTA
    s.close()


def test_a_trial_balance_is_trial_not_free(tmp_path):
    """The distinction that matters economically: a trial depletes, so the
    marginal call is prepaid and finite — calling it `free` is how a router spends
    money it believed was zero."""
    s = Store(tmp_path / "t.db")
    _dep(s, "nvidia:m", trial=True)
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "nvidia:m") == PRICING_TRIAL
    s.close()


def test_a_trial_balance_stays_trial_even_when_it_also_has_a_quota(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "nvidia:m", trial=True)
    s.set_limit("nvidia:m", "day", 1000)
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "nvidia:m") == PRICING_TRIAL
    s.close()


def test_a_subscription_is_not_free(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "featherless:m", subscription=True)
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "featherless:m") == PRICING_SUBSCRIPTION
    s.close()


def test_a_known_price_is_paid(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m", pin=0.075, pout=0.22)
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "p:m") == PRICING_PAID
    s.close()


def test_no_price_and_no_free_marker_is_unknown(tmp_path):
    """Unknown is not free — the rule the whole registry is built on."""
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m")
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "p:m") == PRICING_UNKNOWN
    s.close()


def test_a_free_variant_suffix_is_free(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m", pin=0.0, pout=0.0, free_variant=True, weights="w")
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "p:m") == PRICING_FREE
    s.close()


# --------------------------------------------------------------- precedence


def test_hibernated_wins_over_a_price(tmp_path):
    """A withdrawn arm is not described by its price — the router drops it, so
    calling it `paid` would be a lie the UI repeats."""
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m", pin=0.075, pout=0.22, status="hibernated")
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "p:m") == PRICING_HIBERNATED
    s.close()


def test_deprecated_wins_over_everything(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m", pin=0.0, pout=0.0, zero=True, status="deprecated")
    s.set_limit("p:m", "day", 1000)
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "p:m") == PRICING_DEPRECATED
    s.close()


def test_trial_wins_over_a_zero_price(tmp_path):
    """A provider that lists $0 *and* funds it from a trial balance is a trial:
    the balance is what runs out."""
    s = Store(tmp_path / "t.db")
    _dep(s, "nvidia:m", pin=0.0, pout=0.0, zero=True, trial=True)
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "nvidia:m") == PRICING_TRIAL
    s.close()


# --------------------------------------------------------------- transitions


def test_the_first_state_records_a_null_from_state(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m", pin=0.075, pout=0.22)
    s.commit()
    s.reconcile_prices()
    s.commit()

    rows = s.pricing_transitions("p:m")
    assert len(rows) == 1
    assert rows[0]["from_state"] is None
    assert rows[0]["to_state"] == PRICING_PAID
    s.close()


def test_a_state_change_is_recorded_as_a_transition(tmp_path):
    """The query behind \"free tier disappeared on <date>\"."""
    from mininfer.pricing.providers import read_prices

    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "w"))
    d = Deployment("p:m", "w", "p", "m")
    s.upsert_deployment(d, prices=read_prices(
        "novita", {"input_token_price_per_m": 0, "output_token_price_per_m": 0},
        source="p", observed_at="2026-09-01T00:00:00+00:00"))
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _state(s, "p:m") == PRICING_FREE

    # The same deployment is observed with a real price later.
    s.upsert_deployment(d, prices=read_prices(
        "novita", {"input_token_price_per_m": 750, "output_token_price_per_m": 2200},
        source="p", observed_at="2026-09-29T00:00:00+00:00"))
    s.commit()
    s.reconcile_prices()
    s.commit()

    rows = s.pricing_transitions("p:m")
    # Newest first. The arm started charging, so it left routing for review
    # rather than quietly becoming `paid` — the free -> paid spend event.
    assert [(r["from_state"], r["to_state"]) for r in rows] == [
        (PRICING_FREE, PRICING_HIBERNATED),
        (None, PRICING_FREE),
    ]
    # The order proves itself: the second row's `to_state` is the first row's
    # `from_state`. Both can share a `detected_at` (second precision), which is
    # what the microsecond `transition_id` tiebreak in the query is for.
    s.close()


def test_a_state_that_does_not_change_records_nothing(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m", pin=0.075, pout=0.22)
    s.commit()
    s.reconcile_prices()
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert len(s.pricing_transitions("p:m")) == 1
    s.close()


def test_the_transition_evidence_carries_the_coordinates(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "p:m", pin=0.075, pout=0.22)
    s.commit()
    s.reconcile_prices()
    s.commit()

    evidence = json.loads(s.pricing_transitions("p:m")[0]["evidence"])
    assert evidence["status"] == "live"
    assert evidence["price_in"] == pytest.approx(0.075)
    assert evidence["zero_price"] is False
    assert "headroom" in evidence
    s.close()


def test_every_deployment_gets_a_state_not_just_the_priced_ones(tmp_path):
    """A seeded or agent-ingested row has a price and a status but no observations.
    A state machine that skipped those would leave blank exactly the rows a human
    is most likely to inspect."""
    s = Store(tmp_path / "t.db")
    _dep(s, "seed:no-evidence", pin=1.0, pout=2.0, weights="w1")
    _dep(s, "seed:free", pin=0.0, pout=0.0, zero=True, weights="w2")
    s.commit()
    rep = s.reconcile_prices()
    s.commit()

    assert rep["deployments"] == 0                       # no price evidence
    assert _state(s, "seed:no-evidence") == PRICING_PAID
    assert _state(s, "seed:free") == PRICING_FREE
    s.close()


def test_every_derived_state_is_a_declared_state(tmp_path):
    s = Store(tmp_path / "t.db")
    _dep(s, "a:m", pin=0.075, pout=0.22, weights="w1")
    _dep(s, "b:m", pin=0.0, pout=0.0, zero=True, weights="w2")
    _dep(s, "c:m", trial=True, weights="w3")
    _dep(s, "d:m", weights="w4")
    _dep(s, "e:m", pin=0.075, pout=0.22, status="deprecated", weights="w5")
    s.commit()
    s.reconcile_prices()
    s.commit()

    states = {r["pricing_state"] for r in s.conn.execute(
        "SELECT pricing_state FROM deployments")}
    assert states <= set(PRICING_STATES) and None not in states
    assert len(states) == 5
    s.close()
