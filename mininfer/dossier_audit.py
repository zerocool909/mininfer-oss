"""Does a dossier make sense? The falsifiable test, as code.

A dossier's only routing-relevant field is `best_for`, and the router will refuse an
arm it is not eligible for. So the strongest check available is also the simplest:

    every `best_for` entry must name a task this arm is *eligible* for.

That one criterion subsumes the others worth having. It catches a `best_for` naming a
task that does not exist, a claim contradicting a registry fact (`shelf_image_audit`
on an arm with no image input), and a capability guessed from a web snippet rather
than reported — because eligibility is decided by the same `require`, modality,
context and price gates the router applies.

It was worth writing because the first five real dossiers failed it: **5 of 13
recommended (arm, task) pairs were not servable**, one of them naming `extraction`
for a model whose own row said `structured: false`. Prose in a prompt was not
enough; a check is.

Deliberately **not** criteria: `confidence` (self-reported until ticket 19 derives
it, so it cannot be right or wrong yet), and source count (a thin dossier is
allowed, as long as it does not overreach — which the eligibility check enforces).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .router import Policy, build_candidates
from .store import Store


def _as_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        got = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(x) for x in got] if isinstance(got, list) else []


def audit_dossiers(store: Store, policy_path: str | Path = "config/policy.yaml",
                   *, require_callable: bool = False) -> dict[str, Any]:
    """Every dossier recommendation the router would refuse.

    `require_callable` defaults to False: a dossier describes *fit*, and whether this
    machine happens to hold a provider's key is a deployment fact, not a judgement
    about the model. Leaving it on would fail a dossier for a missing credential.
    """
    policy, tasks = Policy.load(policy_path)
    policy.require_callable = require_callable

    # One candidate pool per task, built once: the audit is O(tasks), not O(dossiers).
    pools: dict[str, dict[str, str | None]] = {}
    for name, task in tasks.items():
        pools[name] = {c.deploy_id: c.rejected
                       for c in build_candidates(store, task, policy)}

    findings: list[dict[str, Any]] = []
    checked = 0
    # Through the `Store`, not raw SQL: `Store` owns the schema and
    # `tests/test_encapsulation.py` holds that line.
    rows = store.dossiers()
    for r in rows:
        for task_name in _as_list(r["best_for"]):
            checked += 1
            if task_name not in tasks:
                findings.append({"deploy_id": r["deploy_id"], "task": task_name,
                                 "reason": "not a task in this policy",
                                 "generated_at": r["generated_at"]})
                continue
            refusal = pools[task_name].get(r["deploy_id"])
            if refusal is None and r["deploy_id"] not in pools[task_name]:
                findings.append({"deploy_id": r["deploy_id"], "task": task_name,
                                 "reason": "not in this task's candidate pool",
                                 "generated_at": r["generated_at"]})
            elif refusal:
                findings.append({"deploy_id": r["deploy_id"], "task": task_name,
                                 "reason": refusal, "generated_at": r["generated_at"]})
    return {"dossiers": len(rows), "checked": checked,
            "unsound": len(findings), "findings": findings}


def format_audit(report: dict[str, Any]) -> str:
    if not report["dossiers"]:
        return "no dossiers to audit"
    lines = [f"dossiers {report['dossiers']}  ·  recommendations {report['checked']}  ·  "
             f"unsound {report['unsound']}"]
    if not report["unsound"]:
        lines.append("  every recommendation is a task its arm is eligible for")
        return "\n".join(lines)
    for f in report["findings"]:
        lines.append(f"  {f['deploy_id'][:46]:46s} {f['task']:18s} {f['reason']}")
    return "\n".join(lines)


def build_pools(store: Store, policy_path: str | Path = "config/policy.yaml",
                *, require_callable: bool = False):
    """One candidate pool per task. Built once per scout pass, not per arm.

    Returned alongside the policy so a caller can reuse both without re-reading the
    file or rebuilding a pool it already has.
    """
    policy, tasks = Policy.load(policy_path)
    policy.require_callable = require_callable
    pools = {name: {c.deploy_id: c.rejected
                    for c in build_candidates(store, task, policy)}
             for name, task in tasks.items()}
    return policy, tasks, pools


def sound_best_for(deploy_id: str, best_for: Any, tasks, pools) -> tuple[list[str], list[dict]]:
    """Drop every recommendation this arm cannot serve. Returns (kept, dropped).

    Enforced rather than requested. The prompt states the rule and the writer does not
    reliably follow it: after adding `caps` to the facts and the rule to the prompt,
    a rescout still produced 4 unsound recommendations out of 14 — including
    `shelf_image_audit` for a model whose own row says `structured: false`.

    So a *stored* dossier is sound by construction. An ineligible entry is a
    prediction the router will contradict, and a form guide that recommends tasks its
    arms cannot serve is worse than a shorter one.
    """
    kept: list[str] = []
    dropped: list[dict] = []
    for task_name in _as_list(best_for):
        if task_name not in tasks:
            dropped.append({"task": task_name, "reason": "not a task in this policy"})
            continue
        pool = pools.get(task_name, {})
        refusal = pool.get(deploy_id)
        if refusal is None and deploy_id not in pool:
            dropped.append({"task": task_name,
                            "reason": "not in this task's candidate pool"})
            continue
        if refusal:
            dropped.append({"task": task_name, "reason": refusal})
            continue
        kept.append(task_name)
    return kept, dropped
