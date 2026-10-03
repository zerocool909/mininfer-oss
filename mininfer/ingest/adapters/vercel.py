"""`vercel` -> registry rows."""
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
@adapter("vercel")
def ingest_vercel(snap: Snapshot) -> Run:
    run = Run(snapshots=[snap])
    for m in snap.payload.get("data", []):
        mid = m.get("id", "")
        name = m.get("name") or mid
        # `pricing.input`/`pricing.output` are USD per token — the `vercel`
        # contract scales them; neither the adapter nor the deployment does.
        price = read_prices_for("vercel", m, snap)
        pin, pout = price.price_in, price.price_out
        mods = (m.get("modalities") or {}).get("input") or []
        free_variant = mid.endswith("-free")
        w = weights_for(name, None, modalities=mods)
        d = make_deploy(
            "vercel", mid, w.weights_id, source=snap.source, source_url=snap.url,
            ctx=m.get("context_window"), maxout=m.get("max_tokens"),
            free_variant=free_variant,
            caps=caps_from_params(m.get("supported_parameters") or None,
                                   vision=bool({"image"} & set(mods)) if mods else None,
                                   reasoning=True if "reasoning" in (m.get("tags") or []) else None),
            status="deprecated" if m.get("deprecated_at") else "live",
        )
        ev = evidence("deployment", d.deploy_id,
                       {"price_in": pin, "price_out": pout, "context_window": d.context_window,
                        "caps": d.caps, "free_variant": free_variant, "tags": m.get("tags")},
                       snap.source, snap.url, snap.fetched_at, "aggregator_api")
        run.add(Bundle(w, d, ev, prices=price))
    return run
