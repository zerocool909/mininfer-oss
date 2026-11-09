#!/usr/bin/env python
"""Aggregate a `model_sweep.py` run into per-model and per-family scorecards.

Reads `runs.jsonl` (read-only, resumable output) and writes `summary.json` +
`report.md` next to it. Nothing here calls a provider, so it is free to re-run.

    python scripts/model_eval.py reports/model-sweep/full

Columns, and why each is separate:
  api_ok      the transport worked (HTTP 200 with content). A model that 404s
              is a registry problem, not a model-quality problem.
  acc         grade_ok / calls. This is the headline: did it answer correctly,
              counting a transport failure as a failure.
  acc_ok      grade_ok / api_ok. Quality *given* that it answered, so a flaky
              gateway does not look like a dumb model.
  lat_p50/p90 wall-clock including proxy overhead.
  $/call      mean estimated cost, from registry prices x reported tokens.
  $/correct   total cost / correct answers. The router's actual objective.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
from collections import defaultdict


def pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def p(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round((len(s) - 1) * q))))
    return round(s[i], 1)


#: Outcomes that say nothing about model quality. A rate-limited or timed-out
#: call is a gateway fact; counting it as a wrong answer would rank a good model
#: below a bad one purely because it was called at a busy minute.
UNMEASURED = {"429", "rate_limited", "auth_error", "http_402", "payment_required",
              "TimeoutException", "ConnectError", "ReadTimeout", "ReadError",
              "RemoteProtocolError"}


def load(path: pathlib.Path) -> list[dict]:
    """Last-write-wins per (model, prompt), preferring a successful attempt."""
    by_pair: dict[tuple[str, str], dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (r["model"], r["prompt_id"])
        prev = by_pair.get(key)
        if prev is None or (r.get("ok") and not prev.get("ok")):
            by_pair[key] = r
    return list(by_pair.values())


def summarize(rows: list[dict], items: list[dict]) -> dict:
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)

    families = {it["id"]: it["family"] for it in items}
    per_item = {it["id"]: {"family": it["family"], "calls": 0, "ok": 0, "api_ok": 0} for it in items}
    for r in rows:
        b = per_item.setdefault(r["prompt_id"], {"family": r.get("family", "?"),
                                                 "calls": 0, "ok": 0, "api_ok": 0})
        b["calls"] += 1
        b["ok"] += 1 if r.get("grade_ok") else 0
        b["api_ok"] += 1 if r.get("ok") else 0

    models = []
    for model, rs in by_model.items():
        rs = [r for r in rs if not r.get("skipped")]
        if not rs:
            continue
        n = len(rs)
        ok = sum(1 for r in rs if r.get("ok"))
        good = sum(1 for r in rs if r.get("grade_ok"))
        unmeasured = sum(1 for r in rs
                         if not r.get("ok") and r.get("error_type") in UNMEASURED)
        measured = n - unmeasured
        lat = [r["latency_ms"] for r in rs if r.get("ok") and r.get("latency_ms")]
        cost = sum((r.get("cost_usd") or 0.0) for r in rs)
        pins = [r.get("tokens_in") or 0 for r in rs]
        pouts = [r.get("tokens_out") or 0 for r in rs]
        errs: dict[str, int] = defaultdict(int)
        fails = []
        for r in rs:
            if not r.get("ok"):
                errs[r.get("error_type") or r.get("error") or "unknown"] += 1
            elif not r.get("grade_ok"):
                fails.append({"prompt_id": r["prompt_id"], "detail": r.get("grade_detail", "")[:160]})
        by_family: dict[str, dict] = defaultdict(lambda: {"n": 0, "ok": 0})
        for r in rs:
            fam = families.get(r["prompt_id"], r.get("family", "?"))
            by_family[fam]["n"] += 1
            by_family[fam]["ok"] += 1 if r.get("grade_ok") else 0
        models.append({
            "model": model,
            "base": rs[0].get("base"),
            "provider": rs[0].get("provider"),
            "pricing_state": rs[0].get("pricing_state"),
            "n": n,
            "measured": measured,
            "unmeasured": unmeasured,
            "api_ok": ok,
            "api_ok_pct": pct(ok, n),
            "reach_pct": pct(ok, measured),
            "correct": good,
            "acc_pct": pct(good, measured),
            "acc_ok_pct": pct(good, ok),
            "lat_p50": p(lat, 0.5),
            "lat_p90": p(lat, 0.9),
            "lat_max": round(max(lat), 1) if lat else None,
            "tokens_in": round(statistics.mean(pins), 1) if pins else 0,
            "tokens_out": round(statistics.mean(pouts), 1) if pouts else 0,
            "cost_usd": round(cost, 6),
            "cost_per_call": round(cost / n, 6),
            "cost_per_correct": round(cost / good, 6) if good else None,
            "errors": dict(sorted(errs.items(), key=lambda kv: -kv[1])),
            "fails": fails,
            "families": {k: {"n": v["n"], "ok": v["ok"], "pct": pct(v["ok"], v["n"])}
                         for k, v in sorted(by_family.items())},
        })
    models.sort(key=lambda m: (-m["acc_pct"], m["cost_per_call"] or 0, -(m["lat_p50"] or 0)))

    total_cost = sum(m["cost_usd"] for m in models)
    total_calls = sum(m["n"] for m in models)
    total_measured = sum(m["measured"] for m in models)
    total_correct = sum(m["correct"] for m in models)
    return {
        "models": len(models),
        "calls": total_calls,
        "measured": total_measured,
        "rate_limited": total_calls - total_measured,
        "total_cost_usd": round(total_cost, 4),
        "total_correct": total_correct,
        "overall_acc_pct": pct(total_correct, total_measured),
        "items": per_item,
        "scorecard": models,
    }


def _table(models: list[dict], n: int = 40) -> list[str]:
    out = ["| # | model | state | acc | acc_ok | p50 ms | $/call | $/correct |",
           "|--:|:--|:--|--:|--:|--:|--:|--:|"]
    for i, m in enumerate(models[:n], 1):
        out.append(
            f"| {i} | `{m['model']}` | {m['pricing_state']} | "
            f"{m['correct']}/{m['measured']} ({m['acc_pct']}%) | {m['acc_ok_pct']}% | "
            f"{m['lat_p50'] if m['lat_p50'] is not None else '—'} | "
            f"${m['cost_per_call']:.5f} | "
            f"{('$%.4f' % m['cost_per_correct']) if m['cost_per_correct'] is not None else '—'} |")
    return out


def report(s: dict) -> str:
    sc = s["scorecard"]
    free = [m for m in sc if m["pricing_state"] in ("free_with_quota", "free", "trial")]
    paid = [m for m in sc if m not in free]
    full = [m for m in sc if m["measured"] == 12]
    partial = [m for m in sc if 0 < m["measured"] < 12]
    perfect = [m for m in full if m["correct"] == 12]
    dead = [m for m in sc if m["api_ok_pct"] < 50]
    lines = [
        "# Model sweep — curated known-answer evaluation",
        "",
        f"- **models**: {s['models']}  ·  **calls**: {s['calls']}  ·  "
        f"**measured**: {s['measured']}  ·  **rate-limited/unmeasured**: {s['rate_limited']}",
        f"- **overall accuracy**: {s['overall_acc_pct']}%  ·  **spend**: ${s['total_cost_usd']}",
        f"- **full coverage** (12/12 measured): {len(full)} models  ·  "
        f"**partial**: {len(partial)}",
        f"- **perfect scores** ({len(perfect)}): " +
        (", ".join(f"`{m['model']}`" for m in perfect[:15]) or "none"),
        f"- **unreachable / <50% transport** ({len(dead)}): " +
        (", ".join(f"`{m['model']}`" for m in dead[:8]) or "none"),
        "",
        "## Top 40 by accuracy",
        "",
    ]
    lines += _table([m for m in sc if m["measured"] == 12], 40)
    if paid:
        lines += ["", "## Free-tier arms (zero marginal cost)", ""]
        lines += _table(sorted([m for m in free if m["measured"] == 12],
                               key=lambda m: (-m["acc_pct"], -(m["lat_p50"] or 0))), 40)
        lines += ["", "## Best value: cost per correct answer (paid, ≥50% acc)", ""]
        value = [m for m in paid if m["measured"] == 12 and m["acc_pct"] >= 50 and m["cost_per_correct"]]
        lines += _table(sorted(value, key=lambda m: m["cost_per_correct"])[:30], 30)
    else:
        # A free-only report: the headline table is the whole population, so a
        # duplicate "free arms" section adds nothing. Rank latency instead.
        lines += ["", "## Fastest full-coverage arms", ""]
        fast = [m for m in sc if m["measured"] == 12 and m["lat_p50"] is not None]
        lines += _table(sorted(fast, key=lambda m: m["lat_p50"]), 40)
    lines += ["", "## Per-item difficulty", "",
              "| item | family | accuracy | api_ok |", "|:--|:--|--:|--:|"]
    for pid, b in sorted(s["items"].items(), key=lambda kv: pct(kv[1]["ok"], kv[1]["calls"])):
        lines.append(f"| {pid} | {b['family']} | {pct(b['ok'], b['calls'])}% | {pct(b['api_ok'], b['calls'])}% |")
    lines += ["", "## Transport failures (top)", ""]
    errs: dict[str, int] = defaultdict(int)
    for m in sc:
        for e, c in m["errors"].items():
            errs[e] += c
    for e, c in sorted(errs.items(), key=lambda kv: -kv[1])[:15]:
        lines.append(f"- `{e}` — {c}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("outdir", nargs="?", default="reports/model-sweep/full")
    ap.add_argument("--dataset", default="scripts/eval_dataset.json")
    ap.add_argument("--tier", choices=["all", "free", "paid"], default="all")
    args = ap.parse_args()
    outdir = pathlib.Path(args.outdir)
    rows = load(outdir / "runs.jsonl")
    items = json.loads(pathlib.Path(args.dataset).read_text(encoding="utf-8"))
    # Filter rows *before* aggregation so the headline totals (calls, spend,
    # overall accuracy) describe the same population as the scorecard.
    FREE = ("free_with_quota", "free", "trial")
    if args.tier == "free":
        rows = [r for r in rows if r.get("pricing_state") in FREE]
    elif args.tier == "paid":
        rows = [r for r in rows if r.get("pricing_state") not in FREE]
    s = summarize(rows, items)
    (outdir / "summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    md = report(s)
    (outdir / "report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
