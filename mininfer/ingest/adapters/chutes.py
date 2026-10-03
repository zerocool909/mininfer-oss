"""`chutes` -> registry rows."""
from __future__ import annotations

from ..shared import (
    caps_from_modalities,
    make_deploy,
    evidence,
    opt,
    read_prices_for,
    weights_for,
    adapter,
)
from ..types import Bundle, Run
@adapter("chutes")
def ingest_chutes(snap: Snapshot) -> Run:
    run = Run(snapshots=[snap])
    for m in snap.payload.get("data", []):
        mid = m.get("id", "")
        # `pricing.*` is USD/Mtok per the `chutes` contract. That unit is flagged
        # `validated=False` — it is the adapter's original assumption, not yet
        # cross-checked against a peer.
        price = read_prices_for("chutes", m, snap)
        pin, pout, pcache = price.price_in, price.price_out, price.price_cached_in
        mods = m.get("input_modalities") or []
        repo = m.get("root") or mid
        w = weights_for(mid, repo if "/" in repo else None, modalities=mods)
        d = make_deploy("chutes", mid, w.weights_id, source=snap.source, source_url=snap.url,
                    ctx=m.get("context_length") or m.get("max_model_len"),
                    maxout=m.get("max_output_length"), quant=m.get("quantization"),
                    caps={"tools": opt(m.get("supports_tools")),
                          **caps_from_modalities(mods)})
        ev = evidence("deployment", d.deploy_id,
                       {"price_in": pin, "price_out": pout, "quantization": d.quantization,
                        "context_window": d.context_window},
                       snap.source, snap.url, snap.fetched_at, "provider_api")
        run.add(Bundle(w, d, ev, prices=price))
    return run
