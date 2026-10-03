"""`cloudflare` -> registry rows."""
from __future__ import annotations

from ..shared import (
    caps_from_params,
    make_deploy,
    weights_for,
    adapter,
)
from ..types import Bundle, Run
@adapter("cloudflare")
def ingest_cloudflare(snap: Snapshot) -> Run:
    run = Run(snapshots=[snap])
    for m in (snap.payload.get("result") or []):
        mid = m.get("name", "")
        w = weights_for(mid, None)
        d = make_deploy("cloudflare", mid, w.weights_id, source=snap.source, source_url=snap.url,
                    caps=caps_from_params(m.get("properties") or None),
                    limits={"neurons_per_day": 10000}, limits_confirmed=False)
        run.add(Bundle(w, d, []))
    return run


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


