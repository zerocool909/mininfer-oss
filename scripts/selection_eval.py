#!/usr/bin/env python
"""Model-selection evaluation: are only *associated* models called on real APIs?

"Associated" = the arm is a general-purpose text generator, not an ASR/TTS/music/
image/embedding/rerank/safety model. `router.special_role` is the registry's own
test for that, so this uses the same definition the primary router gates on.

It reads the live `observations` table (real provider calls: real latency, real
error classes) and splits them by caller:

    primary  the router-chosen chain — what answers the user
    shadow   the background trial (`signal_kind='judge_trial'`) that explores
             untried *free* arms to gather evidence

The point of the split is that the two select arms by different rules, and only
one of them applies the role/capability gate.

Read-only: it never writes to the registry. Run:
    python scripts/selection_eval.py
    python scripts/selection_eval.py --split 2026-10-11T08:39:23+00:00   # before/after
    python scripts/selection_eval.py --since <ts> --until <ts>            # one window

The `--split` timestamp is the deploy that added the trial gate; the report then
carries a before/after table (and the same numbers are printed to stdout), so the
gain is reproducible rather than a one-off measurement.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import statistics

from mininfer.router import special_role


def _connect_ro(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{pathlib.Path(db).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _role_map(conn: sqlite3.Connection) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for r in conn.execute("SELECT deploy_id, provider_model_id, weights_id FROM deployments"):
        out[r["deploy_id"]] = special_role(dict(r))
    return out


def _is_shadow(row: sqlite3.Row) -> bool:
    return (row["signal_kind"] == "judge_trial") or ("shadow" in (row["meta"] or ""))


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _summarize(rows: list[sqlite3.Row], roles: dict[str, str | None]) -> dict:
    if not rows:
        return {"attempts": 0}
    non_chat = [r for r in rows if roles.get(r["deploy_id"])]
    outcomes: dict[str, int] = {}
    for r in rows:
        key = r["error_class"] or "OK"
        outcomes[key] = outcomes.get(key, 0) + 1
    lat = [r["latency_ms"] for r in rows if r["latency_ms"]]
    lat_sorted = sorted(lat)
    non_chat_lat = [r["latency_ms"] or 0 for r in non_chat]
    return {
        "attempts": len(rows),
        "non_chat": len(non_chat),
        "non_chat_pct": _pct(len(non_chat), len(rows)),
        "non_chat_by_role": _counts(roles.get(r["deploy_id"]) for r in non_chat),
        "outcomes": dict(sorted(outcomes.items(), key=lambda kv: -kv[1])),
        "protocol_failures": sum(1 for r in rows
                                 if r["error_class"] in ("http_400", "http_402",
                                                         "not_api_callable")),
        "hard_failures": sum(1 for r in rows
                             if r["error_class"] in ("http_400", "http_402",
                                                     "5xx", "not_api_callable")),
        "provider_time_s": round(sum(lat) / 1000.0, 1) if lat else 0.0,
        "non_chat_provider_time_s": round(sum(non_chat_lat) / 1000.0, 1),
        "non_chat_by_provider": _counts(
            r["deploy_id"].split(":")[0].split("/")[0] for r in non_chat),
        "ok_pct": _pct(outcomes.get("OK", 0), len(rows)),
        "latency_ms": {
            "p50": round(statistics.median(lat), 1) if lat else None,
            "p90": round(lat_sorted[int(len(lat_sorted) * 0.9)], 1) if lat else None,
        },
    }


def _counts(values) -> dict:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def _comparison(before: dict, after: dict) -> list[dict]:
    """Flat before/after rows for the metrics the trial gate is supposed to move.

    Each row carries both windows and their signed delta, so the report reads as
    a result rather than two tables the reader has to subtract by eye.
    """
    def at(block, path):
        cur = block
        for key in path:
            cur = (cur or {}).get(key) if isinstance(cur, dict) else None
        return cur

    def val(block, path):
        v = at(block, path)
        return v if isinstance(v, (int, float)) else 0

    specs = [
        ("shadow trial attempts", ("attempts",)),
        ("  non-chat calls", ("non_chat",)),
        ("  non-chat %", ("non_chat_pct",)),
        ("  hard failures (400/402/5xx)", ("hard_failures",)),
        ("  non-chat provider time (s)", ("non_chat_provider_time_s",)),
        ("  non-chat calls -> groq", ("non_chat_by_provider", "groq")),
        ("  non-chat calls -> openrouter", ("non_chat_by_provider", "openrouter")),
    ]
    return [{"metric": name, "before": val(before, path), "after": val(after, path),
             "delta": round(val(after, path) - val(before, path), 1)}
            for name, path in specs]


def _write_report(path: pathlib.Path, summary: dict, meta: dict) -> None:
    p, s = summary["primary"], summary["shadow"]
    lines = [
        "# Model-selection evaluation — are only associated models called?", "",
        f"- registry: `{meta['db']}`  ·  observations analysed: {meta['total']}",
        f"- window: {meta['first_ts']} → {meta['last_ts']}", "",
        "## Non-chat (unassociated) arms actually called", "",
        "| caller | attempts | non-chat | non-chat % | protocol failures (400/402) |",
        "| --- | --- | --- | --- | --- |",
        f"| primary (router chain) | {p.get('attempts')} | {p.get('non_chat')} | "
        f"{p.get('non_chat_pct')}% | {p.get('protocol_failures')} |",
        f"| shadow trial (background) | {s.get('attempts')} | {s.get('non_chat')} | "
        f"{s.get('non_chat_pct')}% | {s.get('protocol_failures')} |",
        "",
        "## Outcomes", "",
        "| caller | OK % | outcomes | latency p50 / p90 (ms) |",
        "| --- | --- | --- | --- |",
        f"| primary | {p.get('ok_pct')}% | {json.dumps(p.get('outcomes'))} | "
        f"{p.get('latency_ms', {}).get('p50')} / {p.get('latency_ms', {}).get('p90')} |",
        f"| shadow | {s.get('ok_pct')}% | {json.dumps(s.get('outcomes'))} | "
        f"{s.get('latency_ms', {}).get('p50')} / {s.get('latency_ms', {}).get('p90')} |",
        "",
        "## Non-chat arms the shadow trial called", "",
        "| role | arm | calls |", "| --- | --- | --- |",
    ]
    for arm, n in sorted(summary["shadow_non_chat_arms"].items(),
                         key=lambda kv: -kv[1]):
        lines.append(f"| {summary['roles'].get(arm) or '?'} | `{arm}` | {n} |")

    lines += ["", "## Primary path, by task", "",
              "| task | attempts | OK % | distinct arms |", "| --- | --- | --- | --- |"]
    for task, b in sorted(summary["primary_by_task"].items()):
        lines.append(f"| {task} | {b['attempts']} | {b['ok_pct']}% | {b['distinct_arms']} |")

    comp = summary.get("comparison")
    if comp:
        lines += ["", f"## Before / after (split at {comp['split']})", "",
                  "| metric | before | after | delta |", "| --- | --- | --- | --- |"]
        for row in comp["rows"]:
            lines.append(f"| {row['metric']} | {row['before']} | {row['after']} | "
                         f"{row['delta']:+} |")
        lines += ["", "primary-path non-chat calls: "
                  f"{comp['primary_before'].get('non_chat', 0)} → "
                  f"{comp['primary_after'].get('non_chat', 0)} "
                  "(the gate only changes the background trial)."]

    lines += ["", "## Reading", "",
              "The **primary** chain gates arms through `router._reject`, which "
              "rejects any `special_role` arm unless the task names it in "
              "`allow_roles` — so the user-facing answer is only ever produced by "
              "an associated model.", "",
              "The **shadow trial** (`_run_shadow_trial`) builds its arm list "
              "directly from `store.deployments()` with only `free + has key + "
              "low n_obs`, and never calls `special_role`/`_reject`. That is the "
              "only path where unassociated models are dialled.", ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="mininfer.db")
    ap.add_argument("--out", default="reports/selection-eval")
    ap.add_argument("--since", default=None,
                    help="only observations with ts >= this ISO timestamp")
    ap.add_argument("--until", default=None,
                    help="only observations with ts < this ISO timestamp")
    ap.add_argument("--split", default=None,
                    help="also emit a before/after comparison split at this ISO "
                         "timestamp (e.g. the deploy that added the trial gate)")
    args = ap.parse_args()

    conn = _connect_ro(args.db)
    roles = _role_map(conn)
    rows = conn.execute(
        "SELECT deploy_id, task, ok, error_class, latency_ms, signal_kind, meta, ts "
        "FROM observations ORDER BY id").fetchall()
    conn.close()
    if args.since:
        rows = [r for r in rows if r["ts"] >= args.since]
    if args.until:
        rows = [r for r in rows if r["ts"] < args.until]
    if not rows:
        print("[selection] no observations yet")
        return 1

    shadow = [r for r in rows if _is_shadow(r)]
    primary = [r for r in rows if not _is_shadow(r)]

    primary_by_task: dict[str, dict] = {}
    for task, group in _group(primary, lambda r: r["task"]).items():
        summary = _summarize(group, roles)
        primary_by_task[task] = {
            "attempts": summary["attempts"],
            "ok_pct": summary["ok_pct"],
            "distinct_arms": len({r["deploy_id"] for r in group}),
        }

    summary = {
        "primary": _summarize(primary, roles),
        "shadow": _summarize(shadow, roles),
        "roles": {k: v for k, v in roles.items() if v},
        "shadow_non_chat_arms": _counts(
            r["deploy_id"] for r in shadow if roles.get(r["deploy_id"])),
        "primary_by_task": primary_by_task,
    }
    if args.split:
        before = [r for r in rows if r["ts"] < args.split]
        after = [r for r in rows if r["ts"] >= args.split]
        shadow_before = _summarize([r for r in before if _is_shadow(r)], roles)
        shadow_after = _summarize([r for r in after if _is_shadow(r)], roles)
        summary["comparison"] = {
            "split": args.split,
            "before": shadow_before,
            "after": shadow_after,
            "primary_before": _summarize([r for r in before if not _is_shadow(r)], roles),
            "primary_after": _summarize([r for r in after if not _is_shadow(r)], roles),
            "rows": _comparison(shadow_before, shadow_after),
        }
    meta = {"db": args.db, "total": len(rows), "since": args.since,
            "until": args.until, "split": args.split,
            "first_ts": rows[0]["ts"], "last_ts": rows[-1]["ts"]}

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = pathlib.Path(args.out) / stamp
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "summary.json").write_text(
        json.dumps({"meta": meta, "summary": summary}, indent=2), encoding="utf-8")
    _write_report(outdir / "report.md", summary, meta)

    p, s = summary["primary"], summary["shadow"]
    print(f"[selection] primary: {p['attempts']} attempts, {p['non_chat']} non-chat "
          f"({p['non_chat_pct']}%), OK {p['ok_pct']}%")
    print(f"[selection] shadow : {s['attempts']} attempts, {s['non_chat']} non-chat "
          f"({s['non_chat_pct']}%), protocol failures {s['protocol_failures']}")
    if summary.get("comparison"):
        print(f"[selection] before/after at {args.split}:")
        for row in summary["comparison"]["rows"]:
            print(f"    {row['metric']:34s} {row['before']:>8} -> {row['after']:<8} "
                  f"({row['delta']:+})")
    print(f"[selection] report: {outdir / 'report.md'}")
    return 0


def _group(rows, key):
    out: dict = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
