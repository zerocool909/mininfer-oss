"""`mi` — command line for the registry and the router.

    mi ingest                         # every keyless source
    mi ingest openrouter vercel       # named sources
    mi sources                        # provider roadmap + availability
    mi resolve                        # entity resolution pass
    mi stats                          # registry + funnel counters
    mi route sql_generation           # top 3 for a task
    mi route sql_generation --mode manual --candidates a,b,c
    mi explain sql_generation         # why the shortlist lost
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from .env import load_env

from . import bandit as bandit_mod
from . import bench as bench_mod
from . import db as db_mod
from . import ingest as ing
from . import metrics as metrics_mod
from . import search as search_mod
from .execute import Runner
from .fetch import utcnow
from .resolve import propose
from .router import (DEFAULT_TASK, Policy, benchmark_norms, build_candidates,
                     route)
from .store import Store

def _default_db() -> pathlib.Path:
    env_db = os.environ.get("MI_DB")
    if env_db:
        # A `postgresql://` DSN must stay a string — `Path` collapses the slashes
        # and psycopg2 rejects `postgresql:/host/db`.
        return db_mod.target_from_env(env_db)
    # One path now: the previous generations' database filenames went with the
    # names they belonged to.
    return pathlib.Path("mininfer.db")


def _default_policy() -> pathlib.Path:
    env_policy = os.environ.get("MI_POLICY")
    if env_policy:
        return pathlib.Path(env_policy)
    return pathlib.Path("config/policy.yaml")


DB = _default_db()
POLICY = _default_policy()


# --------------------------------------------------------------------------- #


def _open(args) -> Store:
    return Store(args.db)


def _load_env() -> None:
    """Load `.env` from the CWD, without clobbering real env vars.

    Delegates to `mi.env.load_env`, which the proxy module also calls at import
    time (uvicorn's reloader never runs `main`).
    """
    load_env()


def _money(v: float | None, *, per_mtok: bool = True) -> str:
    if v is None:
        return "?"
    if v == 0:
        return "FREE"
    if v == float("inf"):
        return "unknown"
    return f"${v:.4f}" if v < 1 else f"${v:.2f}"


def cmd_ingest(args) -> int:
    store = _open(args)
    _ingest_all(store, args)
    c = store.counts()
    print(f"\nregistry: {c['weights']} weights  {c['deployments']} deployments  "
          f"{c['evidence']} evidence rows  {c['snapshots']} raw snapshots")
    store.close()
    return 0


def _ingest_all(store, args) -> None:
    """Run every available source into `store`. Shared by `ingest` and `verify`."""
    names = args.sources or [n for n, s in ing.SOURCES.items() if s.tier == 0 and s.url]
    for name in names:
        spec = ing.SOURCES.get(name)
        if spec is None:
            print(f"  ! unknown source {name}", file=sys.stderr)
            continue
        if not spec.available:
            print(f"  - {name:12s} skipped (needs {spec.key_env})")
            continue
        try:
            kw: dict = {}
            if name == "openrouter":
                kw = {"with_endpoints": not args.no_endpoints,
                      "max_endpoints": args.endpoints_top}
            if name == "nvidia":
                kw = {"limit": args.limit} if args.limit else {}
            run = ing.run_source(name, force=args.force, **kw)
        except Exception as exc:
            print(f"  ! {name:12s} FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        for b in run.bundles:
            store.upsert_weights(b.weights, b.evidence)
            store.upsert_deployment(b.deployment, b.evidence, run.quarantine,
                                    prices=b.prices)
        for s in run.snapshots:
            store.record_snapshot(s)
        store.commit()
        print(f"  + {name:12s} {len(run.bundles):5d} deployments  "
              f"{len(run.quarantine):3d} quarantined  ({spec.tier=} {spec.note})")

    # After every source, not per source: the peer comparison needs all of them.
    # Reconciling mid-loop would let whichever provider happened to be ingested
    # first vouch for a value the later ones contradict.
    rep = store.reconcile_prices()
    store.commit()
    if rep["kinds"]:
        states = "  ".join(f"{k}={v}" for k, v in sorted(rep["states"].items()))
        print(f"\n  reconciled {rep['deployments']} deployments "
              f"({rep['kinds']} prices, {rep['changed']} changed): {states}")
        an = rep["anomalies"]
        if an["opened"] or an["refreshed"] or an["closed"]:
            print(f"  anomalies: {an['opened']} opened, {an['refreshed']} refreshed, "
                  f"{an['closed']} closed")
            if an["opened"] or an["refreshed"]:
                print("  review with `mi anomalies`")
        ps = rep["pricing_states"]
        if ps["changed"]:
            print(f"  pricing states: {ps['changed']} changed — see `mi transitions`")


def cmd_reconcile(args) -> int:
    """Re-derive every deployment's price from the evidence already stored.

    The rule lives in `Store.reconcile_prices`. This exists because the decision
    is derived: changing a threshold should be a re-run, not a data migration,
    and making it a command is what keeps that true.
    """
    store = _open(args)
    rep = store.reconcile_prices()
    store.commit()
    print(f"reconciled {rep['deployments']} deployments  "
          f"({rep['kinds']} prices, {rep['changed']} changed)")
    for state, n in sorted(rep["states"].items()):
        print(f"  {state:12s} {n}")
    an = rep["anomalies"]
    if any(an.values()):
        print(f"  anomalies: {an['opened']} opened, {an['refreshed']} refreshed, "
              f"{an['closed']} closed")
    ps = rep["pricing_states"]
    if ps["states"]:
        top = "  ".join(f"{k}={v}" for k, v in sorted(ps["states"].items()))
        print(f"  pricing states: {top}")
        if ps["changed"]:
            print(f"    {ps['changed']} changed — see `mi transitions`")
    if not rep["kinds"]:
        print("  (no price observations yet — run `mi ingest` first)")
    store.close()
    return 0


def cmd_transitions(args) -> int:
    """The pricing-state history: what a deployment was, and when it changed.

    This is what answers "when did this stop being free" as a fact rather than a
    string that was formatted once for a human and then overwritten.
    """
    store = _open(args)
    rows = store.pricing_transitions(args.deploy_id, limit=args.limit)
    if not rows:
        where = f" for {args.deploy_id}" if args.deploy_id else ""
        print(f"no pricing-state changes recorded{where} (run `mi reconcile`)")
        store.close()
        return 0
    for t in rows:
        print(f"  {t['detected_at']}  {t['deploy_id']}")
        print(f"      {t['from_state'] or '(first seen)'}  ->  {t['to_state']}"
              f"   ({_money(t['price_in'])}/{_money(t['price_out'])} per Mtok)")
    store.close()
    return 0


def cmd_anomalies(args) -> int:
    """The pricing anomaly log: what the reconciler refused or flagged.

    A row is one ongoing *incident*, not one observation: it opens when a price is
    refused and closes when the price returns to canonical. `--status ""` shows the
    whole history.
    """
    store = _open(args)
    rows = store.anomalies(status=args.status or None, limit=args.limit)
    if not rows:
        print("no anomalies" if not args.status else f"no {args.status} anomalies")
        store.close()
        return 0
    for a in rows:
        print(f"  [{a['severity']:<8}] {a['kind']:<12} {a['deploy_id']}  ({a['dimension']})")
        print(f"      {a['detail']}")
        print(f"      observed {_money(a['observed'])} vs expected {_money(a['expected'])}"
              f"  ·  {a['source_count']} source(s)"
              f"  ·  {a['status']}"
              f"  ·  last seen {a['last_seen_at']}")
        print(f"      {a['anomaly_id']}")
    print(f"\n  {len(rows)} shown. Decide with: mi anomaly <id> --acknowledge | --resolve")
    store.close()
    return 0


def cmd_anomaly(args) -> int:
    """Acknowledge (seen, still wrong) or resolve (closed) a pricing anomaly.

    Acknowledging stops it being re-alerted on every reconcile; it does not change
    a price. Resolving one that is still genuinely wrong does not silence it — the
    next reconcile opens a new row.
    """
    if args.acknowledge == args.resolve:
        print("pass exactly one of --acknowledge / --resolve", file=sys.stderr)
        return 2
    status = "acknowledged" if args.acknowledge else "resolved"
    store = _open(args)
    ok = store.decide_anomaly(args.anomaly_id, status=status, note=args.note)
    store.close()
    if not ok:
        print(f"no such anomaly {args.anomaly_id}", file=sys.stderr)
        return 1
    print(f"{args.anomaly_id} -> {status}")
    return 0


def cmd_seed_google(args) -> int:
    """Add the current direct Gemini deployments to the registry.

    Idempotent — existing rows are left alone, so this only ever adds ids a key
    can actually call. A command rather than an init-time seed, because seeding
    on every open would rewrite registries that do not use Google at all.
    """
    store = _open(args)
    before = {d["deploy_id"] for d in store.deployments()}
    store.seed_google_and_local_deployments()
    added = sorted({d["deploy_id"] for d in store.deployments()} - before)
    print(f"google: {len(added)} added"
          + (f" -> {', '.join(added)}" if added else " (all already present)"))
    store.close()
    return 0


def cmd_reviews(args) -> int:
    """Models hibernated for review: they were free, and now they charge."""
    store = _open(args)
    rows = store.reviews()
    if not rows:
        print("no models awaiting review")
    for r in rows:
        print(f"  {r['deploy_id']}")
        print(f"      {r['status_reason']}   (since {r.get('status_changed_at')})")
    if rows:
        print("\n  decide with: mi review <deploy_id> --approve | --reject")
    store.close()
    return 0


def cmd_review(args) -> int:
    """Approve a hibernated model back to `live`, or reject it to `deprecated`."""
    if args.approve == args.reject:
        print("pass exactly one of --approve / --reject", file=sys.stderr)
        return 2
    store = _open(args)
    ok = store.decide_review(args.deploy_id, approve=args.approve, note=args.note)
    store.close()
    if not ok:
        print(f"no hibernated review for {args.deploy_id}", file=sys.stderr)
        return 1
    print(f"{args.deploy_id} -> "
          + ("live (approved)" if args.approve else "deprecated (rejected)"))
    return 0


def _price_snapshot(store) -> dict[str, tuple]:
    """What the registry claims about price and free status, per deployment."""
    return {
        d["deploy_id"]: (
            d.get("price_in"), d.get("price_out"),
            int(d.get("zero_price") or 0), int(d.get("free_variant") or 0),
        )
        for d in store.deployments()
    }


def _price_label(snap: tuple) -> str:
    if snap[2] or snap[3]:
        return "free"
    return f"${snap[0] or 0:.4f}/${snap[1] or 0:.4f} per Mtok"


def cmd_verify(args) -> int:
    """Re-read every source and report what changed price or free status.

    The daily drift check. A registry that still says "free" after a provider
    started charging is worse than an empty one: the router keeps choosing that
    arm and every request quietly costs money. Re-ingesting and diffing is the
    honest way to know — a stored price is a claim about the past.
    """
    store = _open(args)
    before = _price_snapshot(store)
    _ingest_all(store, args)
    after = _price_snapshot(store)

    appeared: list[str] = []
    changed: list[tuple[str, tuple, tuple]] = []
    flips: list[tuple[str, tuple, tuple]] = []
    for did, now in after.items():
        old = before.get(did)
        if old is None:
            appeared.append(did)
            continue
        if old != now:
            changed.append((did, old, now))
            if bool(old[2] or old[3]) != bool(now[2] or now[3]):
                flips.append((did, old, now))

    print(f"\nverify: {len(after)} deployments checked · {len(appeared)} new · "
          f"{len(changed)} changed · {len(flips)} free-status flips")
    for did, old, now in changed:
        kind = ("FREE -> PAID" if (old[2] or old[3]) and not (now[2] or now[3])
                else "PAID -> FREE" if (now[2] or now[3]) and not (old[2] or old[3])
                else "price")
        print(f"  {kind:<12} {did}")
        print(f"      was {_price_label(old)}  ->  now {_price_label(now)}")

    if args.report:
        pathlib.Path(args.report).write_text(json.dumps({
            "checked": len(after),
            "new": appeared,
            "changed": [{"deploy_id": d, "before": list(o), "after": list(n)}
                        for d, o, n in changed],
            "free_flips": [d for d, _o, _n in flips],
            "awaiting_review": [r["deploy_id"] for r in store.reviews()],
        }, indent=2))
        print(f"  report -> {args.report}")

    awaiting = store.reviews()
    if awaiting:
        print(f"\n{len(awaiting)} model(s) hibernated for review (was free, now charges):")
        for r in awaiting[:10]:
            print(f"    {r['deploy_id']}  — {r['status_reason']}")
        print("  decide with: mi review <deploy_id> --approve | --reject")
    store.close()
    return 0


def cmd_sources(args) -> int:
    tiers = {0: "keyless — works now", 1: "free API key — real free quota", 2: "local runtime"}
    for tier, label in tiers.items():
        print(f"\nTIER {tier}  {label}")
        for name, s in ing.SOURCES.items():
            if s.tier != tier:
                continue
            mark = "ok " if s.available else "key"
            env = f"  [{s.key_env}]" if s.key_env else ""
            print(f"  {mark} {name:12s} {s.note}{env}")
    store = _open(args)
    seen = {r["provider"] for r in store.deployments()}
    print(f"\nproviders present in registry: {len(seen)}")
    store.close()
    return 0


def cmd_resolve(args) -> int:
    store = _open(args)
    merged, review = propose(store, auto=not args.no_auto)
    store.commit()
    print(f"auto-merged: {len(merged)}")
    for p in merged[:15]:
        print(f"  {p.alias_id}\n    -> {p.canonical_id}   ({p.reason})")
    print(f"\nneeds review: {len(review)}")

    if args.adjudicate and review:
        from . import agent_resolve

        rep = agent_resolve.adjudicate(store, review, agent_model=args.agent_model,
                                       min_confidence=args.min_confidence,
                                       dry_run=args.dry_run,
                                       max_proposals=args.limit)
        if rep.get("error"):
            print(f"adjudicator error: {rep['error']}")
            store.close()
            return 1
        print(f"adjudicator: reviewed {rep['considered']}/{rep['total_proposals']} "
              f"-> {rep['merged']} merged  {rep['kept']} kept  "
              f"{rep['refused']} refused" + ("   (dry-run)" if rep["dry_run"] else ""))
        for m in rep["merged_list"][:15]:
            print(f"  + {m['left']} -> {m['right']}  (conf {m['confidence']:.2f})")
        for k in rep["kept_list"][:15]:
            print(f"  = {k['left']}  !=  {k['right']}  ({k['reason']})")
        for r in rep["refused_list"][:15]:
            print(f"  ! {r['alias_id']} ~ {r['canonical_id']}  REFUSED (param mismatch)")
    else:
        for p in review[:15]:
            print(f"  {p.similarity:.3f}  {p.alias_id}  ~=  {p.canonical_id}")
    store.close()
    return 0


def cmd_stats(args) -> int:
    store = _open(args)
    c = store.counts()
    print("registry counters")
    for k, v in c.items():
        print(f"  {k:14s} {v}")

    rows = store.providers_summary()
    print("\ndeployments by provider")
    print(f"  {'provider':28s} {'n':>6s} {'free-ish':>9s} {'min $/Mtok in':>14s}")
    for r in rows:
        print(f"  {r['provider']:28s} {r['n']:6d} {r['free_n']:9d} "
              f"{_money(r['min_in']):>14s}")
    store.close()
    return 0


def cmd_route(args) -> int:
    store = _open(args)
    policy, tasks = Policy.load(args.policy)
    if args.task not in tasks:
        print(f"unknown task {args.task!r}; known: {', '.join(tasks)}", file=sys.stderr)
        return 2
    task = tasks[args.task]
    if args.objective:
        policy.objective = args.objective
    if getattr(args, "bandit", False):
        policy.objective = "bandit"
    if getattr(args, "explore_eps", None) is not None:
        policy.exploration_eps = args.explore_eps
    if args.max_cost_in is not None:
        policy.max_cost_in = args.max_cost_in
    if args.min_success is not None:
        policy.min_success_lb = args.min_success
    if args.allow_unknown_price:
        policy.allow_unknown_price = True
    if getattr(args, "include_uncredentialed", False):
        policy.require_callable = False
    policy.top_k = args.top_k

    manual = [s.strip() for s in (args.candidates or "").split(",") if s.strip()]
    pool = build_candidates(store, task, policy)
    dec = route(store, task, policy, mode=args.mode, manual_ids=manual, pool=pool)

    print(f"TASK     {task.name}  — {task.description}")
    print(f"POLICY   {policy.name}  objective={policy.objective}  top_k={policy.top_k}"
          f"  strategy={dec.strategy}")
    print(f"TOKENS   ~{task.tokens_in} in / ~{task.tokens_out} out per call")
    f = dec.funnel
    print(f"FUNNEL   {f['total']} deployments -> {f['after_hard_filter']} eligible "
          f"({f['free_eligible']} at zero marginal cost, "
          f"{f['distinct_providers']} distinct upstreams)")
    print(f"         rejected: {f['rejected_unsupported']} unsupported, "
          f"{f['rejected_unverified']} unverified, "
          f"{f['rejected_quality']} below quality floor, "
          f"{f['rejected_no_evidence']} with no benchmark coverage, "
          f"{f.get('rejected_no_key', 0)} with no API key")
    print(f"DIVERSITY fallback separation achieved: {dec.diversity}")
    if args.mode == "manual":
        print(f"MANUAL   restricted to {manual}")

    if not dec.chosen:
        print("\nno eligible candidate. closest rejections:")
        for c in dec.rejected_sample:
            print(f"  {c.deploy_id:44s} {c.rejected}")
        store.close()
        return 1

    medals = ["1st", "2nd", "3rd", "4th", "5th"]
    print("\nSELECTED")
    for i, c in enumerate(dec.chosen):
        tag = medals[i] if i < len(medals) else f"{i+1:02d}"
        if c.free_kind is None:
            price = f"{_money(c.price_in)} in / {_money(c.price_out)} out  per Mtok"
        elif c.price_in:
            price = (f"$0 marginal ({c.free_kind}); list {_money(c.price_in)} in / "
                     f"{_money(c.price_out)} out per Mtok")
        else:
            price = f"$0 marginal ({c.free_kind})"
        print(f"\n  {tag}  {c.deploy_id}")
        print(f"       {c.display_name}")
        print(f"       price      {price}")
        print(f"       p(success) {c.p_lb:.2f} lower bound   "
              f"(prior {c.p_prior:.2f} from {c.prior_key}, "
              f"{c.n_obs} observations)")
        print(f"       availability {c.availability:.2f}   "
              f"headroom {('unknown' if c.headroom is None else f'{c.headroom:.0%}')}   "
              f"observed 429 rate {c.rate_429:.0%}   quota risk {c.quota_risk:.0%}")
        print(f"       cost/success  {_money(c.cost_per_success, per_mtok=False)}   "
              f"(per call {_money(c.cost_per_call, per_mtok=False)})")
        print(f"       upstream   gateway {c.provider_family} / upstream {c.upstream}")
        print(f"       context  {c.context_window or '?':>9}   "
              f"latency {c.latency_ms if c.latency_ms is not None else '?'}")

    store.record_decision(task=task.name, policy=policy.name, mode=args.mode,
                          chosen=dec.chosen[0].deploy_id if dec.chosen else "",
                          candidates=[c.deploy_id for c in dec.chosen],
                          reason=json.dumps({"funnel": dec.funnel, "why": dec.why, "diversity": dec.diversity, "strategy": dec.strategy}))
    store.commit()
    store.close()
    return 0


def cmd_explain(args) -> int:
    store = _open(args)
    policy, tasks = Policy.load(args.policy)
    if getattr(args, "include_uncredentialed", False):
        policy.require_callable = False
    task = tasks[args.task]
    pool = build_candidates(store, task, policy)
    elig = [c for c in pool if c.rejected is None]
    rej = [c for c in pool if c.rejected]
    elig.sort(key=lambda c: c.cost_per_success)

    print(f"WHY NOT — {task.name}: {len(elig)} eligible, {len(rej)} rejected\n")
    print(f"{'deployment':46s} {'p_lb':>5s} {'$/call':>10s} {'$/success':>10s}  note")
    for c in elig[:args.top]:
        print(f"{c.deploy_id:46s} {c.p_lb:5.2f} {_money(c.cost_per_call):>10s} "
              f"{_money(c.cost_per_success):>10s}")
    if rej:
        print(f"\nrejected (top {args.top} shown, grouped by reason)")
        from collections import Counter

        for reason, n in Counter(c.rejected for c in rej).most_common(args.top):
            example = next(c.deploy_id for c in rej if c.rejected == reason)
            print(f"  {n:5d}  {reason:44s} e.g. {example}")

    q = [c for c in pool if c.free_kind and c.rejected is None]
    if q:
        print("\nfree arms: cost is zero only while headroom lasts")
        print(f"{'deployment':46s} {'kind':14s} {'headroom':>9s} {'src':>10s} {'p_lb':>5s}")
        for c in sorted(q, key=lambda c: -c.p_lb)[:args.top]:
            h = "unknown" if c.headroom is None else f"{c.headroom:.0%}"
            print(f"{c.deploy_id:46s} {c.free_kind:14s} {h:>9s} {c.headroom_src:>10s} "
                  f"{c.p_lb:5.2f}")
    store.close()
    return 0


def cmd_bench(args) -> int:
    """RouterBench: verifiable-reward eval. `--self-test` needs no network/key."""
    if args.self_test:
        passed, failed = bench_mod.self_test()
        total = passed + failed
        print(f"benchmark self-test: {passed}/{total} gold answers verify")
        for t in bench_mod.TASKS:
            ok, _ = bench_mod.verify(t, bench_mod.gold_answer(t))
            if not ok:
                print(f"  FAIL {t.task_id}")
        return 1 if failed else 0

    if not args.deploy:
        print("bench: --deploy is required (or use --self-test)", file=sys.stderr)
        return 2

    store = _open(args)
    families = list(bench_mod.FAMILIES) if args.family == "all" else [args.family]
    runner = Runner(api_key=args.api_key, base_url=args.base_url, model=args.model,
                    timeout=args.timeout, max_tokens=args.max_tokens)
    for family in families:
        rep = bench_mod.run_bench(store, family, args.deploy, runner,
                                  write=not args.dry_run, limit=args.limit or None)
        print(f"\n{family:12s} -> {bench_mod.FAMILY_ROUTE_TASK[family]:16s}"
              f"  {rep.ok}/{rep.total} verified  {rep.failed} wrong  "
              f"{rep.errors} errors  cost {_money(rep.total_cost_usd, per_mtok=False)}")
        if args.verbose:
            for r in rep.rows:
                if r["ok"]:
                    mark = "ok "
                elif r["error"] == "bad_output":
                    mark = "bad"
                else:
                    mark = "err"
                cost = _money(r["cost_usd"], per_mtok=False)
                print(f"  {mark} {r['task']:14s} {r['error'] or '':16s} {cost:>10s}")
        print(f"  {'wrote' if not args.dry_run else 'dry-run (no writes)'} -> "
              f"observations task={bench_mod.FAMILY_ROUTE_TASK[family]}")
    store.close()
    return 0


def _free_port(host: str) -> int:
    """Ask the OS for an unused TCP port on `host`."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _port_busy(host: str, port: int) -> bool:
    """True when something is already listening on host:port.

    Binding (not connecting) is the check that matches what uvicorn is about to
    do; SO_REUSEADDR mirrors uvicorn's own socket options, so a port this calls
    free is a port uvicorn can actually take.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def _listening_pid(port: int) -> int | None:
    """PID holding `port` as a listener, when lsof is available (nicer errors)."""
    import shutil
    import subprocess

    if shutil.which("lsof") is None:
        return None
    try:
        out = subprocess.run(["lsof", "-nP", "-ti", f":{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    pids = [int(x) for x in out.stdout.split() if x.isdigit()]
    return pids[0] if pids else None


def cmd_proxy(args) -> int:
    """OpenAI-compatible proxy. Set provider keys + MI_CA_BUNDLE in the env."""
    try:
        import uvicorn

        from .proxy import app
    except ImportError as exc:  # fastapi/uvicorn are optional deps
        print(f"proxy needs fastapi + uvicorn: pip install '.[server]' ({exc})",
              file=sys.stderr)
        return 1

    host = args.host
    port = _free_port(host) if args.port == 0 else args.port
    if _port_busy(host, port):
        pid = _listening_pid(port)
        who = f" (pid {pid})" if pid else ""
        print(f"port {port} is already in use{who} on {host}.", file=sys.stderr)
        print(f"  pick another port:  mi proxy --port {port + 1}", file=sys.stderr)
        print("  let the OS choose:  mi proxy --port 0", file=sys.stderr)
        print(f"  or free it:         kill $(lsof -ti :{port})", file=sys.stderr)
        return 2

    print(f"MinInfer proxy on http://{host}:{port}")
    print("  POST /v1/chat/completions  {'model':'auto','task':'sql_generation',...}")
    print("  GET  /v1/models")
    if args.reload:
        # uvicorn's reloader needs an import string, not the app object.
        uvicorn.run("mininfer.proxy:app", host=host, port=port,
                    log_level=args.log_level, reload=True)
    else:
        uvicorn.run(app, host=host, port=port, log_level=args.log_level)
    return 0


def cmd_sync(args) -> int:
    """Push the local SQLite registry to Supabase / Postgres."""
    from . import sync as sync_mod

    if args.print_schema:
        print(sync_mod.ddl())
        return 0

    url = sync_mod.database_url(args.database_url)
    if not url:
        print("no database URL: put SUPABASE_DB_URL in .env, or pass --database-url\n"
              "(use the pooler connection string from Supabase -> Project Settings -> "
              "Database)", file=sys.stderr)
        return 2

    store = _open(args)
    try:
        rep = sync_mod.sync(
            store, url, evidence=args.evidence, dry_run=args.dry_run,
            apply_schema=not args.no_schema,
            tables=[t.strip() for t in args.tables.split(",") if t.strip()] or None)
    except Exception as exc:
        store.close()
        print(f"sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    store.close()
    print("sync " + ("(dry-run) — nothing written" if rep.get("dry_run") else "complete"))
    for t, n in rep["tables"].items():
        print(f"  {t:16s} {n:>8d} rows")
    return 0


def cmd_bandit(args) -> int:
    """Posterior view: what the bandit believes about each eligible arm."""
    store = _open(args)
    policy, tasks = Policy.load(args.policy)
    if args.task not in tasks:
        print(f"unknown task {args.task!r}; known: {', '.join(tasks)}", file=sys.stderr)
        store.close()
        return 2
    task = tasks[args.task]
    if args.explore_eps is not None:
        policy.exploration_eps = args.explore_eps
    if args.prior_strength is not None:
        policy.prior_strength = args.prior_strength

    pool = build_candidates(store, task, policy)
    eligible = [c for c in pool if c.rejected is None]
    rows = bandit_mod.describe(eligible, task, policy)
    print(f"BANDIT   {task.name} — {len(eligible)} eligible arms, "
          f"prior_strength={policy.prior_strength}  eps={policy.exploration_eps}")
    print(f"{'deployment':44s} {'n':>4s} {'wins':>4s} {'prior':>6s} {'mean':>6s} "
          f"{'var':>10s} {'$exp/succ':>10s}")
    for r in rows[:args.top]:
        print(f"{r['deploy_id']:44s} {r['n_obs']:>4d} {r['wins']:>4d} {r['p_prior']:>6.2f} "
              f"{r['mean']:>6.3f} {r['variance']:>10.6f} "
              f"{_money(r['expected_cost_per_success'], per_mtok=False):>10s}")
    store.close()
    return 0


def cmd_quota(args) -> int:
    """Declared free-tier limits and the live headroom the router reads."""
    from . import quota

    store = _open(args)
    if args.action == "seed":
        rep = quota.seed(store, config=args.config, dry_run=args.dry_run)
        print(f"quota seed: {rep['entries']} entries -> {rep['buckets']} buckets"
              + ("   (dry-run, nothing written)" if args.dry_run else ""))
        for name, n in sorted(rep["matched"].items()):
            print(f"  {name:26s} {n} deployment(s)")
    else:
        rows = quota.describe(store, limit=args.limit)
        if not rows:
            print("no quota buckets configured; run `mi quota seed`")
        else:
            print(f"{'deployment':44s} {'window':7s} {'used':>7s} {'limit':>7s} "
                  f"{'head':>6s} {'source':9s} when")
            for r in rows:
                head = "?" if r["headroom"] is None else f"{r['headroom']:.0%}"
                # `limit_n` is the *configured* limit and may be NULL for a bucket
                # we have only ever observed; fall back to what the provider said.
                limit = r["limit_n"] if r["limit_n"] is not None else r["observed_limit_n"]
                ex = r["exhaustion"]
                if ex["estimated_exhaustion_at"]:
                    when = (f"exhausts ~{ex['estimated_exhaustion_at'][11:16]}"
                            f" (p={ex['confidence']:.1f})")
                elif r["reset_at"]:
                    when = f"resets {r['reset_at'][11:16]}"
                else:
                    when = ""
                print(f"{r['deploy_id']:44s} {r['window']:7s} {r['used_n'] or 0:>7d} "
                      f"{('—' if limit is None else str(limit)):>7s} {head:>6s} "
                      f"{r['headroom_source']:9s} {when}")
            observed = sum(1 for r in rows if r["observed_limit_n"])
            print(f"\n  {len(rows)} bucket(s), {observed} with a provider-reported limit"
                  "  ·  headroom is min(configured, observed)")
    store.close()
    return 0


def cmd_history(args) -> int:
    """What MinInfer believed a price was, and when it changed its mind.

    `--as-of` answers "what did we believe at T" — the belief in effect at that
    instant, not the observation nearest to it. The two differ whenever we learned
    something late, which is exactly when the distinction matters.
    """
    store = _open(args)
    rows = store.price_history(args.deploy_id, as_of=args.as_of or None, limit=args.limit)
    store.close()
    if not rows:
        where = f" for {args.deploy_id}" if args.deploy_id else ""
        print(f"no price beliefs recorded{where} (run `mi reconcile`)")
        return 0
    print(f"{'as of ' + args.as_of if args.as_of else 'belief timeline'}"
          f" — {len(rows)} row(s)")
    for r in rows:
        print(f"  {r['effective_from']}  {r['deploy_id']}  ({r['kind']})")
        print(f"      {_money(r['usd_per_mtok'])}  ·  {r['state']}"
              f"  ·  from {r['source'] or '?'} observed {r['observed_at'] or '?'}")
        if r["reason"]:
            print(f"      {r['reason']}")
    return 0


def cmd_retire(args) -> int:
    """Take a deployment out of routing on purpose.

    The operator's half of `mi enable`, and the answer to a model that cannot be
    called through the API at all: OpenRouter serves some `:free` models only to
    allowlisted apps ("agentic harnesses") and answers a plain client with 401. The
    router also discovers that by calling one — it classifies it `not_api_callable`
    and retires it — but a human who already knows should not have to wait for a
    failed request, or watch the arm sit in a fallback list until one happens.

    Sticky: `status_source` is stamped, so the next ingest reports the model `live`
    from the catalogue and cannot put it back.
    """
    store = _open(args)
    ok = store.retire_deployment(
        args.deploy_id,
        args.reason or "retired by operator",
        source="review",
    )
    store.commit()
    store.close()
    if not ok:
        print(f"{args.deploy_id} is already out of routing (or unknown)", file=sys.stderr)
        return 1
    print(f"{args.deploy_id} -> deprecated")
    print(f"  restore with: mi enable {args.deploy_id}")
    return 0


def cmd_disabled(args) -> int:
    """Deployments we took out of routing from *runtime* evidence or by hand.

    Listed rather than silently dropped. A model that vanishes from the ranking
    with no explanation is indistinguishable from a bug, and this listing is the
    difference between "we removed it on purpose" and "where did it go".
    """
    store = _open(args)
    rows = [d for d in store.deployments()
            if d.get("status_source") in ("runtime", "review") and d.get("status") != "live"]
    store.close()
    if not rows:
        print("nothing is retired from runtime evidence")
        return 0
    for d in rows:
        print(f"  {d['deploy_id']}")
        print(f"      {d['status']} since {d.get('status_changed_at') or '?'}")
        print(f"      {d.get('status_reason') or ''}")
    print(f"\n  {len(rows)} retired. Restore one with: mi enable <deploy_id>")
    return 0


def cmd_enable(args) -> int:
    """Clear a runtime retirement so an arm can be routed to again.

    The escape hatch that keeps stickiness from being permanent: a provider can
    change which apps it serves, and only a human knows that.
    """
    store = _open(args)
    ok = store.enable_deployment(args.deploy_id)
    store.close()
    if not ok:
        print(f"{args.deploy_id} is not retired by a runtime observation", file=sys.stderr)
        return 1
    print(f"{args.deploy_id} -> live")
    return 0


def cmd_add_url(args) -> int:
    """Ingest an arbitrary webpage through the LangGraph agent pipeline."""
    from . import agent_ingest

    rep = agent_ingest.add_url(
        args.url, source=args.source, source_type=args.source_type,
        agent_model=args.agent_model, db=args.db, dry_run=args.dry_run, ttl=args.ttl)
    print(f"URL      {rep['url']}")
    print(f"source   {rep['source']}  type={rep['source_type']}")
    if rep.get("error"):
        print(f"error    {rep['error']}")
        return 1
    print(f"models   {rep['models_found']} found  {rep['accepted']} accepted  "
          f"{rep['quarantined']} quarantined")
    for d in rep["accepted_deployments"]:
        print(f"  + {d}")
    for r in rep["rejected"]:
        print(f"  ~ {r['deploy_id']}  ({r['reason']})")
    if args.dry_run:
        print("dry-run: nothing written")
    return 0


def cmd_observe(args) -> int:
    """Record an outcome. Phase 2's eval harness writes these; so does production."""
    store = _open(args)
    store.observe(args.deploy_id, args.task, ok=not args.fail, ts=utcnow(),
                  error_class=args.error, latency_ms=args.latency_ms,
                  tokens_in=args.tokens_in, tokens_out=args.tokens_out,
                  cost_usd=args.cost, signal_kind=args.signal_kind,
                  signal_value=args.signal_value)
    store.commit()
    print(f"recorded {'failure' if args.fail else 'success'} for {args.deploy_id} "
          f"on {args.task}")
    store.close()
    return 0


def _intent_report(args) -> int:
    """Show where an explicit task disagreed with the classifier's guess.

    That disagreement is the only ground truth the system gets for free: the
    caller naming a task is a label. Cue weights stay hand-written — six samples
    is not enough to fit anything safely — but the cases worth adding a cue for
    are exactly the disagreements, so they are worth collecting.
    """
    store = _open(args)
    rows = store.decisions_with_intent(args.limit)
    store.close()

    seen = agreed = 0
    misses: list[tuple[str, dict]] = []
    for row in rows:
        try:
            reason = json.loads(row["reason"] or "{}")
        except json.JSONDecodeError:
            continue
        info = reason.get("intent") or {}
        if "explicit" not in info:
            continue
        seen += 1
        if info.get("agreed"):
            agreed += 1
        else:
            misses.append((row["task"], info))

    print(f"labelled decisions: {seen}   classifier agreed: {agreed}   disagreed: {len(misses)}")
    if not seen:
        print("\nno labels yet — labels come from callers passing an explicit task "
              "(model: \"sql_generation\") rather than model: \"auto\"")
        return 0
    if not misses:
        print("\nthe classifier agreed with every explicit task")
        return 0

    print("\ndisagreements (newest first) — candidates for a new cue")
    for routed, info in misses[: args.top]:
        scores = ", ".join(f"{k}={v}" for k, v in (info.get("scores") or {}).items())
        print(f"  explicit={info['explicit']:18s} predicted={info['task']:18s} "
              f"conf={info.get('confidence')}  {scores}")
        print(f"    routed as {routed}")
    return 0


def cmd_intent(args) -> int:
    """Classify a prompt, or report where the classifier disagreed with a caller."""
    from . import intent as intent_mod

    if args.report:
        return _intent_report(args)

    _, tasks = Policy.load(args.policy)
    text = args.prompt or sys.stdin.read()
    if not text.strip():
        print("nothing to classify (pass a prompt or pipe one in)", file=sys.stderr)
        return 2

    default = DEFAULT_TASK
    res = intent_mod.classify(text, tasks, default=default)
    profile = tasks.get(res.task)

    print(f"PROMPT   {' '.join(text.split())[:84]}")
    print(f"TASK     {res.task}   confidence={res.confidence:.2f}  source={res.source}")
    if res.scores:
        print("CUES")
        for name, score in res.top(len(tasks)):
            print(f"  {name:20s} {score:5.2f}  {'#' * int(score)}")
    else:
        print("CUES     none matched — fell back to the default task")
    if profile:
        print(f"PROFILE  tokens {profile.tokens_in}/{profile.tokens_out}  "
              f"require {profile.require or '{}'}  min_context {profile.min_context}  "
              f"floor {profile.min_success_lb}")
    return 0


def cmd_classify(args) -> int:
    """Structured understanding: task, routing heads and entities.

    One local call answers several heads at once — the `GLiNER2.5-Decide` shape
    (task, provider, cost, context, style) — with no routed LLM call and no
    generated tokens. Uses the optional `[understanding]` layer when installed;
    otherwise it falls back to the cue classifier and *says so* rather than
    pretending the model ran.
    """
    from . import intent as intent_mod
    from . import understanding

    _, tasks = Policy.load(args.policy)
    text = args.prompt or sys.stdin.read()
    if not text.strip():
        print("nothing to classify (pass a prompt or pipe one in)", file=sys.stderr)
        return 2

    backend = understanding.backend_name()
    out: dict = {"prompt": " ".join(text.split())[:200], "backend": backend}

    providers: list[str] = []
    if args.providers:
        providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    elif backend != "none":
        # The registry knows the providers; the classifier is told them at call
        # time. A failure here is not fatal — the provider head is just omitted.
        try:
            store = _open(args)
            providers = [r["provider"] for r in store.providers_summary(limit=args.providers_top)]
            store.close()
        except Exception:
            providers = []

    decision = None
    if backend != "none":
        decision = understanding.decide(text, tasks=list(tasks), providers=providers or None)

    if decision and isinstance(decision.get("task"), dict):
        head = decision["task"]
        out["task"] = {"name": head.get("label"),
                       "confidence": head.get("confidence"),
                       "source": "gliner"}
        out["heads"] = {k: v for k, v in decision.items() if k != "task"}
    else:
        res = intent_mod.classify(text, tasks, default=DEFAULT_TASK)
        out["task"] = {"name": res.task, "confidence": res.confidence,
                       "source": res.source}
        if backend == "none":
            out["note"] = (
                "[understanding] extra not installed; cue classifier used. "
                "`pip install 'mininfer[understanding]'` enables local decisions "
                "and entity extraction (see CLOUD_ACTIVITY.md §2.0)."
            )

    if backend != "none" and not args.no_extract:
        entities = understanding.extract(
            text, list(understanding.schemas.EVIDENCE_ENTITIES))
        if entities:
            out["entities"] = entities

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_leaderboards(args) -> int:
    """Side-by-side leaderboard comparison for the models we have scored.

    Raw value plus its normalised percentile within the registry, so an AA index
    (0-100) and arena Elo (~1000-1500) can be read on one line without pretending
    they share a scale.
    """
    from . import leaderboards as lb

    store = _open(args)
    norms = benchmark_norms(store)
    needle = (args.model or "").lower()
    rows: list[tuple[str, dict, str]] = []
    for r in store.weights_with_benchmarks():
        try:
            bench = json.loads(r["benchmark"] or "{}")
        except json.JSONDecodeError:
            continue
        if not bench:
            continue
        name = r["display_name"] or r["weights_id"]
        if needle and needle not in name.lower() and needle not in r["weights_id"].lower():
            continue
        rows.append((name, bench, r["benchmark_source"] or ""))

    if not rows:
        print("no leaderboard scores matched", file=sys.stderr)
        store.close()
        return 1

    keys = sorted({k for _, b, _ in rows for k in b})

    def pct(key: str, val: float) -> float:
        lo, hi = norms.get(key, (0.0, 0.0))
        return 0.5 if hi <= lo else (float(val) - lo) / (hi - lo)

    def mean_score(bench: dict) -> float:
        vals = [pct(k, bench[k]) for k in keys if k in bench]
        return sum(vals) / len(vals) if vals else 0.0

    rows.sort(key=lambda t: -mean_score(t[1]))
    shown = rows[: args.top]

    heads = [lb.describe(k).label for k in keys]
    print(f"{'model':42s} " + " ".join(f"{h:>21s}" for h in heads))
    for name, bench, _src in shown:
        cells = []
        for k in keys:
            cells.append("—".rjust(21) if k not in bench
                         else f"{bench[k]:>10.1f} ({pct(k, bench[k]):>4.2f})".rjust(21))
        print(f"{name[:42]:42s} " + " ".join(cells))

    print("\nlegend — raw value, then position in the registry band (0 = p5, 1 = p95)")
    for k in keys:
        b = lb.describe(k)
        lo, hi = norms.get(k, (0.0, 0.0))
        print(f"  {k:20s} {b.source:22s} {b.label:30s} p5={lo:.1f} p95={hi:.1f}")
    print(f"\n{len(shown)} of {len(rows)} scored model(s), by mean normalised score")
    store.close()
    return 0


def cmd_search(args) -> int:
    """Web search, free tiers first.

    This is the tool surface a model would call, exposed so it can be exercised on
    its own. Every response is snapshotted to the raw lake *and* recorded in the
    `snapshots` table, so the registry knows what was fetched even though nothing
    was routed.
    """
    store = _open(args)
    rep = search_mod.search(args.query, provider=args.provider, limit=args.limit,
                            force=args.force, store=store)
    session = None
    if args.session:
        # Charged to the same ledger the proxy uses, so `mi search --session X`
        # and a future tool loop are bounded by one budget, not two.
        policy, _tasks = Policy.load(_policy_path())
        store.add_session_usage(args.session, calls=0, searches=1,
                                search_cost_usd=rep.cost_usd,
                                cost_usd=rep.cost_usd)
        session = store.session_usage(args.session)
    store.commit()
    store.close()
    if args.json:
        print(json.dumps(rep.as_dict(), indent=2))
        return 0 if rep.ok else 1
    if not rep.ok:
        print(f"no results — tried: {', '.join(rep.tried)}", file=sys.stderr)
        if rep.error:
            print(f"  {rep.error}", file=sys.stderr)
        return 1
    cost = "free" if not rep.cost_usd else f"${rep.cost_usd:.4f}"
    print(f"{len(rep.results)} result(s) from {rep.provider} ({cost})"
          f"{' [cached]' if rep.from_cache else ''}")
    if rep.tried and len(rep.tried) > 1:
        print(f"  tried first: {', '.join(rep.tried[:-1])}")
    for i, r in enumerate(rep.results, 1):
        print(f"\n{i}. {r['title']}\n   {r['url']}")
        if r.get("snippet"):
            print(f"   {r['snippet'][:300]}")
    print(f"\n  snapshot: {rep.snapshot}")
    if session:
        print(f"  session {session['session_id']}: {session['searches']} search(es),"
              f" ${session['cost_usd']:.4f} total")
    return 0


def cmd_metrics(args) -> int:
    """Enrich deployments with provider performance metrics (throughput / TTFT)."""
    store = _open(args)
    if args.source != "vercel":
        print(f"unknown metrics source {args.source!r} (only 'vercel' is supported)",
              file=sys.stderr)
        store.close()
        return 2
    snap = metrics_mod.fetch_vercel(force=args.force, persist=not args.dry_run)
    rows = metrics_mod.parse_metrics(snap.payload)
    if args.dry_run:
        rep = metrics_mod.preview(store, rows)
        have = sum(1 for r in rows if r["throughput"] is not None
                   or r["first_token_ms"] is not None)
        print(f"metrics (dry-run) — {len(rows)} models parsed, {have} with values, "
              f"{rep['quarantined']} would be refused; nothing written")
        for f in rep["quarantine_fields"][:10]:
            print(f"  REFUSED {f}")
        for r in rows[:10]:
            print(f"  {r['model']:45s} ttft={r['first_token_ms']} tput={r['throughput']}")
        store.close()
        return 0
    rep = metrics_mod.apply(store, rows)
    store.record_snapshot(snap)
    store.commit()
    print(f"metrics complete — {rep['parsed']} parsed, {rep['updated']} deployments "
          f"updated, {rep['missing']} not in registry, "
          f"{rep['empty']} with nothing to apply, "
          f"{rep['quarantined']} metric(s) refused")
    for f in rep["quarantine_fields"][:10]:
        print(f"  REFUSED {f}  (see `mi stats` / quarantine table)")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_env()
    p = argparse.ArgumentParser(prog="mi", description="MinInfer model router")
    p.add_argument("--db", default=str(DB))
    p.add_argument("--policy", default=str(POLICY))
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="pull sources into the registry")
    i.add_argument("sources", nargs="*")
    i.add_argument("--force", action="store_true", help="ignore the raw cache")
    i.add_argument("--no-endpoints", action="store_true",
                   help="skip OpenRouter per-endpoint pricing")
    i.add_argument("--endpoints-top", type=int, default=25,
                   help="also fetch endpoints for the N highest-benchmark models")
    i.add_argument("--limit", type=int, default=0, help="cap models per source")
    i.set_defaults(fn=cmd_ingest)

    rc = sub.add_parser("reconcile",
                        help="re-derive prices from stored evidence")
    rc.set_defaults(fn=cmd_reconcile)

    an = sub.add_parser("anomalies", help="pricing anomalies awaiting review")
    an.add_argument("--status", default="open",
                    choices=["open", "acknowledged", "resolved", ""],
                    help="filter by status; '' shows the whole history")
    an.add_argument("--limit", type=int, default=50)
    an.set_defaults(fn=cmd_anomalies)

    an1 = sub.add_parser("anomaly", help="acknowledge or resolve a pricing anomaly")
    an1.add_argument("anomaly_id")
    an1.add_argument("--acknowledge", action="store_true", help="seen, still wrong")
    an1.add_argument("--resolve", action="store_true", help="closed")
    an1.add_argument("--note", default="")
    an1.set_defaults(fn=cmd_anomaly)

    tr = sub.add_parser("transitions", help="pricing-state history (free -> paid, ...)")
    tr.add_argument("deploy_id", nargs="?", help="omit for the whole registry")
    tr.add_argument("--limit", type=int, default=50)
    tr.set_defaults(fn=cmd_transitions)

    hi = sub.add_parser("history", help="what we believed a price was, and when")
    hi.add_argument("deploy_id", nargs="?", help="omit for the whole registry")
    hi.add_argument("--as-of", dest="as_of", default="",
                    help="ISO timestamp: the belief in effect at that instant")
    hi.add_argument("--limit", type=int, default=50)
    hi.set_defaults(fn=cmd_history)

    dis = sub.add_parser("disabled",
                         help="deployments retired from runtime evidence or by hand")
    dis.set_defaults(fn=cmd_disabled)

    rt = sub.add_parser("retire", help="take a deployment out of routing for good")
    rt.add_argument("deploy_id")
    rt.add_argument("--reason", default="", help="why (shown by `mi disabled`)")
    rt.set_defaults(fn=cmd_retire)

    en = sub.add_parser("enable", help="clear a runtime retirement (uncallable model)")
    en.add_argument("deploy_id")
    en.set_defaults(fn=cmd_enable)

    v = sub.add_parser("verify",
                       help="re-read sources and report price / free-status drift")
    v.add_argument("sources", nargs="*")
    v.add_argument("--force", action="store_true", default=True,
                   help="ignore the raw cache (default: on — a cache cannot verify)")
    v.add_argument("--no-endpoints", action="store_true",
                   help="skip OpenRouter per-endpoint pricing")
    v.add_argument("--endpoints-top", type=int, default=25)
    v.add_argument("--limit", type=int, default=0)
    v.add_argument("--report", default="",
                   help="write the drift report as JSON to this path")
    v.set_defaults(fn=cmd_verify)

    sg = sub.add_parser("seed-google",
                        help="add the current direct Gemini deployments")
    sg.set_defaults(fn=cmd_seed_google)

    rv = sub.add_parser("reviews",
                        help="models hibernated for review (was free, now charges)")
    rv.set_defaults(fn=cmd_reviews)

    rd = sub.add_parser("review", help="approve or reject a hibernated model")
    rd.add_argument("deploy_id")
    rd.add_argument("--approve", action="store_true", help="keep using it at the new price")
    rd.add_argument("--reject", action="store_true", help="stop using it")
    rd.add_argument("--note", default="", help="why (recorded on the row)")
    rd.set_defaults(fn=cmd_review)

    sub.add_parser("sources", help="provider roadmap + availability").set_defaults(fn=cmd_sources)

    r = sub.add_parser("resolve", help="entity resolution pass")
    r.add_argument("--no-auto", action="store_true")
    r.add_argument("--adjudicate", action="store_true",
                   help="LLM-review the near-match proposals")
    r.add_argument("--agent-model", default=None,
                   help="deploy_id for the adjudicator LLM (env MI_AGENT_MODEL / MI_AGENT_MODEL)")
    r.add_argument("--min-confidence", type=float, default=0.7)
    r.add_argument("--limit", type=int, default=60,
                   help="cap near-match proposals sent to the adjudicator (0 = all)")
    r.add_argument("--dry-run", action="store_true", help="decide but merge nothing")
    r.set_defaults(fn=cmd_resolve)

    sub.add_parser("stats", help="registry counters").set_defaults(fn=cmd_stats)

    bd = sub.add_parser("bandit", help="posterior view for a task")
    bd.add_argument("task")
    bd.add_argument("--top", type=int, default=20)
    bd.add_argument("--explore-eps", type=float)
    bd.add_argument("--prior-strength", type=float)
    bd.set_defaults(fn=cmd_bandit)

    sy = sub.add_parser("sync", help="push the registry to Supabase/Postgres")
    sy.add_argument("--database-url", default=None,
                    help="Postgres URI (default: $SUPABASE_DB_URL from .env)")
    sy.add_argument("--tables", default="", help="comma-separated subset")
    sy.add_argument("--evidence", action="store_true",
                    help="also sync the (large) evidence table")
    sy.add_argument("--no-schema", action="store_true",
                    help="do not apply supabase/schema.sql first")
    sy.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    sy.add_argument("--print-schema", action="store_true",
                    help="print supabase/schema.sql and exit (paste into the SQL editor)")
    sy.set_defaults(fn=cmd_sync)

    for name, fn in (("route", cmd_route), ("explain", cmd_explain)):
        s = sub.add_parser(name)
        s.add_argument("task")
        s.add_argument("--mode", choices=["auto", "manual"], default="auto")
        s.add_argument("--candidates", default="", help="comma-separated deploy ids")
        s.add_argument("--top-k", type=int, default=3)
        s.add_argument("--top", type=int, default=12)
        s.add_argument("--objective", choices=["cost_per_success", "quality", "latency"])
        s.add_argument("--max-cost-in", type=float)
        s.add_argument("--min-success", type=float)
        s.add_argument("--allow-unknown-price", action="store_true")
        s.add_argument("--include-uncredentialed", action="store_true",
                       help="also rank providers we hold no API key for")
        s.add_argument("--bandit", action="store_true",
                       help="rank by Thompson-sampled cost-per-success")
        s.add_argument("--explore-eps", type=float,
                       help="epsilon-greedy exploration probability (bandit)")
        s.set_defaults(fn=fn)

    b = sub.add_parser("bench", help="run RouterBench against a deployment")
    b.add_argument("family", choices=["sql", "extraction", "tool_call", "reasoning", "all"])
    b.add_argument("--deploy", default="", help="deploy_id, e.g. openrouter:qwen/qwen3.8-27b:free")
    b.add_argument("--self-test", action="store_true",
                   help="verify gold answers offline; no network, no writes")
    b.add_argument("--dry-run", action="store_true", help="run but do not write observations")
    b.add_argument("--base-url", help="override provider endpoint")
    b.add_argument("--api-key", help="override API key")
    b.add_argument("--model", help="override model id sent to the endpoint")
    b.add_argument("--timeout", type=float, default=120.0)
    b.add_argument("--max-tokens", type=int, default=1024)
    b.add_argument("--limit", type=int, default=0, help="cap tasks per family")
    b.add_argument("-v", "--verbose", action="store_true", help="per-task rows")
    b.set_defaults(fn=cmd_bench)

    qu = sub.add_parser("quota", help="declared free-tier limits and live headroom")
    qu.add_argument("action", choices=["seed", "show"])
    qu.add_argument("--config", default="config/quotas.yaml")
    qu.add_argument("--dry-run", action="store_true")
    qu.add_argument("--limit", type=int, default=40)
    qu.set_defaults(fn=cmd_quota)

    a = sub.add_parser("add-url", help="ingest an arbitrary webpage via LLM agents")
    a.add_argument("url")
    a.add_argument("--source", default=None, help="source name (default: add-url)")
    a.add_argument("--source-type", default="auto",
                   choices=["auto", "leaderboard", "pricing", "model_catalog", "other"])
    a.add_argument("--agent-model", default=None,
                   help="deploy_id for the extraction LLM (env MI_AGENT_MODEL / MI_AGENT_MODEL)")
    a.add_argument("--dry-run", action="store_true", help="extract+validate but write nothing")
    a.add_argument("--ttl", type=int, default=6 * 3600, help="fetch cache TTL seconds")
    a.set_defaults(fn=cmd_add_url)

    x = sub.add_parser("proxy", help="run the OpenAI-compatible HTTP proxy")
    x.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    x.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")),
                   help="TCP port; 0 asks the OS for a free one")
    x.add_argument("--log-level", default="info")
    x.add_argument("--reload", action="store_true",
                   help="auto-reload on source changes (development)")
    x.set_defaults(fn=cmd_proxy)

    ldb = sub.add_parser("leaderboards",
                         help="compare models across ingested leaderboards")
    ldb.add_argument("model", nargs="?", default="", help="substring filter")
    ldb.add_argument("--top", type=int, default=25)
    ldb.set_defaults(fn=cmd_leaderboards)

    it = sub.add_parser("intent", help="classify a prompt into a task (what `auto` does)")
    it.add_argument("prompt", nargs="?", default="")
    it.add_argument("--report", action="store_true",
                    help="show where an explicit task disagreed with the classifier")
    it.add_argument("--top", type=int, default=15)
    it.add_argument("--limit", type=int, default=500, help="decisions to scan")
    it.add_argument("--policy", default=str(POLICY))
    it.set_defaults(fn=cmd_intent)

    cl = sub.add_parser("classify",
                        help="local decision: task + provider/cost/context/style + entities")
    cl.add_argument("prompt", nargs="?", default="")
    cl.add_argument("--policy", default=str(POLICY))
    cl.add_argument("--providers", default="",
                    help="comma-separated provider labels for the provider head")
    cl.add_argument("--providers-top", type=int, default=40,
                    help="take the top N registry providers when --providers is unset")
    cl.add_argument("--no-extract", action="store_true",
                    help="classify only; skip entity extraction")
    cl.set_defaults(fn=cmd_classify)

    se = sub.add_parser("search", help="web search (free tiers first, Tavily as fallback)")
    se.add_argument("query")
    se.add_argument("--provider", default="auto",
                    choices=["auto", "wikipedia", "duckduckgo", "tavily"])
    se.add_argument("--limit", type=int, default=5)
    se.add_argument("--force", action="store_true", help="ignore the raw cache")
    se.add_argument("--json", action="store_true")
    se.add_argument("--session", help="charge this search to a session ledger")
    se.set_defaults(fn=cmd_search)

    mt = sub.add_parser("metrics",
                        help="enrich deployments with perf metrics (throughput/TTFT)")
    mt.add_argument("--source", default="vercel")
    mt.add_argument("--force", action="store_true", help="ignore the raw cache")
    mt.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    mt.set_defaults(fn=cmd_metrics)

    o = sub.add_parser("observe", help="record an outcome observation")
    o.add_argument("deploy_id")
    o.add_argument("task")
    o.add_argument("--fail", action="store_true")
    o.add_argument("--error", help="429 | timeout | 5xx | bad_output")
    o.add_argument("--latency-ms", type=float)
    o.add_argument("--tokens-in", type=int)
    o.add_argument("--tokens-out", type=int)
    o.add_argument("--cost", type=float)
    o.add_argument("--signal-kind", default="verified",
                   choices=["verified", "subjective", "provider_reported"])
    o.add_argument("--signal-value", type=float)
    o.set_defaults(fn=cmd_observe)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
