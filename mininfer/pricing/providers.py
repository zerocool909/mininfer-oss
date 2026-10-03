"""Per-provider pricing contracts: which field, and in which unit.

This is the file an engineer opens when they ask *"why are we multiplying by
0.0001?"*. Every provider that publishes a price declares it here — the field
path into its payload, the unit that field is in, and a note explaining why. An
adapter calls `read_prices(provider, row)` and never sees a conversion factor.

Two guards fall out of that shape:

* adding a provider without a contract is a `PriceUnitError`, not a silent
  zero-price or a 10x price;
* a contract can only name a unit that exists in `pricing.units.UNITS`, so a new
  unit is a deliberate addition, never a typo'd factor.

`validated` records whether the unit has been *independently* checked against a
second source. Novita's has (five models against OpenRouter). The rest are
preserved from the adapter's original assumption and are `False` — an honest
label, so the next person knows which facts are checked and which are inherited.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schema import (
    PRICING_KINDS,
    PRICING_TYPES,
    PriceObservation,
    PricingKind,
    PricingType,
    ProviderPrice,
    as_observation,
)
from .units import PriceUnitError, UNITS


@dataclass(frozen=True, slots=True)
class PriceField:
    """One billing dimension of a provider's payload."""

    kind: PricingKind
    path: str  # dot path into the provider row, e.g. "metadata.pricing.input_tokens"
    unit: str  # a key of UNITS
    pricing_type: PricingType = "paid"


@dataclass(frozen=True, slots=True)
class ProviderPricing:
    """A provider's complete declared price surface."""

    provider: str
    schema_version: int
    fields: tuple[PriceField, ...]
    note: str = ""
    validated: bool = False

    def field(self, kind: PricingKind) -> PriceField | None:
        return next((f for f in self.fields if f.kind == kind), None)

    def kinds(self) -> tuple[PricingKind, ...]:
        return tuple(f.kind for f in self.fields)


# --------------------------------------------------------------------------- #
# The contracts
#
# Keyed by adapter kind (the name `@adapter(...)` registers), not by report
# source: `huggingface` is fetched as a source but parsed by the `hf_router`
# adapter, and it is the adapter that knows the payload shape.
# --------------------------------------------------------------------------- #

PROVIDER_PRICING: dict[str, ProviderPricing] = {
    "novita": ProviderPricing(
        provider="novita",
        schema_version=1,
        validated=True,
        fields=(
            PriceField("input", "input_token_price_per_m", "usd_per_myriad_mtok"),
            PriceField("output", "output_token_price_per_m", "usd_per_myriad_mtok"),
        ),
        note=(
            "`*_price_per_m` is 1e-4 USD/Mtok: raw 750 == $0.075/Mtok, NOT $0.75. "
            "Cross-checked against OpenRouter for five models — /10000 matches each "
            "to four decimals, /1000 matches none."
        ),
    ),
    "openrouter": ProviderPricing(
        provider="openrouter",
        schema_version=1,
        validated=False,
        fields=(
            PriceField("input", "pricing.prompt", "usd_per_token"),
            PriceField("output", "pricing.completion", "usd_per_token"),
            PriceField("cached_input", "pricing.input_cache_read", "usd_per_token"),
        ),
        note=(
            "`pricing.*` is USD per token, published as a string. A negative value "
            "(`-1`, `-1000000`) is a sentinel meaning 'depends on what the inner "
            "router picks' for meta-routers — unknown, NOT free."
        ),
    ),
    "deepinfra": ProviderPricing(
        provider="deepinfra",
        schema_version=1,
        validated=False,
        fields=(
            PriceField("input", "metadata.pricing.input_tokens", "usd_per_mtok"),
            PriceField("output", "metadata.pricing.output_tokens", "usd_per_mtok"),
            PriceField("cached_input", "metadata.pricing.cache_read_tokens", "usd_per_mtok"),
        ),
        note="`metadata.pricing.*` is already USD per Mtok; no scaling.",
    ),
    "vercel": ProviderPricing(
        provider="vercel",
        schema_version=1,
        validated=False,
        fields=(
            PriceField("input", "pricing.input", "usd_per_token"),
            PriceField("output", "pricing.output", "usd_per_token"),
        ),
        note="`pricing.*` is USD per token. Free tier is encoded in the model id suffix.",
    ),
    "sambanova": ProviderPricing(
        provider="sambanova",
        schema_version=1,
        validated=False,
        fields=(
            PriceField("input", "pricing.prompt", "usd_per_token"),
            PriceField("output", "pricing.completion", "usd_per_token"),
        ),
        note="`pricing.*` is USD per token.",
    ),
    "chutes": ProviderPricing(
        provider="chutes",
        schema_version=1,
        validated=False,
        fields=(
            PriceField("input", "pricing.prompt", "usd_per_mtok"),
            PriceField("output", "pricing.completion", "usd_per_mtok"),
            PriceField("cached_input", "pricing.input_cache_read", "usd_per_mtok"),
        ),
        note=(
            "`pricing.*` treated as USD per Mtok — preserved from the original "
            "adapter's assumption. NOT yet independently cross-validated: if a "
            "peer provider ever disagrees by ~1000x, check this unit first."
        ),
    ),
    "hf_router": ProviderPricing(
        provider="hf_router",
        schema_version=1,
        validated=False,
        fields=(
            PriceField("input", "pricing.input", "usd_per_mtok"),
            PriceField("output", "pricing.output", "usd_per_mtok"),
        ),
        note="Per-provider `pricing.*` is USD per Mtok.",
    ),
}


