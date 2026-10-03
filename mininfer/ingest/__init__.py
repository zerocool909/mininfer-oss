"""Pulling provider catalogues into the registry.

One adapter per provider, under `adapters/`. Each is a pure function of a
`Snapshot` — the fetched bytes, already persisted — which is what makes a
provider changing its response shape a parser fix plus a replay of the snapshot
rather than lost data.

The shared surface lives in `shared.py` (price guards, row builders) and the
source catalogue in `catalogue.py`. This module only wires them together.
"""
from __future__ import annotations

import os

from ..fetch import fetch
from ..schema import Deployment, Evidence, Weights  # noqa: F401 - re-exported
from . import adapters  # noqa: F401 - importing registers every adapter
from .adapters import *  # noqa: F401,F403 - one `ingest_*` per provider
from .catalogue import SOURCES
from .shared import (
    ADAPTERS,
    PRICE_SENTINELS,
    adapter,
    caps_from_modalities,
    caps_from_params,
    default_free_limits,
    make_deploy,
    evidence,
    f,
    opt,
    per_mtok_from_per_token,
    popular,
    price_sentinel,
    read_prices_for,
    validate_prices,
    weights_for,
)
from .types import Bundle, Run, SourceSpec

__all__ = [
    "ADAPTERS", "SOURCES", "Bundle", "Run", "SourceSpec", "adapter", "make_deploy",
    "evidence", "weights_for", "validate_prices", "caps_from_params",
    "caps_from_modalities", "default_free_limits", "per_mtok_from_per_token",
    "price_sentinel", "PRICE_SENTINELS", "read_prices_for", "run_source",
]


def run_source(name: str, *, force: bool = False, **kw) -> Run:
    spec = SOURCES[name]
    if not spec.url:
        raise ValueError(f"source {name!r} needs configuration (no default endpoint)")
    headers = {}
    if spec.key_env and (key := os.environ.get(spec.key_env)):
        headers["Authorization"] = f"Bearer {key}"
    snap = fetch(name, spec.url, headers=headers, force=force)
    snap.source = name
    snap.observed_via = spec.confidence  # type: ignore[attr-defined]
    fn = ADAPTERS[spec.kind]
    out = fn(snap, **kw) if kw else fn(snap)
    for b in out.bundles:
        b.deployment.source = name
    return out
