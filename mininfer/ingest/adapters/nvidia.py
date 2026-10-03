"""`nvidia` -> registry rows."""
from __future__ import annotations

from ..shared import (
    caps_from_params,
    default_free_limits,
    make_deploy,
    evidence,
    weights_for,
    adapter,
)
from ..types import Bundle, Run
@adapter("nvidia")
def ingest_nvidia(snap: Snapshot, *, limit: int = 0) -> Run:
    run = Run(snapshots=[snap])
    rows = snap.payload.get("data", [])
    if limit:
        rows = rows[:limit]
    for m in rows:
        mid = m.get("id", "")
        repo = mid if "/" in mid else None
        w = weights_for(mid, repo)
        d = make_deploy("nvidia", mid, w.weights_id, source=snap.source, source_url=snap.url,
                    trial=True, caps=caps_from_params(None),
                    limits=default_free_limits("nvidia"), limits_confirmed=False)
        ev = evidence("deployment", d.deploy_id, {"trial_credits": True},
                       snap.source, snap.url, snap.fetched_at, "provider_api")
        run.add(Bundle(w, d, ev))
    return run


# --------------------------------------------------------------------------- #
# Tier 1 / generic adapters
# --------------------------------------------------------------------------- #