def _dig(row: Any, path: str) -> Any:
    """Walk a dot path, tolerating any missing or non-dict link as absent."""
    cur = row
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def read_prices(
    provider: str,
    row: Any,
    *,
    pricing_type: PricingType | None = None,
    source: str | None = None,
    url: str | None = None,
    observed_at: str | None = None,
    confidence: float | None = None,
) -> ProviderPrice:
    """Read every declared price field from one provider payload row.

    The caller names the provider; the units come from the contract. A provider
    with no contract raises rather than guessing, because a guessed unit is the
    bug this module exists to prevent.

    `pricing_type` overrides the contract's default for positive prices — used by
    providers whose paid list price is nonetheless billed from a trial balance.
    The provenance arguments are optional and opaque here: this layer does not
    know what a snapshot is, only that an observation should be able to say where
    it came from. The ingest layer fills them (`shared.read_prices_for`).
    """
    spec = PROVIDER_PRICING.get(provider)
    if spec is None:
        raise PriceUnitError(
            f"no pricing contract for provider {provider!r}; "
            "add one to pricing.providers.PROVIDER_PRICING"
        )
    row = row if isinstance(row, dict) else {}
    observations: list[PriceObservation] = []
    for field in spec.fields:
        observations.append(
            as_observation(
                provider,
                field.kind,
                _dig(row, field.path),
                field.unit,
                field=field.path,
                pricing_type=pricing_type or field.pricing_type,
                source=source,
                url=url,
                observed_at=observed_at,
                confidence=confidence,
            )
        )
    return ProviderPrice(provider=provider, observations=tuple(observations))


def contract_is_well_formed(spec: ProviderPricing) -> list[str]:
    """Return the reasons `spec` is invalid; empty means valid.

    Used by the contract tests, and cheap enough that a new provider's contract
    can be asserted rather than trusted.
    """
    problems: list[str] = []
    if spec.schema_version < 1:
        problems.append("schema_version must be >= 1")
    if not spec.fields:
        problems.append("no fields declared")
    seen: set[str] = set()
    for f in spec.fields:
        if f.unit not in UNITS:
            problems.append(f"field {f.kind!r} names unknown unit {f.unit!r}")
        if f.kind not in PRICING_KINDS:
            problems.append(f"unknown pricing kind {f.kind!r}")
        if f.pricing_type not in PRICING_TYPES:
            problems.append(f"field {f.kind!r} names unknown pricing_type {f.pricing_type!r}")
        if not f.path:
            problems.append(f"field {f.kind!r} has an empty path")
        if f.kind in seen:
            problems.append(f"duplicate kind {f.kind!r}")
        seen.add(f.kind)
    if not spec.note:
        problems.append("no note explaining the unit")
    return problems


__all__ = [
    "PROVIDER_PRICING",
    "PriceField",
    "ProviderPricing",
    "contract_is_well_formed",
    "read_prices",
]
