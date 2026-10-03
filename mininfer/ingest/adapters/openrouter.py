"""`openrouter` -> registry rows."""
from __future__ import annotations

import dataclasses as dc

from ...fetch import fetch
from ..shared import (
    caps_from_params,
    default_free_limits,
    make_deploy,
    make_deploy_id,
    evidence,
    f,
    popular,
    read_prices_for,
    validate_prices,
    weights_for,
    adapter,
)
from ..types import Bundle, Run
@adapter("openrouter")
def ingest_openrouter(snap: Snapshot, *, with_endpoints: bool = True, max_endpoints: int = 0) -> Run:
    run = Run()
    run.snapshots.append(snap)
    rows = snap.payload.get("data", [])
    free_ids: list[str] = []

    for m in rows:
        mid = m.get("id", "")
        name = m.get("name") or mid
        hf = m.get("hugging_face_id")
        arch = m.get("architecture") or {}
        mods = arch.get("input_modalities") or []
        # `pricing.*` is USD per token — the `openrouter` contract scales it. The
        # deployment does not carry a price; the observation below does.
        price = read_prices_for("openrouter", m, snap)
        pin, pout = price.price_in, price.price_out
        bench = m.get("benchmarks") or {}
        aa = bench.get("artificial_analysis") or {}

        benchmark: dict[str, float] = {}
        if (v := f(aa.get("intelligence_index"))) is not None:
            benchmark["aa_intelligence"] = v
        if (v := f(aa.get("coding_index"))) is not None:
            benchmark["coding"] = v
        if (v := f(aa.get("agentic_index"))) is not None:
            benchmark["agentic"] = v
        # Design Arena is a second, independent leaderboard (arena Elo per
        # category). Collapse its per-category rows to one mean Elo so it sits
        # beside the AA indices as an equally weightable signal; the router
        # normalises it by its own p5/p95, so the Elo scale needs no conversion.
        elos = [e for e in (f(x.get("elo")) for x in (bench.get("design_arena") or [])
                           if isinstance(x, dict)) if e is not None]
        if elos:
            benchmark["design_arena_elo"] = round(sum(elos) / len(elos), 1)

        sources = []
        if aa:
            sources.append("openrouter/artificial_analysis")
        if elos:
            sources.append("openrouter/design_arena")
        w = weights_for(name, hf, modalities=mods, benchmark=benchmark,
                         benchmark_source="+".join(sources) or None)
        pmid = mid
        free_variant = mid.endswith(":free")
        # A negative price is a sentinel for OpenRouter's meta-routers
        # (`openrouter/auto`, `pareto-code`, `fusion`): "depends on what the inner
        # router picks". Quarantine it with the raw value; the contract already
        # resolved it to unknown rather than to free.
        if price.sentinels:
            run.quarantine.append((
                "openrouter", make_deploy_id("openrouter", pmid), "pricing",
                {f"raw_{o.kind}": str(o.raw_value) for o in price.sentinels},
                "negative price sentinel: routing-dependent (meta-router), price = unknown",
            ))
        d = make_deploy(
            "openrouter", pmid, w.weights_id, source=snap.source, source_url=snap.url,
            ctx=m.get("context_length"), maxout=(m.get("top_provider") or {}).get("max_completion_tokens"),
            free_variant=free_variant,
            caps=caps_from_params(m.get("supported_parameters") or None,
                                   vision=bool({"image"} & set(mods)) if mods else None,
                                   reasoning=True if (m.get("reasoning") or {}).get("mandatory") else None),
            limits=default_free_limits("openrouter") if (free_variant or (pin == 0)) else {},
            limits_confirmed=False,
        )
        if (pin == 0 and pout == 0) or free_variant:
            free_ids.append(mid)

        ev = evidence("weights", w.weights_id,
                       {"display_name": name, "hf_repo": hf, "benchmark": benchmark or None},
                       snap.source, snap.url, snap.fetched_at, snap.observed_via)
        ev += evidence("deployment", d.deploy_id,
                        {"price_in": pin, "price_out": pout, "context_window": d.context_window,
                         "caps": d.caps, "free_variant": free_variant},
                        snap.source + "/models", snap.url, snap.fetched_at, "aggregator_api")
        if (reason := validate_prices("openrouter", mid, pin, pout)) and pin:
            run.quarantine.append(("openrouter", d.deploy_id, "pricing", {"in": pin, "out": pout}, reason))
        run.add(Bundle(w, d, ev, prices=price))

    # ---- per-endpoint deployments (real deployment-level economics) --------
    if with_endpoints:
        targets = free_ids if max_endpoints <= 0 else free_ids + popular(rows, max_endpoints)
        for mid in dict.fromkeys(targets):
            try:
                ep_snap = fetch("openrouter", f"https://openrouter.ai/api/v1/models/{mid}/endpoints")
            except Exception as exc:  # a dead endpoint must not kill the run
                run.quarantine.append(("openrouter", mid, "endpoints", None, f"fetch failed: {exc}"))
                continue
            run.snapshots.append(ep_snap)
            data = ep_snap.payload.get("data", {})
            for ep in data.get("endpoints", []):
                tag = ep.get("tag") or ep.get("provider_name") or "unknown"
                # Endpoints carry the same `pricing.*` shape as the model listing,
                # with their own snapshot provenance.
                ep_price = read_prices_for("openrouter", ep, ep_snap)
                pin, pout = ep_price.price_in, ep_price.price_out
                base = next((b for b in run.bundles if b.deployment.provider_model_id == mid
                             and b.deployment.provider == "openrouter"), None)
                if base is not None:
                    w = dc.replace(base.weights)
                else:
                    w = weights_for(mid, None)
                dep = make_deploy(
                    f"openrouter/{tag}", mid, w.weights_id, source=snap.source, source_url=ep_snap.url,
                    ctx=ep.get("context_length"), maxout=ep.get("max_completion_tokens"),
                    quant=ep.get("quantization"),
                    discount=f((ep.get("pricing") or {}).get("discount")),
                    caps=caps_from_params(ep.get("supported_parameters") or None),
                    uptime=f(ep.get("uptime_last_1d")),
                    ftl=f(ep.get("latency_last_30m")), tput=f(ep.get("throughput_last_30m")),
                    status="live" if ep.get("status") == 0 else "degraded",
                )
                ev = evidence("deployment", dep.deploy_id,
                               {"price_in": pin, "price_out": pout, "quantization": dep.quantization,
                                "uptime_1d": dep.uptime_1d, "provider_name": ep.get("provider_name"),
                                "discount": dep.discount},
                               snap.source + "/endpoints", ep_snap.url, ep_snap.fetched_at,
                               "provider_reported_probe")
                run.add(Bundle(w, dep, ev, prices=ep_price))
    return run
