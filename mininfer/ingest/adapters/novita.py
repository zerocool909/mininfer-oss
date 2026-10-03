"""`novita` -> registry rows."""
from __future__ import annotations

from ..shared import (
    caps_from_params,
    make_deploy,
    evidence,
    read_prices_for,
    validate_prices,
    weights_for,
    adapter,
)
from ..types import Bundle, Run
@adapter("novita")
def ingest_novita(snap: Snapshot) -> Run:
    run = Run(snapshots=[snap])
    for m in snap.payload.get("data", []):
        mid = m.get("id", "")
        # The adapter emits an *observation*; it does not set a price. The unit
        # comes from the `novita` contract (raw 750 == $0.075/Mtok, 1e-4 USD/Mtok)
        # and `Store.upsert_deployment` resolves the stored economics from it. The
        # old `/1000` here put every direct-Novita arm 10x over its real price.
        price = read_prices_for("novita", m, snap)
        pin, pout = price.price_in, price.price_out
        ctx = m.get("context_size") or m.get("context_length")
        w = weights_for(mid, mid if "/" in mid else None)
        # No `pin`/`pout`: the deployment does not decide its own price, the store
        # does, from the observations on the bundle.
        d = make_deploy("novita", mid, w.weights_id, source=snap.source, source_url=snap.url,
                    ctx=ctx, caps=caps_from_params(m.get("supported_parameters") or None))
        # The plausibility band still runs on the normalized numbers, so a wild
        # value is quarantined with the observation that produced it.
        if (reason := validate_prices("novita", mid, pin, pout)) and pin:
            run.quarantine.append(("novita", d.deploy_id, "pricing",
                                   {"in": pin, "out": pout}, reason))
        ev = evidence("deployment", d.deploy_id, {"price_in": pin, "price_out": pout,
                                                   "context_window": ctx},
                       snap.source, snap.url, snap.fetched_at, "provider_api")
        run.add(Bundle(w, d, ev, prices=price))
    return run
