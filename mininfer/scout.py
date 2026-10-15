"""Model scout — the agentic "form guide" builder.

For every free deployment in the registry this gathers what we already know
(price, status, uptime, capabilities, leaderboards, call outcomes), runs a web
search for current context, and has an LLM write a structured dossier: the arm's
core competency, its strengths and weaknesses, and — the part that makes it a
form guide — *when it should be picked* for a task.

The search is TinyFish by default (`MI_SCOUT_SEARCH_PROVIDER` to override): its
structured JSON is the most reliable evidence for agent research, and the scout
prefers an ungrounded dossier over a thinly-sourced one. With no
`TINYFISH_API_KEY` the search is skipped, not swapped for a different tier.

The output is the `model_dossiers` table, read by the dashboard's form guide and
`/v1/dossiers` as a real-time helper for choosing the right model for a task:
the horse for the race. It is a helper, not a judge — the router's ranking is
unchanged; a dossier tells a human (or a future agent) *why* a horse exists, not
which one to bet on.

Keeps itself current on the cheap: `scout()` skips arms whose dossier is younger
than `max_age_hours`, and `mi scout --interval` loops the same pass forever — the
worker/verify shape the rest of the project already uses.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Callable

import re

from . import search as search_mod
from .fetch import utcnow
from .dossier_audit import build_pools, sound_best_for
from .leaderboards import describe
from .store import Store

# Which arm writes dossiers is resolved by `agent_llm.agent_models`:
# `--agent-model` > `MI_AGENT_MODEL` > `MI_AGENT_MODELS` > a free-first default
# chain with a paid last resort. Scout passes `None` through when nothing is
# pinned, so the chain (and its fallback) actually applies — defaulting here to a
# single constant silently disabled `MI_AGENT_MODELS`.

_SEARCH_LIMIT = 3
#: How much of a search-result snippet to keep. The provider returns ~140
#: characters of its own, so this rarely binds — it is a guard, not a trimmer.
_SNIPPET_CHARS = 400


def _parse_json_field(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _leaderboard_tags(benchmark: dict) -> list[dict]:
    out = []
    for key, value in (benchmark or {}).items():
        b = describe(key)
        out.append({"key": key, "label": b.label, "source": b.source,
                    "value": value, "url": b.url})
    return out


def _facts(deploy: dict, outcomes: dict | None, tags: list[dict]) -> dict:
    """The registry-side facts an arm's dossier is written from.

    Every field here is what the registry already believes, so a dossier can be
    re-checked later against `facts` — a claim is only as good as the evidence it
    was synthesised from, and the evidence is snapshotted here.
    """
    return {
        "deploy_id": deploy["deploy_id"],
        "weights_id": deploy["weights_id"],
        "display_name": deploy["display_name"],
        "provider": deploy["provider"],
        "provider_model_id": deploy["provider_model_id"],
        "family": deploy.get("family"),
        "params_b": deploy.get("params_b"),
        "arch": deploy.get("arch"),
        "modalities": _parse_json_field(deploy.get("modalities"), []),
        "context_window": deploy.get("context_window"),
        "max_output": deploy.get("max_output"),
        "price_in": deploy.get("price_in"),
        "price_out": deploy.get("price_out"),
        "free_markers": {
            "zero_price": bool(deploy.get("zero_price")),
            "free_variant": bool(deploy.get("free_variant")),
            "trial_credits": bool(deploy.get("trial_credits")),
            "subscription": bool(deploy.get("subscription")),
        },
        "status": deploy.get("status"),
        "status_reason": deploy.get("status_reason"),
        "pricing_state": deploy.get("pricing_state"),
        # The arm's capabilities, and how much to trust them. Without these the
        # dossier is written blind to what the model can *do*, and recommends it for
        # tasks the router will then refuse: measured on the first five real
        # dossiers, 5 of 13 recommended (arm, task) pairs were not servable, and one
        # of them named `extraction` for a model whose own row said
        # `structured: false`. `_prompt` already told the writer not to contradict
        # the facts; it simply was not given the ones that matter most here.
        "caps": _parse_json_field(deploy.get("caps"), {}),
        "caps_state": _parse_json_field(deploy.get("caps_state"), {}),
        "uptime_1d": deploy.get("uptime_1d"),
        "first_token_ms": deploy.get("first_token_ms"),
        "throughput": deploy.get("throughput"),
        "leaderboards": tags,
        "outcomes": outcomes,
    }


def _search_query(deploy: dict) -> str:
    name = (deploy.get("display_name") or deploy["deploy_id"]).strip()
    return f'"{name}" model capabilities what is it good at benchmarks'


def _search(store: Store, deploy: dict, enabled: bool) -> tuple[list[dict], dict]:
    """Free-first web search for current context about one arm.

    Returns `(sources, meta)`: the sources are what a reader can follow to verify
    a dossier claim; `meta` carries provider/cost/snapshot for provenance. A
    search failure is reported, never fatal — the registry facts alone are enough
    to write a weaker dossier.
    """
    if not enabled:
        return [], {"enabled": False}
    # TinyFish only, by default. It returns structured JSON with URLs, titles and
    # snippets, and is the most reliable source for agent research — so the
    # scout does not silently fall back to thinner scraped tiers. With no
    # `TINYFISH_API_KEY` the search is skipped and the dossier is written from
    # registry facts alone (recorded in `facts.search.error`). Override with
    # `MI_SCOUT_SEARCH_PROVIDER` when a different provider is preferred.
    provider = (os.environ.get("MI_SCOUT_SEARCH_PROVIDER") or "tinyfish").strip().lower()
    try:
        rep = search_mod.search(_search_query(deploy), provider=provider,
                                limit=_SEARCH_LIMIT, store=store)
    except Exception as exc:  # network / TLS / provider refusal
        return [], {"error": f"{type(exc).__name__}: {exc}"}
    sources = []
    for r in rep.results:
        sources.append({
            "title": (r.get("title") or "").strip(),
            "url": (r.get("url") or "").strip(),
            "snippet": (r.get("snippet") or "")[:_SNIPPET_CHARS],
        })
    return sources, {
        "enabled": True,
        "query": rep.query,
        "provider": rep.provider,
        "snapshot": rep.snapshot,
        "cost_usd": rep.cost_usd,
        "error": rep.error,
    }


def _prompt(facts: dict, sources: list[dict], task_names: list[str] | None) -> str:
    tasks = ", ".join(task_names) if task_names else "(task list not supplied)"
    return f"""You are the model scout for min(Infer), an LLM router. Write a "form guide" \
