"""What every adapter shares: the registry, the price guards, row builders.

`adapter(kind)` is a decorator rather than a dict literal so an adapter and its
registration cannot drift apart — a new provider is one decorated function, and
forgetting to register it is not possible.

The guards exist because provider APIs lie in specific, recurring ways: `-1` and
`-1000000` mean "unknown", per-token and per-Mtok prices differ by a factor of a
million, and a missing field means "never told us" rather than "false".
"""
from __future__ import annotations

import dataclasses as dc
import os
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from ..fetch import Snapshot, fetch
from ..normalize import (family_of, is_moe_name, normalize, params_b_from_name,
                         weights_id_for)
from ..pricing.providers import read_prices
from ..pricing.schema import ProviderPrice
from ..pricing.units import to_usd_per_mtok
from ..schema import (SOURCE_CONFIDENCE, Deployment, Evidence, Weights,
                      make_deploy_id)

from .types import Bundle, Run


ADAPTERS: dict[str, Callable[..., Iterator[Bundle]]] = {}


def adapter(kind: str):
    def deco(fn):
        ADAPTERS[kind] = fn
        return fn
    return deco


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# OpenRouter publishes `pricing.prompt = -1` for its meta-routers
# (`openrouter/auto`, `pareto-code`, `fusion`). That is a sentinel meaning
# "price depends on what the inner router picks", NOT an error and NOT free.
# Treating it as a number either corrupts the price column or (worse) makes a
# competitor's router look free.
PRICE_SENTINELS = {-1.0, -1000000.0}


def price_sentinel(v) -> bool:
    x = f(v)
    return x is not None and x < 0


def per_mtok_from_per_token(v) -> float | None:
    """USD per token -> USD per Mtok.

    A compatibility shim. It is re-exported from `mininfer.ingest` and pinned by
    `tests/test_router.py`, so it stays — but new adapters do not call it. They
    name a provider and let `pricing.providers.read_prices` supply the unit, so a
    conversion factor never appears in an adapter body again.
    """
    if price_sentinel(v):
        return None
    x = to_usd_per_mtok(v, "usd_per_token")
    return None if x is None else float(x)


def read_prices_for(provider: str, row, snap: Snapshot) -> ProviderPrice:
    """`read_prices` with this snapshot's provenance stamped on every observation.

    The one place a `Snapshot` is mapped onto a price observation. The pricing
    layer stays ignorant of fetching; the adapter stays ignorant of units. Both
    boundaries are one function wide, which is what lets each be tested alone.

    `observed_at` is the snapshot's `fetched_at`, not "now": an observation is a
    claim about the moment it was read, and a replayed snapshot must carry its
    original timestamp or a stale price looks fresh.
    """
    return read_prices(
        provider,
        row,
        source=snap.source,
        url=snap.url,
        observed_at=snap.fetched_at,
        confidence=SOURCE_CONFIDENCE.get(getattr(snap, "observed_via", "") or "", 0.5),
    )


PRICE_MIN, PRICE_MAX = 1e-4, 5000.0


def validate_prices(provider: str, pmid: str, pin: float | None, pout: float | None) -> str | None:
    """Cheap guard against unit mistakes. A wrong scale is the most likely silent bug."""
    for label, p in (("price_in", pin), ("price_out", pout)):
        if p is None:
            continue
        if p < 0:
            return f"{label}={p} negative"
        if p > 0 and not (PRICE_MIN <= p <= PRICE_MAX):
            return f"{label}={p} outside plausible USD/Mtok range"
    if pin and pout and pin > 0 and pout / pin > 200:
        return f"output/input ratio {pout / pin:.0f}x implausible"
    return None


def evidence(
    kind: str, eid: str, fields: dict, source: str, url: str | None,
    fetched_at: str, observed_via: str,
) -> list[Evidence]:
    conf = SOURCE_CONFIDENCE.get(observed_via, 0.5)
    return [
        Evidence(kind, eid, k, v, source, url, fetched_at, conf, observed_via)
        for k, v in fields.items()
        if v is not None
    ]


def weights_for(name: str, hf_repo: str | None, *, released: str | None = None,
                 modalities: Iterable[str] = (), source: str = "", url: str | None = None,
                 fetched_at: str = "", benchmark: dict | None = None,
                 benchmark_source: str | None = None) -> Weights:
    wid = weights_id_for(hf_repo, name)
    return Weights(
        weights_id=wid,
        display_name=name,
        hf_repo=hf_repo,
        family=family_of(name),
        params_b=params_b_from_name(name),
        arch="moe" if is_moe_name(name) else None,
        released_at=released,
        modalities=tuple(modalities),
        aliases=(name, normalize(name)),
        benchmark=benchmark or {},
        benchmark_source=benchmark_source,
    )


