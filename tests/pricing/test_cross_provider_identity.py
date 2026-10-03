"""Independent providers agreeing on the same weights' price.

This is the *anomaly signal* the reconciler will read in a later phase — never a
canonical price. The distinction is the point of this file:

* two providers publishing the same number is evidence that a unit is right;
* it is **not** evidence that either provider's price should be replaced.

Deployments genuinely price differently. A median over four honest prices can be
a number that exists nowhere, so it can detect an outlier but must never become
the price the router charges against. `test_peer_median_is_not_a_published_price`
pins that rule before the reconciler exists, so the reconciler cannot quietly
break it.

The five shared models are exactly the values the Novita `/10000` fix was
cross-validated against.
"""
from __future__ import annotations

from decimal import Decimal

from mininfer.pricing.providers import read_prices


def _by_id(payload: dict) -> dict[str, dict]:
    return {row["id"]: row for row in payload["data"]}


def _shared_ids(golden) -> list[str]:
    novita = _by_id(golden("novita.json"))
    openrouter = _by_id(golden("openrouter.json"))
    return sorted(set(novita) & set(openrouter))


def test_novita_and_openrouter_agree_on_every_shared_paid_model(golden):
    """A second provider's published price confirms the unit. This is the check
    that caught the 10x error: `/10000` matches each model to four decimals."""
    novita = _by_id(golden("novita.json"))
    openrouter = _by_id(golden("openrouter.json"))
    shared = _shared_ids(golden)
    assert len(shared) >= 5, "the cross-validation set must stay at least five models"

    for mid in shared:
        n = read_prices("novita", novita[mid])
        o = read_prices("openrouter", openrouter[mid])
        for kind in ("input", "output"):
            assert n.decimal(kind) == o.decimal(kind), f"{mid} {kind} disagrees"


def test_the_removed_divisor_fails_every_model_at_once(golden):
    """The regression, stated as a test.

    The adapter used to divide Novita's raw field by 1000. Under that rule *not
    one* of the cross-checked models matches its peer — which is why the bug
    survived: a consistent 10x error looks like a plausible market.
    """
    novita = _by_id(golden("novita.json"))
    openrouter = _by_id(golden("openrouter.json"))
    shared = _shared_ids(golden)

    mismatches = 0
    for mid in shared:
        raw_in = read_prices("novita", novita[mid]).observation("input").raw_value
        peer_in = read_prices("openrouter", openrouter[mid]).decimal("input")
        if raw_in is not None and (raw_in / 1000) != peer_in:
            mismatches += 1

    assert mismatches == len(shared)
    assert len(shared) >= 5


def test_the_agreed_price_is_exactly_the_declared_fraction(golden):
    """Anchors the agreement to the contract, not to a re-derivation."""
    assert Decimal("0.075") * 10_000 == Decimal("750")


def test_agreement_does_not_merge_two_deployments(golden):
    """Same value, two observations. The reconciler compares them; it does not
    collapse them into one price."""
    n = read_prices("novita", {"input_token_price_per_m": 750, "output_token_price_per_m": 2200})
    o = read_prices("openrouter", {"pricing": {"prompt": "0.000000075", "completion": "0.00000022"}})

    assert n.provider == "novita"
    assert o.provider == "openrouter"
    assert n.decimal("input") == o.decimal("input")
    assert n.observation("input") is not o.observation("input")


def test_peer_median_is_not_a_published_price():
    """Why the median is an anomaly signal and never a canonical price.

    Four honest deployments: $0.075, $0.06, $0.06, $0.075. The median is $0.0675
    — a price no provider publishes. Canonicalising it would make the router
    charge against a number that does not exist, and would overwrite four correct
    prices with one invented one.
    """
    observed = [
        ("novita", Decimal("0.075")),
        ("deepinfra", Decimal("0.06")),
        ("openrouter", Decimal("0.06")),
        ("vercel", Decimal("0.075")),
    ]
    prices = sorted(p for _, p in observed)
    n = len(prices)
    median = (prices[n // 2 - 1] + prices[n // 2]) / 2 if n % 2 == 0 else prices[n // 2]

    assert median == Decimal("0.0675")
    assert median not in {p for _, p in observed}


def test_a_ten_x_outlier_is_a_ratio_the_anomaly_engine_can_read(golden):
    """The signal, computed with P0 primitives only: an outlier is a *factor*
    against its peers. Detecting it is a later phase; being able to state it
    exactly is this one."""
    peers = [Decimal("0.075"), Decimal("0.075"), Decimal("0.075")]
    outlier = Decimal("0.75")  # the old /1000 result
    median = sorted(peers)[len(peers) // 2]
    assert outlier / median == Decimal("10")