dossier for ONE free model so an operator can decide which task to pick it for — \
the way a racing form helps pick a horse for a race.

Known tasks in this deployment: {tasks}

Registry facts (the source of truth — do not contradict these):
{json.dumps(facts, indent=2, default=str)}

Fresh web-search context (snippets, may be marketing — treat as claims, not facts):
{json.dumps(sources, indent=2, default=str)}

Reply with exactly one JSON object, no prose, no markdown fences:

{{
  "core_competency": "one sentence: what this model is genuinely best at",
  "summary": "2-3 sentences of background: family, size, what it is, what it is not",
  "strengths": ["..."],
  "weaknesses": ["..."],
  "when_to_use": ["task name or concrete scenario"],
  "when_not_to_use": ["scenario"],
  "best_for": ["1-3 task names from the list above, or 'none' if the list does not fit"],
  "confidence": 0.0
}}

Rules:
- Everything you write is read by an operator, and `best_for` is read by the router.
  There is no decorative field: every sentence must be something the facts or the
  sources actually support.
- **`best_for` and `when_to_use` must only name tasks this arm can actually serve.**
  Read `caps` against the task list: a task requiring `tools` cannot be recommended
  for an arm whose `tools` is not `true`, and one requiring `structured` cannot be
  recommended for an arm whose `structured` is not `true`. `null` means *nobody has
  reported it*, so it is not a yes. A recommendation the router then refuses is worse
  than no recommendation — it reads as a considered judgement and is not one.
- `when_to_use` and `best_for` must use the task names given above where possible.
- Never assert a benchmark, a price, or a capability the registry facts do not show.
  The leaderboards above are the whole of what is known about its standing — do not
  invent a score, and do not imply one it is not on.
- `confidence` is your own confidence in the dossier, 0.0-1.0: lower it when the
  search context is thin or the registry facts are sparse.
