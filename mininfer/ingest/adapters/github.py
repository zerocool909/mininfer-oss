"""`github` -> registry rows."""
from __future__ import annotations

import json

from ..shared import (
    caps_from_params,
    default_free_limits,
    evidence,
    make_deploy,
    weights_for,
    adapter,
)
from ..types import Bundle, Run


def _modalities(m: dict) -> list[str]:
    """GitHub models publish one flat input list; there is no output field to read.

    Returning the list (rather than assuming) matters in both directions: a model
    with no declared modalities stays *unreported*, and the output half stays `[]`
    because this source says nothing about it — an absent list is not `["text"]`.
    """
    mods = m.get("supported_input_modalities") or []
    return [mods] if isinstance(mods, str) else list(mods)


@adapter("github")
def ingest_github(snap: Snapshot) -> Run:
    run = Run(snapshots=[snap])
    rows = snap.payload if isinstance(snap.payload, list) else snap.payload.get("models", [])
    for m in rows:
        mid = m.get("id") or m.get("name") or ""
        # `supported_input_modalities` is a *modality* list, and it used to be passed
        # to `caps_from_params`, which expects `supported_parameters`. The
        # intersection is empty, so it derived `tools: False, structured: False` — a
        # confident refutation computed from the wrong field, which is worse than
        # `None` because `None` would follow `on_unverified_capability` and `False`
        # rejects outright. GitHub publishes no `supported_parameters`, so the honest
        # answer is to say nothing.
        mods = _modalities(m)
        w = weights_for(mid, None, modalities=mods)
        limits = {"max_input_tokens": (m.get("limits") or {}).get("max_input_tokens"),
                  "max_output_tokens": (m.get("limits") or {}).get("max_output_tokens")}
        d = make_deploy("github", mid, w.weights_id, source=snap.source, source_url=snap.url,
                    ctx=(m.get("limits") or {}).get("max_input_tokens"),
                    maxout=(m.get("limits") or {}).get("max_output_tokens"),
                    caps=caps_from_params(None),
                    limits=default_free_limits("github"), limits_confirmed=False)
        # This adapter used to emit nothing at all, so no GitHub fact had provenance
        # and nothing about the model was recorded as a claim.
        ev = evidence("deployment", d.deploy_id,
                       {"provider_model_id": mid, "context_window": d.context_window,
                        "caps": d.caps, "modalities": mods if mods else None,
                        "limits": json.dumps(limits)},
                       snap.source, snap.url, snap.fetched_at, "provider_api")
        run.add(Bundle(w, d, ev))
    return run


