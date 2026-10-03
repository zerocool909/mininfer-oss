"""The pricing layer: units, observations, and per-provider contracts.

This package exists because a wrong price scale is the quietest bug in the
registry. The number stays plausible; only the economic decision is wrong.

The rule it enforces:

    Adapters produce evidence. The pricing layer decides what a number *means*.

An adapter never divides by a literal. It names the provider, and the conversion
comes from `pricing.providers.PROVIDER_PRICING` -> `pricing.units.UNITS`.
"""
from __future__ import annotations

from .providers import PROVIDER_PRICING, PriceField, ProviderPricing, read_prices
from .schema import (
    PRICING_KINDS,
    PriceObservation,
    PricingKind,
    PricingType,
    ProviderPrice,
)
from .units import (
    CANONICAL_UNIT,
    UNITS,
    PriceUnitError,
    UnitSpec,
    as_float,
    to_decimal,
    to_usd_per_mtok,
)

__all__ = [
    "CANONICAL_UNIT",
    "PROVIDER_PRICING",
    "PRICING_KINDS",
    "PriceField",
    "PriceObservation",
    "PriceUnitError",
    "PricingKind",
    "PricingType",
    "ProviderPrice",
    "ProviderPricing",
    "UNITS",
    "UnitSpec",
    "as_float",
    "read_prices",
    "to_decimal",
    "to_usd_per_mtok",
]
