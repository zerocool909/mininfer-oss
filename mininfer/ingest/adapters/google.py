"""`google` -> registry rows."""
from __future__ import annotations

from ..shared import (
    default_free_limits,
    make_deploy,
    evidence,
    weights_for,
    adapter,
)
from ..types import Bundle, Run
@adapter("google")
def ingest_google(snap: Snapshot) -> Run:
    run = Run(snapshots=[snap])
    for m in snap.payload.get("models", []):
        mid = (m.get("name") or "").removeprefix("models/")
        mods = m.get("supportedGenerationMethods") or []
        w = weights_for(mid, None)
        d = make_deploy("google", mid, w.weights_id, source=snap.source, source_url=snap.url,
                    ctx=m.get("inputTokenLimit"), maxout=m.get("outputTokenLimit"),
                    caps={"tools": "generateContent" in mods},
                    limits=default_free_limits("google"), limits_confirmed=False)
        ev = evidence("deployment", d.deploy_id,
                       {"context_window": d.context_window, "methods": mods},
                       snap.source, snap.url, snap.fetched_at, "provider_api")
        run.add(Bundle(w, d, ev))
    return run


