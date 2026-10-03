"""`sambanova` -> registry rows."""
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
@adapter("sambanova")
def ingest_sambanova(snap: Snapshot) -> Run:
    run = Run(snapshots=[snap])
    for m in snap.payload.get("data", []):
        mid = m.get("id", "")
        # `pricing.prompt`/`pricing.completion` are USD per token per the
        # `sambanova` contract.
        price = read_prices_for("sambanova", m, snap)
        pin, pout = price.price_in, price.price_out
        w = weights_for(mid, None)
        # `trial` is a *deployment* fact (the key buys rate-limited access at $0),
        # not a price: it stays here while the price itself comes from the store.
        d = make_deploy("sambanova", mid, w.weights_id, source=snap.source, source_url=snap.url,
                    ctx=m.get("context_length"), maxout=m.get("max_completion_tokens"),
                    trial=True, caps=caps_from_params(None))
        ev = evidence("deployment", d.deploy_id, {"price_in": pin, "price_out": pout,
                                                   "context_window": d.context_window},
                       snap.source, snap.url, snap.fetched_at, "provider_api")
        run.add(Bundle(w, d, ev, prices=price))
    return run
