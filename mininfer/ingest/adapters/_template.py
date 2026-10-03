"""Copy this file to add a provider. Nothing here is imported or registered.

An adapter is a **pure function of a `Snapshot`** — the bytes that were fetched,
already written to the raw lake. That shape is the whole design: a provider
changing its response shape becomes a parser fix plus a replay of the snapshot,
never lost data. So an adapter must not fetch, must not touch the store, and must
not read the environment.

The rule about price is the one to internalise:

    An adapter emits *observations*. The store decides *state*.

So an adapter never sets `Deployment.price_in`. It attaches a `ProviderPrice` to
its `Bundle`, and `Store.upsert_deployment` records the observations and resolves
the stored price from them. There is no conversion factor anywhere in this file.

Checklist for a new provider:

1. `ingest/catalogue.py` — add a `SourceSpec`. The `note` is the only place that
   records *why* the source is worth pulling; write it for the next person.
2. `pricing/providers.py` — add a `ProviderPricing` contract **if the source
   publishes prices**. Declare each price field's dot path and its unit; the
   adapter never scales it. A source that publishes no price (a keyless free
   tier, a local runtime) has no contract, and that is correct — its price is
   *unknown*, not zero.
3. Copy this file to `ingest/adapters/<kind>.py` and implement the body.
4. `ingest/adapters/__init__.py` — add the `from . import` and the
   `from .<kind> import ingest_<kind>` lines. The import *is* the registration.
5. `tests/test_ingest.py` — `EXPECTED_SOURCES` will fail until you add the name,
   which is the point: adding a source is a deliberate act.
6. `tests/pricing/test_provider_contracts.py` — add the adapter to
   `PRICING_ADAPTERS` and add a golden payload under `tests/pricing/fixtures/`.

The provider quirks that have shipped as real bugs, now structural rather than
remembered:

* **a price in the wrong unit** — Novita's `/1000` put every direct arm 10x over
  its real price. Fixed by declaring the unit in the pricing contract.
* **a negative price sentinel** meaning "unknown" (`-1`, `-1000000`) —
  `read_prices_for` resolves it to `None` and reports it via `price.sentinels`,
  so it can be quarantined rather than read as free.
* **a missing capability field read as `False`** — `caps_from_params` keeps
  `None` (unreported) distinct from `False` (a claim).

Rules the shared helpers already enforce, so you do not have to:

* `validate_prices` — a cheap plausibility band on the *normalized* price.
* `caps_from_params` / `caps_from_modalities` — three-valued capabilities. Leave a
  capability `None` when the source never reported it; `False` is a claim.
* `make_deploy` sets `limits_confirmed=False` unless you pass `True`, so a
  declared quota is treated as unconfirmed until usage proves it.
* Return an empty `Run()` for a payload you do not recognise. Do not raise: a
  provider changing shape mid-ingest must cost one source, not the run.
"""
from __future__ import annotations

# from ..shared import (
#     adapter, caps_from_modalities, caps_from_params, default_free_limits,
#     evidence, f, make_deploy, opt, read_prices_for, validate_prices, weights_for,
# )
# from ..types import Bundle, Run
#
#
# @adapter("example")
# def ingest_example(snap: Snapshot) -> Run:
#     """`example` -> registry rows.
#
#     `snap.payload` is whatever `fetch` parsed — usually a dict from JSON, but it
#     may be anything, so start by refusing a shape you did not expect.
#     """
#     run = Run(snapshots=[snap])
#     payload = snap.payload if isinstance(snap.payload, dict) else {}
#     for m in payload.get("data") or []:
#         pmid = m.get("id")
#         if not pmid:
#             continue                      # a row with no id cannot be keyed
#         # Read the price as an *observation*, stamped with this snapshot's
#         # provenance. No conversion happens here: the unit comes from the
#         # `example` contract in `pricing/providers.py`.
#         price = read_prices_for("example", m, snap)
#         pin, pout = price.price_in, price.price_out
#         bad = validate_prices(snap.source, pmid, pin, pout)
#         if bad:
#             run.quarantine.append((snap.source, pmid, "price", pin, bad))
#             continue
#         weights = weights_for(pmid, m.get("hf_repo"))
#         run.add(Bundle(
#             weights=weights,
#             deployment=make_deploy(
#                 snap.source, pmid, weights.weights_id,
#                 source=snap.source, source_url=snap.url,
#                 ctx=m.get("context_window"),
#                 # no `pin=`/`pout=`: the deployment does not price itself
#                 caps=caps_from_params(m),
#                 limits=default_free_limits(snap.source),
#             ),
#             evidence=evidence("deployment", f"{snap.source}:{pmid}",
#                               {"price_in": pin, "price_out": pout},
#                               source=snap.source, url=snap.url,
#                               fetched_at=snap.fetched_at,
#                               observed_via="provider_api"),
#             prices=price,     # <- the store resolves the stored price from this
#         ))
#     return run
