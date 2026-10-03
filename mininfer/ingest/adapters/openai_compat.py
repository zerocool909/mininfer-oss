"""`openai_compat` -> registry rows."""
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
#: Owner names that mean "this is our own API", not a provider. The `/v1/models`
#: handler stamps `owned_by`, and these are the names it has ever used: the project
#: was MinInfer before it was MinInfer, and `vllm`'s default port is the one the
#: container publishes. Both spellings have to match, or a renamed deployment
#: cheerfully ingests whichever instance it used to be.
SELF_OWNERS = frozenset({"mininfer", "mininfer"})


@adapter("openai_compat")
def ingest_openai_compat(snap: Snapshot) -> Run:
    """Generic /v1/models shape. Prices are usually absent here -> unknown, not zero.

    A provider with a declared free allowance is a different case: the key buys
    rate-limited access at $0, so the arm is marked `trial` and priced at zero
    while headroom lasts. Without that flag it carries no price *and* no free
    marker, so the router rejects it as "price unknown" — ingested but
    unreachable, which is the worst of both.
    """
    run = Run(snapshots=[snap])
    free_limits = default_free_limits(snap.source) or None
    for m in snap.payload.get("data", []):
        mid = m.get("id", "")
        # A local runtime's default port can be *this* app. `vllm` points at
        # `:8000`, which is the port the container publishes, so with MinInfer
        # running there `mi ingest` reads MinInfer's own `/v1/models` and turns
        # task names — `auto`, `general_chat`, `sql_generation` — into "models".
        # A model list that says it belongs to us is ours, not a provider's.
        if str(m.get("owned_by") or "").strip().lower() in SELF_OWNERS:
            continue
        w = weights_for(mid, mid if "/" in mid else None)
        d = make_deploy(snap.source, mid, w.weights_id, source=snap.source, source_url=snap.url,
                    ctx=m.get("context_length") or m.get("context_window") or m.get("max_model_len"),
                    pin=None, pout=None,
                    trial=bool(free_limits),
                    caps=caps_from_params(m.get("supported_parameters") or None),
                    limits=free_limits,
                    limits_confirmed=False)
        ev = evidence("deployment", d.deploy_id,
                       {"provider_model_id": mid, "context_window": d.context_window},
                       snap.source, snap.url, snap.fetched_at, "provider_api")
        run.add(Bundle(w, d, ev))
    return run