- If a fact is unknown, say so in `summary` rather than guessing.
"""


def _parse_json(s: str) -> dict | list | None:
    """Same tolerant JSON extraction as `agent_ingest._parse_json`, copied so this
    module imports without the `[agents]` extra (which pulls in bs4/LangGraph).
    """
    t = (s or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S | re.I)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _synthesize(agent_model: str | None, prompt: str) -> dict:
    """Run the LLM and normalise whatever JSON it returned into dossier fields."""
    try:
        from .agent_ingest import _agent_llm
    except ImportError as exc:  # beautifulsoup4 / langgraph / langchain-openai
        raise RuntimeError(
            "the agent runtime is not installed — `pip install -e '.[agents]'`"
        ) from exc
    llm = _agent_llm(agent_model)
    resp = llm.invoke(prompt)
    content = resp.content if hasattr(resp, "content") else str(resp)
    data = _parse_json(content)
    if not isinstance(data, dict):
        raise ValueError("agent did not return a JSON object")

    def _list(key: str) -> list[str]:
        v = data.get(key) or []
        return v if isinstance(v, list) else [v]

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        # Which arm in the fallback chain actually answered — provenance for the
        # row, so a dossier is not "written by nobody".
        "_agent_model": getattr(resp, "agent_model", ""),
        "core_competency": str(data.get("core_competency") or "").strip(),
        "summary": str(data.get("summary") or "").strip(),
        "strengths": [str(x) for x in _list("strengths")],
        "weaknesses": [str(x) for x in _list("weaknesses")],
        "when_to_use": [str(x) for x in _list("when_to_use")],
        "when_not_to_use": [str(x) for x in _list("when_not_to_use")],
        "best_for": [str(x) for x in _list("best_for")],
        "confidence": max(0.0, min(1.0, confidence)),
    }


def _facts_changed(deploy: dict, existing: dict) -> bool:
    """True when the registry's view of an arm differs from what its dossier was
    written from — the signal that a dossier needs a re-write even if young."""
    stored = _parse_json_field(existing.get("facts"), {})
    return (stored.get("status") != deploy.get("status")
            or stored.get("pricing_state") != deploy.get("pricing_state")
            or stored.get("price_out") != deploy.get("price_out"))


def _need_search(facts: dict, existing: dict | None) -> tuple[bool, str]:
    """Decompose whether a fresh web search is worth its cost for this arm.

    The search-necessity gate: deterministic and auditable, so a dossier records
    *why* it searched or did not. Search is spent only when the registry facts
    cannot carry the write alone — a first dossier, a changed price/status, or
    an arm with no benchmark and no outcome history. An arm with fresh facts and
    evidence is re-summarised from the snapshot instead of re-searching, which is
    what keeps the daily free allowance from being burned on unchanged horses.
    """
    if existing is None:
        return True, "first_dossier"
    stored = _parse_json_field(existing.get("facts"), {})
    if (stored.get("status") != facts.get("status")
            or stored.get("pricing_state") != facts.get("pricing_state")):
        return True, "facts_changed"
    if not facts.get("leaderboards") and not facts.get("outcomes"):
        return True, "sparse_evidence"
    return False, "facts_sufficient"


def scout_one(store: Store, deploy: dict, *, agent_model: str | None = None,
              search_enabled: bool = True, task_names: list[str] | None = None,
              dry_run: bool = False, existing: dict | None = None,
              tasks: dict | None = None, pools: dict | None = None) -> dict:
    """Write one arm's dossier. Returns the dossier dict, with `error` on failure."""
    try:
        benchmark = _parse_json_field(deploy.get("benchmark"), {})
        outcomes = store.outcome_stats(deploy["deploy_id"])
        facts = _facts(deploy, outcomes, _leaderboard_tags(benchmark))
        # Leaderboards are a first-class field, not just a line inside `facts`: a
        # reader wants them without parsing a provenance blob. They were already
        # computed here and then buried.
        leaderboards = _leaderboard_tags(benchmark)
        # Decompose before spending: no search when the facts suffice, and the
        # decision is recorded so a reader can see why the arm was (not) searched.
        if not search_enabled:
            sources, search_meta = [], {"skipped": "search_disabled"}
        else:
            need, why = _need_search(facts, existing)
            if need:
                sources, search_meta = _search(store, deploy, True)
            else:
                sources, search_meta = [], {"skipped": why}
        facts["search"] = search_meta
        dossier = _synthesize(agent_model, _prompt(facts, sources, task_names))
        facts["agent_model"] = dossier.get("_agent_model") or ""
        # Enforce, do not request. The prompt states the eligibility rule and the
        # writer does not reliably follow it, so a stored dossier is made sound here
        # instead. What was dropped is recorded, because silently shortening a list
        # would hide that the writer overreached.
        if tasks is not None and pools is not None:
            kept, dropped = sound_best_for(deploy["deploy_id"],
                                           dossier.get("best_for"), tasks, pools)
            dossier["best_for"] = kept
            if dropped:
                facts["dropped_best_for"] = dropped
    except Exception as exc:  # no key / bad agent output / search failure
        return {"deploy_id": deploy["deploy_id"], "error": str(exc)}

    dossier.update({
        "deploy_id": deploy["deploy_id"],
        "weights_id": deploy["weights_id"],
        "display_name": deploy["display_name"],
        "provider": deploy["provider"],
        "price_out": deploy.get("price_out"),
        "search_sources": sources,
        "facts": facts,
        "generated_at": utcnow(),
    })
    if not dry_run:
        store.upsert_dossier(
            deploy["deploy_id"],
            weights_id=deploy["weights_id"],
            display_name=deploy["display_name"],
            provider=deploy["provider"],
            price_out=deploy.get("price_out"),
            core_competency=dossier["core_competency"],
            summary=dossier["summary"],
            leaderboards=leaderboards,
            strengths=dossier["strengths"],
            weaknesses=dossier["weaknesses"],
            when_to_use=dossier["when_to_use"],
            when_not_to_use=dossier["when_not_to_use"],
            best_for=dossier["best_for"],
            search_sources=sources,
            facts=facts,
            confidence=dossier["confidence"],
            generated_at=dossier["generated_at"],
        )
    return dossier


