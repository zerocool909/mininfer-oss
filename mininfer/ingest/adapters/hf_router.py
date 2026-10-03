"""`hf_router` -> registry rows."""
from __future__ import annotations

from ..shared import (
    caps_from_modalities,
    make_deploy,
    evidence,
    f,
    opt,
    read_prices_for,
    weights_for,
    adapter,
)
from ..types import Bundle, Run
@adapter("hf_router")
def ingest_hf_router(snap: Snapshot) -> Run:
    run = Run(snapshots=[snap])
    for m in snap.payload.get("data", []):
        repo = m.get("id", "")
        arch = m.get("architecture") or {}
        mods = arch.get("input_modalities") or []
        w = weights_for(repo, repo, modalities=mods)
        for p in m.get("providers", []):
            prov = p.get("provider") or "unknown"
            # Each provider block carries its own `pricing.*`, in USD/Mtok per
            # the `hf_router` contract. The provider name is reported, not used
            # to pick a unit: the unit is a property of the payload shape.
            price = read_prices_for("hf_router", p, snap)
            pin, pout = price.price_in, price.price_out
            is_free = bool(p.get("is_free"))
            d = make_deploy(
                f"hf/{prov}", repo, w.weights_id, source=snap.source, source_url=snap.url,
                ctx=p.get("context_length"),
                free_variant=is_free,
                caps={"tools": opt(p.get("supports_tools")),
                      "structured": opt(p.get("supports_structured_output")),
                      **caps_from_modalities(mods)},
                uptime=None, ftl=f(p.get("first_token_latency_ms")),
                tput=f(p.get("throughput")),
                status=p.get("status") or "live",
            )
            ev = evidence("deployment", d.deploy_id,
                           {"price_in": pin, "price_out": pout, "is_free": is_free,
                            "first_token_ms": d.first_token_ms, "throughput": d.throughput,
                            "caps": d.caps, "context_window": d.context_window},
                           snap.source, snap.url, snap.fetched_at, "provider_reported_probe")
            run.add(Bundle(w, d, ev, prices=price))
    return run
