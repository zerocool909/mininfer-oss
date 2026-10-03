"""Typed price observations — the shape between a provider payload and the router.

`Deployment.price_in/price_out` is an economic *conclusion*. This module is the
*observation* that conclusion is drawn from: which provider, which billing
dimension, what they literally published, in what unit, and what it is once
normalized to the canonical unit.

Both are kept. `raw_value` + `raw_unit` are the provenance — without them a
reconciliation bug is undebuggable, because the original number is gone. The
normalized `usd_per_mtok` is the only field two providers may be compared on.

The extra billing dimensions (`reasoning`, `image_input`, `audio_input`, ...) are
declared now even though only `input`/`output`/`cached_input` are populated
today. The point is that a provider that starts billing reasoning tokens has a
place to go that is not a new column and a new adapter literal.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .units import CANONICAL_UNIT, PriceUnitError

PricingKind = Literal[
    "input",
    "output",
    "cached_input",
    "reasoning",
    "image_input",
    "audio_input",
    "video_input",
    "tool_call",
]

# The declared vocabulary. Ordered so `input`/`output` come first: a reader
# scanning a contract sees the two that every provider has before the rest.
PRICING_KINDS: tuple[PricingKind, ...] = (
    "input",
    "output",
    "cached_input",
    "reasoning",
    "image_input",
    "audio_input",
    "video_input",
    "tool_call",
)

# What the price *is*, not what the deployment is. A `:free` id suffix is a
# deployment-level fact (`Deployment.free_variant`); this is the billing state of
# the number itself.
PricingType = Literal["free", "paid", "trial", "subscription", "unknown"]

PRICING_TYPES: tuple[PricingType, ...] = (
    "free", "paid", "trial", "subscription", "unknown",
)


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """One provider's published number for one billing dimension.

    Frozen because an observation is a fact about a moment: correcting it means
    appending a newer observation, never editing this one.
    """

    provider: str
    kind: PricingKind

    # Exactly what the provider published, and the unit they published it in.
    raw_value: Decimal | None
    raw_unit: str
    field: str = ""

    # The same number in the canonical unit. `None` means unknown.
    usd_per_mtok: Decimal | None = None

    pricing_type: PricingType = "unknown"

    # A negative price is a sentinel ("depends on routing"), not an error and not
    # free. Carried explicitly so an adapter can quarantine it with the raw value
    # instead of re-deriving that from a `None`.
    is_sentinel: bool = False

    # Provenance. Filled by the ingest layer from the `Snapshot`, so the pricing
    # layer itself stays independent of how a payload was fetched. Without these a
    # stored observation cannot be judged later: "which source said $0.075, and
    # when" is the question the reconciler has to answer.
    source: str | None = None
    url: str | None = None
    observed_at: str | None = None
    confidence: float | None = None

    @property
    def known(self) -> bool:
        """A price we can act on. `None` is unknown, never zero."""
        return self.usd_per_mtok is not None

    @property
    def unit_label(self) -> str:
        return CANONICAL_UNIT


@dataclass(frozen=True, slots=True)
class ProviderPrice:
    """Every price component read from one provider row in one snapshot.

    Adapters hand this to `make_deploy`, which is the boundary where the economic
    conclusion (`Deployment.price_*`, still floats) is drawn.
    """

    provider: str
    observations: tuple[PriceObservation, ...] = ()

    def observation(self, kind: PricingKind) -> PriceObservation | None:
        return next((o for o in self.observations if o.kind == kind), None)

    def decimal(self, kind: PricingKind) -> Decimal | None:
        o = self.observation(kind)
        return o.usd_per_mtok if o is not None else None

    def price(self, kind: PricingKind) -> float | None:
        x = self.decimal(kind)
        return None if x is None else float(x)

    # ---- the fields `make_deploy` takes, named as it names them -------------
    @property
    def price_in(self) -> float | None:
        return self.price("input")

    @property
    def price_out(self) -> float | None:
        return self.price("output")

    @property
    def price_cached_in(self) -> float | None:
        return self.price("cached_input")

    @property
    def zero_price(self) -> bool:
        """Both sides known and exactly zero — the same test `make_deploy` makes."""
        return self.decimal("input") == 0 and self.decimal("output") == 0

    @property
    def sentinels(self) -> tuple[PriceObservation, ...]:
        return tuple(o for o in self.observations if o.is_sentinel)

    @property
    def priced(self) -> tuple[PriceObservation, ...]:
        return tuple(o for o in self.observations if o.known)

    @property
    def recordable(self) -> tuple[PriceObservation, ...]:
        """Observations worth persisting: a known price, or an explicit sentinel.

        A field the provider simply did not publish is not evidence — "DeepInfra
        did not list a cache price" is not a claim about the cache price — so it
        is dropped rather than stored as an `unknown` row. A sentinel is kept: it
        is a deliberate statement that the price depends on routing.
        """
        return tuple(o for o in self.observations if o.known or o.is_sentinel)


def as_observation(
    provider: str, kind: PricingKind, raw, unit: str, *, field: str = "",
    pricing_type: PricingType = "paid",
    source: str | None = None, url: str | None = None,
    observed_at: str | None = None, confidence: float | None = None,
) -> PriceObservation:
    """Build one observation, applying the sentinel rule in a single place.

    Kept here rather than in an adapter so that "a negative price means unknown"
    is one decision, not one per provider.
    """
    from .units import to_decimal, to_usd_per_mtok  # local: avoids an import cycle

    raw_d = to_decimal(raw)
    if raw_d is not None and raw_d < 0:
        return PriceObservation(
            provider=provider, kind=kind, raw_value=raw_d, raw_unit=unit,
            field=field, usd_per_mtok=None, pricing_type="unknown",
            is_sentinel=True, source=source, url=url, observed_at=observed_at,
            confidence=confidence,
        )
    usd = to_usd_per_mtok(raw_d, unit)
    if usd is None:
        kind_type: PricingType = "unknown"
    elif usd == 0:
        kind_type = "free"
    else:
        kind_type = pricing_type
    return PriceObservation(
        provider=provider, kind=kind, raw_value=raw_d, raw_unit=unit, field=field,
        usd_per_mtok=usd, pricing_type=kind_type, source=source, url=url,
        observed_at=observed_at, confidence=confidence,
    )


__all__ = [
    "PRICING_KINDS",
    "PRICING_TYPES",
    "PriceObservation",
    "PricingKind",
    "PricingType",
    "ProviderPrice",
    "PriceUnitError",
    "as_observation",
]