def make_deploy(
    provider: str, pmid: str, weights_id: str, *, source: str, source_url: str | None = None,
    ctx: int | None = None, maxout: int | None = None, quant: str | None = None,
    pin: float | None = None, pout: float | None = None, pcache: float | None = None,
    free_variant: bool = False, subscription: bool = False, trial: bool = False,
    discount: float | None = None, caps: dict | None = None, limits: dict | None = None,
    limits_confirmed: bool = False, uptime: float | None = None, ftl: float | None = None,
    tput: float | None = None, status: str = "live",
) -> Deployment:
    zero = pin is not None and pout is not None and pin == 0 and pout == 0
    return Deployment(
        deploy_id=make_deploy_id(provider, pmid),
        weights_id=weights_id,
        provider=provider,
        provider_model_id=pmid,
        context_window=ctx,
        max_output=maxout,
        quantization=quant,
        price_in=pin,
        price_out=pout,
        price_cached_in=pcache,
        zero_price=zero,
        free_variant=free_variant,
        subscription=subscription,
        trial_credits=trial,
        discount=discount,
        caps=caps or {},
        limits=limits or {},
        limits_confirmed=limits_confirmed,
        uptime_1d=uptime,
        first_token_ms=ftl,
        throughput=tput,
        status=status,
        source=source,
        source_url=source_url,
    )


def opt(v) -> bool | None:
    """Absent field -> unknown. Never coerce missing data to False."""
    return None if v is None else bool(v)


def caps_from_params(
    params: Iterable[str] | None, *, vision: bool | None = None,
    reasoning: bool | None = None,
) -> dict:
    """Capabilities are THREE-valued: True / False / None (unreported).

    A source that omits `supported_parameters` is not claiming the model lacks
    tools — it is declining to say. Collapsing that to False silently deleted 824
    of 1660 deployments on the first real run; collapsing it to True is worse.
    `None` is carried through and the policy decides, the same way an unknown
    price is not a zero price.
    """
    p = {str(x).lower() for x in params} if params else None
    if p is None:
        tools = structured = caching = None
    else:
        tools = bool({"tools", "tool_choice", "function_calling"} & p)
        structured = bool({"response_format", "structured_outputs", "structured_output",
                           "json_mode", "json_schema"} & p)
        caching = bool({"cache", "prompt_cache", "caching"} & p)
    if reasoning is None and p is not None:
        reasoning = bool({"reasoning", "include_reasoning", "reasoning_effort"} & p)
    return {
        "tools": tools,
        "structured": structured,
        "vision": vision,
        "reasoning": reasoning,
        "caching": caching,
        "audio": None,
    }


def caps_from_modalities(mods: Iterable[str] | None) -> dict:
    m = {str(x).lower() for x in mods} if mods else None
    if m is None:
        return {"vision": None, "audio": None}
    return {"vision": bool({"image", "vision", "video"} & m), "audio": "audio" in m}


def default_free_limits(provider: str) -> dict[str, int]:
    """Known free-tier shapes. Marked unconfirmed until read from the console."""
    table = {
        "groq": {"rpm": 30, "rpd": 1000, "tpm": 6000},
        "google": {"rpm": 15, "rpd": 1500},
        "cerebras": {"rpm": 30, "rpd": 1000},
        "mistral": {"rpm": 60, "rpd": 1000},
        "github": {"rpm": 10, "rpd": 150},
        "nvidia": {"rpm": 40},
        "openrouter": {"rpd": 50},
    }
    return table.get(provider, {})


# --------------------------------------------------------------------------- #
# Tier 0 adapters
# --------------------------------------------------------------------------- #


def popular(rows: list[dict], n: int) -> list[str]:
    """Cheap stand-in for 'models people actually use': has AA benchmarks and tools."""
    scored = []
    for m in rows:
        aa = ((m.get("benchmarks") or {}).get("artificial_analysis") or {})
        intel = f(aa.get("intelligence_index")) or 0.0
        if intel:
            scored.append((intel, m.get("id")))
    scored.sort(reverse=True)
    return [mid for _, mid in scored[:n]]


