"""The shapes an ingest produces.

Their own module so an adapter can import them without importing the package that
registers it — `mi/ingest/__init__.py` imports every adapter, so a type defined
there would be a cycle the moment an adapter wanted a `Bundle`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..pricing.schema import ProviderPrice
from ..schema import Deployment, Evidence, Weights


@dataclass(slots=True)
class Bundle:
    weights: Weights
    deployment: Deployment
    evidence: list[Evidence] = field(default_factory=list)
    #: The price *observations* this deployment was read from, if the source
    #: publishes prices. The adapter emits these; `Store.upsert_deployment`
    #: records them and resolves the stored economics from them. An adapter that
    #: has no contract (a keyless free tier, a local runtime) leaves this None.
    prices: ProviderPrice | None = None


@dataclass
class Run:
    bundles: list[Bundle] = field(default_factory=list)
    quarantine: list[tuple] = field(default_factory=list)  # (source, eid, field, value, reason)
    snapshots: list[Snapshot] = field(default_factory=list)

    def add(self, b: Bundle) -> None:
        self.bundles.append(b)


# --------------------------------------------------------------------------- #
# Source catalogue
# --------------------------------------------------------------------------- #
#
# The catalogue used to be declared twice: here, and in `catalogue.py` (which is
# the copy `mininfer.ingest` actually imports). The duplicate was dead and had
# already drifted — this one documented Novita's unit as 1e-3 USD/Mtok while
# `catalogue.py` said 1e-4. A stale second copy of a price unit is exactly the
# confusion `pricing.units` exists to remove, so only `catalogue.SOURCES` remains.
# Per-provider price units now live in `pricing.providers.PROVIDER_PRICING`.


@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: str
    tier: int
    url: str
    kind: str
    note: str = ""
    key_env: str | None = None
    confidence: str = "aggregator_api"
    extra_url: str | None = None

    @property
    def available(self) -> bool:
        # Tier 0 is keyless by design. Tier 2 is a *local* runtime: it also needs
        # no credential, so what can fail is reachability, not auth — and that
        # failure is already handled per-source by `mi ingest`. Treating it as
        # unavailable made these impossible to ingest at all (key_env is None).
        #
        # `key_env` wins over the tier, because it is the *fact* and the tier is the
        # roadmap. `deepseek` was listed tier 0 with a key_env, so `available` said
        # yes, `mi ingest` tried it every run, and the API answered 401 every run —
        # a permanent, self-inflicted failure line that nothing checked.
        if self.key_env is not None:
            return bool(os.environ.get(self.key_env))
        return self.tier in (0, 2)
