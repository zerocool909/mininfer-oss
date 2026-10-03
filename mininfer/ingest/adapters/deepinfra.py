"""`deepinfra` -> registry rows."""
from __future__ import annotations

from ..shared import (
    caps_from_params,
    make_deploy,
    evidence,
    read_prices_for,
    weights_for,
    adapter,
)
from ..types import Bundle, Run
@adapter("deepinfra")
def ingest_deepinfra(snap: Snapshot) -> Run:
    run = Run(snapshots=[snap])
    for m in snap.payload.get("data", []):
        repo = m.get("id", "")
        meta = m.get("metadata") or {}
        # `metadata.pricing.*` is already USD/Mtok — declared by the `deepinfra`
        # contract, not assumed here. The adapter emits the observation; the store
        # resolves the price and the `zero_price` flag from it.
        price = read_prices_for("deepinfra", m, snap)
        pin, pout, pcache = price.price_in, price.price_out, price.price_cached_in
        tags = meta.get("tags") or []
        w = weights_for(repo, repo)
        d = make_deploy("deepinfra", repo, w.weights_id, source=snap.source, source_url=snap.url,
                    ctx=meta.get("context_length"), maxout=meta.get("max_tokens"),
                    caps=caps_from_params(
                        meta.get("supported_parameters") or None,
                        reasoning=True if "reasoning_effort" in tags else None))
        ev = evidence("deployment", d.deploy_id,
                       {"price_in": pin, "price_out": pout, "price_cached_in": pcache,
                        "context_window": d.context_window},
                       snap.source, snap.url, snap.fetched_at, "provider_api")
        run.add(Bundle(w, d, ev, prices=price))
    return run