def _age_hours(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        stamp = dt.datetime.fromisoformat(iso)
    except ValueError:
        return float("inf")
    now = dt.datetime.now(dt.timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return (now - stamp).total_seconds() / 3600.0


def scout(store: Store, *, limit: int = 0, agent_model: str | None = None,
          search_enabled: bool = True, force: bool = False,
          max_age_hours: float = 24.0, dry_run: bool = False,
          only_new: bool = False, task_names: list[str] | None = None,
          on_arm: Callable[[dict], None] | None = None,
          on_skip: Callable[[dict], None] | None = None) -> dict:
    """The pass: refresh every stale free arm's dossier, newest facts first.

    Skips arms whose dossier is still fresh *and* whose registry facts have not
    changed since it was written — that is what makes `mi scout --interval` a
    cheap "keeps checking" loop rather than a full re-write every cycle.

    `only_new` limits the pass to free arms with no dossier at all, the
    "new free model" event queue fed by ingest.

    `on_arm` fires once per arm that is processed; `on_skip` fires once per arm
    left alone. Both are progress hooks: `on_arm` alone cannot report a total,
    because an arm that is skipped never reaches it, so a caller showing
    "n of m" would watch the bar stop short on a healthy pass.
    """
    summary: dict[str, Any] = {"scanned": 0, "written": 0, "skipped_fresh": 0,
                               "errors": 0, "dossiers": [], "dropped_best_for": 0}
    # Built once for the pass: eligibility is a property of the task, not of the arm.
    _, _tasks, _pools = build_pools(store)
    arms = store.new_dossier_arms() if only_new else store.free_deployments()
    for deploy in arms:
        if limit and summary["scanned"] >= int(limit):
            break
        summary["scanned"] += 1

        existing = store.dossier(deploy["deploy_id"]) if not force else None
        # One reason a dossier may be stale beyond its age: the registry's view of the
        # arm moved (`_facts_changed`). It is then re-synthesised, and `_need_search`
        # decides whether that costs a search.
        dirty = existing is not None and _facts_changed(deploy, existing)
        if (existing is not None and not dirty
                and _age_hours(existing.get("generated_at")) < max_age_hours):
            summary["skipped_fresh"] += 1
            if on_skip:
                on_skip({"deploy_id": deploy["deploy_id"], "reason": "fresh"})
            continue

        dossier = scout_one(store, deploy, agent_model=agent_model,
                            search_enabled=search_enabled,
                            task_names=task_names, dry_run=dry_run,
                            existing=existing, tasks=_tasks, pools=_pools)
        if dossier.get("error"):
            summary["errors"] += 1
        else:
            summary["written"] += 1
        summary["dossiers"].append(dossier)
        if on_arm:
            on_arm(dossier)
    return summary
