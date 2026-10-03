"""`github` -> registry rows."""
from __future__ import annotations

from ..shared import (
    caps_from_params,
    default_free_limits,
    make_deploy,
    weights_for,
    adapter,
)
from ..types import Bundle, Run
@adapter("github")
def ingest_github(snap: Snapshot) -> Run:
    run = Run(snapshots=[snap])
    rows = snap.payload if isinstance(snap.payload, list) else snap.payload.get("models", [])
    for m in rows:
        mid = m.get("id") or m.get("name") or ""
        w = weights_for(mid, None)
        d = make_deploy("github", mid, w.weights_id, source=snap.source, source_url=snap.url,
                    ctx=(m.get("limits") or {}).get("max_input_tokens"),
                    maxout=(m.get("limits") or {}).get("max_output_tokens"),
                    caps=caps_from_params(m.get("supported_input_modalities") or None),
                    limits=default_free_limits("github"), limits_confirmed=False)
        run.add(Bundle(w, d, []))
    return run


