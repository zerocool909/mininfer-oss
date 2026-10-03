"""`cohere` -> registry rows."""
from __future__ import annotations

from ..shared import (
    caps_from_params,
    make_deploy,
    weights_for,
    adapter,
)
from ..types import Bundle, Run
@adapter("cohere")
def ingest_cohere(snap: Snapshot) -> Run:
    run = Run(snapshots=[snap])
    for m in snap.payload.get("models", []):
        mid = m.get("name", "")
        w = weights_for(mid, None)
        d = make_deploy("cohere", mid, w.weights_id, source=snap.source, source_url=snap.url,
                    ctx=m.get("context_length"), caps=caps_from_params(None))
        run.add(Bundle(w, d, []))
    return run


