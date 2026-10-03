"""The one price unit the registry stores, and every unit a provider publishes in.

A wrong price scale is the quietest bug in the registry: the number stays
plausible and only the economic decision is wrong. Novita's `/1000` put every
direct arm 10x over its real price and went unnoticed until a hibernation reason
read "$0.75/Mtok" for a model four other providers priced at $0.075.

The fix is not a better comment. It is that the conversion factor is *data* — one
named table, one description per entry — so an adapter never divides by a
literal. `to_usd_per_mtok()` is the only path from a provider's number to the
canonical unit.

`Decimal`, not `float`: 1e-4 is exact in base-10 and not in base-2, and this
layer exists precisely to keep a 10x scale error from hiding in rounding.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Every `Deployment.price_*` is in this unit. The name is spelled out because the
# column's unit has been the source of at least three shipped bugs.
CANONICAL_UNIT = "usd_per_mtok"


class PriceUnitError(ValueError):
    """An unknown unit name, or a raw value that is not a number."""


@dataclass(frozen=True, slots=True)
class UnitSpec:
    name: str
    to_usd_per_mtok: Decimal
    description: str


# The registry's unit vocabulary. A unit is added here once and named by
# providers; nothing else knows how to scale a price.
UNITS: dict[str, UnitSpec] = {
    "usd_per_token": UnitSpec(
        "usd_per_token",
        Decimal(1_000_000),
        "USD per single token (OpenRouter, Vercel, SambaNova). x1e6 -> USD/Mtok.",
    ),
    "usd_per_mtok": UnitSpec(
        "usd_per_mtok",
        Decimal(1),
        "Already USD per 1,000,000 tokens (DeepInfra, Chutes, HF Router).",
    ),
    "usd_per_myriad_mtok": UnitSpec(
        "usd_per_myriad_mtok",
        Decimal("0.0001"),
        "1e-4 USD per Mtok (Novita `*_price_per_m`): raw 750 == $0.075/Mtok. "
        "Confirmed against OpenRouter for five models; /1000 matched none.",
    ),
    "usd_per_ktok": UnitSpec(
        "usd_per_ktok",
        Decimal(1_000),
        "USD per 1,000 tokens.",
    ),
}


def to_decimal(value) -> Decimal | None:
    """Coerce a provider's field to Decimal.

    `None` and `""` are *unknown*, not zero — the same distinction the schema
    makes for `price_in` and for three-valued capabilities. A malformed value is
    also unknown rather than an exception: one bad row must cost one field, not
    the ingest run.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int; a price is never a flag
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def to_usd_per_mtok(raw, unit: str) -> Decimal | None:
    """Convert a provider's published number to the canonical USD/Mtok.

    The only function that scales a price. `unit` is the *declared* unit from the
    provider contract, never inferred from the number's magnitude — inferring is
    what made the original bug invisible for weeks.
    """
    spec = UNITS.get(unit)
    if spec is None:
        raise PriceUnitError(
            f"unknown price unit {unit!r}; declare it in pricing.units.UNITS"
        )
    x = to_decimal(raw)
    if x is None:
        return None
    return x * spec.to_usd_per_mtok


def as_float(value: Decimal | None) -> float | None:
    """Boundary cast for `Deployment.price_*`, which is still a float column.

    Kept in one place so the Decimal -> float narrowing is a single, visible
    decision rather than something each adapter does differently.
    """
    return None if value is None else float(value)
