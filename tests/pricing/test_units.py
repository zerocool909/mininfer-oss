"""The unit table, and the one function that scales a price.

A wrong price scale keeps the number plausible, so these assert *exactness* —
`Decimal`, not `pytest.approx`. 750 must be 0.075 and not 0.07499999: a 10x-scale
check is a ratio, and rounding noise is exactly what hides one.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mininfer.pricing.units import (
    CANONICAL_UNIT,
    UNITS,
    PriceUnitError,
    as_float,
    to_decimal,
    to_usd_per_mtok,
)


def test_canonical_unit_is_usd_per_mtok():
    """Every `Deployment.price_*` is in this unit. The name is asserted so a
    rename cannot happen silently."""
    assert CANONICAL_UNIT == "usd_per_mtok"


@pytest.mark.parametrize(
    "unit,raw,expected",
    [
        ("usd_per_token", "0.000000075", "0.075"),
        ("usd_per_token", 0.0000003, "0.3"),
        ("usd_per_mtok", 0.834, "0.834"),
        ("usd_per_mtok", 2.501, "2.501"),
        ("usd_per_myriad_mtok", 750, "0.075"),
        ("usd_per_myriad_mtok", 2200, "0.22"),
        ("usd_per_myriad_mtok", 25010, "2.501"),
        ("usd_per_myriad_mtok", 13200, "1.32"),
        ("usd_per_ktok", 0.000075, "0.075"),
    ],
)
def test_units_scale_to_canonical_exactly(unit, raw, expected):
    assert to_usd_per_mtok(raw, unit) == Decimal(expected)


@pytest.mark.parametrize("raw", [None, "", "   ", "not-a-number", True, []])
def test_missing_or_malformed_price_is_unknown_not_zero(raw):
    """One bad field costs one field, not the ingest run, and never reads as $0."""
    assert to_usd_per_mtok(raw, "usd_per_token") is None


def test_unknown_unit_raises_rather_than_guessing():
    with pytest.raises(PriceUnitError):
        to_usd_per_mtok(1.0, "usd_per_furlong")


def test_every_declared_unit_is_documented_and_positive():
    for name, spec in UNITS.items():
        assert spec.name == name
        assert spec.description, f"{name} has no description"
        assert spec.to_usd_per_mtok > 0


def test_decimal_input_is_passed_through_unchanged():
    d = Decimal("0.075")
    assert to_decimal(d) is d


def test_as_float_is_the_only_narrowing_point():
    assert as_float(Decimal("0.075")) == 0.075
    assert as_float(None) is None


def test_negative_is_not_special_cased_here():
    """The sentinel rule lives in `pricing.schema`, not the unit table: this
    function only scales, so a negative value is returned as-is and the caller
    decides it means unknown."""
    assert to_usd_per_mtok(-1, "usd_per_token") == Decimal(-1_000_000)
