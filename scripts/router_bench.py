#!/usr/bin/env python
"""Routing-path benchmark: how long does one `build_candidates` take, and why?

The router builds its candidate list on every chat call, so this measures the
hot path directly (no server, no model calls). It reports wall time, the number
of SQL statements issued, and the candidate count, for a few tasks — enough to
prove an optimization moved the number and to catch a regression.

Run:
    python scripts/router_bench.py
    python scripts/router_bench.py --iters 20 --tasks general_chat,extraction
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import sqlite3
import statistics
import tempfile
import time


def _copy_db(src_path: str) -> pathlib.Path:
    """Work on a snapshot so the live registry can keep serving (and so the
    benchmark is not measuring lock contention with a running server)."""
    tmp = pathlib.Path(tempfile.gettempdir()) / "mi_router_bench.db"
    if tmp.exists():
        tmp.unlink()
    src = sqlite3.connect(f"file:{pathlib.Path(src_path).as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(tmp))
    src.backup(dst)
    dst.close()
    src.close()
    return tmp


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="mininfer.db")
    ap.add_argument("--policy", default="config/policy.yaml")
    ap.add_argument("--iters", type=int, default=10)
    ap.add_argument("--tasks", default="general_chat,code_edit,extraction,hard_reasoning")
    args = ap.parse_args()

    from mininfer.store import Store
    from mininfer import router

    store = Store(_copy_db(args.db))
    policy, tasks = router.Policy.load(args.policy)

    # Count SQL statements without changing behaviour.
    counts = collections.Counter()
    raw_execute = store.conn.execute

    def counting_execute(sql, *a, **k):
        counts[" ".join(str(sql).split())[:60]] += 1
        return raw_execute(sql, *a, **k)

    store.conn.execute = counting_execute

    print(f"registry: {len(store.deployments())} deployments\n")
    print(f"{'task':16s} {'mean ms':>8s} {'p50 ms':>7s} {'queries':>8s} {'cands':>6s}")
    grand = []
    for task_name in [t.strip() for t in args.tasks.split(",") if t.strip()]:
        task = tasks.get(task_name)
        if task is None:
            continue
        router.build_candidates(store, task, policy)  # warm caches
        times, queries, n = [], [], 0
        for _ in range(args.iters):
            counts.clear()
            t0 = time.perf_counter()
            cands = router.build_candidates(store, task, policy)
            times.append((time.perf_counter() - t0) * 1000.0)
            queries.append(sum(counts.values()))
            n = len(cands)
        grand.append(statistics.fmean(times))
        print(f"{task_name:16s} {statistics.fmean(times):8.1f} "
              f"{statistics.median(times):7.1f} {statistics.fmean(queries):8.0f} {n:6d}")

    # The single biggest contributor, isolated.
    deploy_times = []
    for _ in range(args.iters):
        t0 = time.perf_counter()
        store.deployments()
        deploy_times.append((time.perf_counter() - t0) * 1000.0)
    print(f"\n  store.deployments(): {statistics.fmean(deploy_times):.1f} ms")
    if grand:
        print(f"  build_candidates avg: {statistics.fmean(grand):.1f} ms")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
