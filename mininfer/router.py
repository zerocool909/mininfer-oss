"""Routing: constraint-first, score-second.

Order of operations:

    1. HARD FILTER   capability / context / status / price ceiling. Nothing below
                      this line can be recovered by a good score.
    2. p(success)    Wilson lower bound, seeded with a benchmark prior so a cold
                      arm is uncertain rather than assumed bad.
    3. COST          effective marginal cost *right now* — a free arm whose bucket
                      is empty costs the price of the next option, not zero.
    4. RANK          by cost-per-success, tie-broken by quality then latency.
    5. DIVERSIFY     #2 and #3 must not share a provider family, because 429s and
                      outages are correlated by gateway, not by model.

There is deliberately no weighted-sum formula anywhere. A linear blend lets a
cheap mediocre model beat a good one at the moment you specifically needed the
good one; hard thresholds plus a cost-per-success objective does not.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
from dataclasses import dataclass, field
from typing import Any

from . import cache as cache_mod
from . import leaderboards as lb
from . import probe as probe_mod
from .bandit import bandit_order
from .execute import available_providers
from .schema import Candidate, TaskProfile
from .store import Store

# --------------------------------------------------------------------------- #
# statistics & caching
# --------------------------------------------------------------------------- #

# The norms cache: in-process by default, shared through Redis when
# `MI_REDIS_URL` is set. It is resolved per call so the URL takes effect without
# a restart, and cached by URL so the Redis client is built once — the same
# pattern `proxy._limiter()` uses for the rate limiter.
_NORMS_CACHE: cache_mod.TTLCache = cache_mod.InProcessCache()
_NORMS_CACHE_URL: str | None = None
_NORMS_TTL = 120.0


def _norms_cache() -> cache_mod.TTLCache:
    global _NORMS_CACHE, _NORMS_CACHE_URL
    url = os.environ.get("MI_REDIS_URL") or ""
    if url != _NORMS_CACHE_URL:
        _NORMS_CACHE = cache_mod.make_cache(url)
        _NORMS_CACHE_URL = url
    return _NORMS_CACHE


def wilson_lb(wins: float, n: float, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval.

    With n=3 a point estimate is meaningless; this is why the router never uses
    raw success rate. `wins` may be fractional — that's how priors enter.
    """
    if n <= 0:
        return 0.0
    p = wins / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / d)


def _pct(sorted_vals: list[float], frac: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(frac * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def benchmark_norms(store: Store) -> dict[str, tuple[float, float]]:
    """Per-key p5/p95 across the registry, so priors are scale-agnostic.

    Benchmark scales are arbitrary (AA intelligence, arena Elo, pass@1). Mapping
    to a percentile band means a new benchmark source needs no unit conversion.

    Cached for 120 seconds to eliminate repeated full-table scans. With
    `MI_REDIS_URL` set the cached band is shared by every replica, so N replicas
    do one scan per window instead of N. Values cross the cache as JSON lists
    and are re-typed to tuples on read.
    """
    key = f"norms:{getattr(store, 'target', '')}"   # path or DSN; the cache key either way
    cached = _norms_cache().get(key)
    if cached is not None:
        return {k: (float(v[0]), float(v[1])) for k, v in cached.items()}

    buckets: dict[str, list[float]] = {}
    for data in store.all_benchmarks():
        for k, v in (data or {}).items():
            try:
                buckets.setdefault(k, []).append(float(v))
            except (TypeError, ValueError):
                continue
    out: dict[str, tuple[float, float]] = {}
    for k, vals in buckets.items():
        vals.sort()
        out[k] = (_pct(vals, 0.05), _pct(vals, 0.95))
    _norms_cache().set(key, {k: [lo, hi] for k, (lo, hi) in out.items()}, _NORMS_TTL)
    return out


def prior_success(benchmark: dict[str, float], keys: tuple[str, ...],
                  norms: dict[str, tuple[float, float]],
                  *, weights: dict[str, float] | None = None,
                  floor: float = 0.30, span: float = 0.65,
                  ) -> tuple[float, str, list[str]]:
    """Blend external leaderboard evidence into a prior success probability.

    Every matching key contributes its own percentile position, weighted by
    `weights` (default 1.0) and averaged. Two reasons this beats first-match:
    one leaderboard should not be trusted alone, and taking only the first match
    silently ignored every other source as soon as one was present.

    Returns `(prior, lead_key, contributing_keys)`; `lead_key` is the
    highest-weight contributor, kept as the short display label.
    """
    acc = total = 0.0
    used: list[tuple[str, float]] = []
    for key in keys:
        if key not in benchmark:
            continue
        lo, hi = norms.get(key, (0.0, 0.0))
        q = 0.5 if hi <= lo else (float(benchmark[key]) - lo) / (hi - lo)
        q = min(1.0, max(0.0, q))
        w = float((weights or {}).get(key, 1.0))
        if w <= 0:
            continue
        acc += w * q
        total += w
        used.append((key, w))
    if not used:
        return 0.5, "none", []
    q = acc / total
    lead = max(used, key=lambda kv: kv[1])[0]
    return min(0.97, max(0.05, floor + span * q)), lead, [k for k, _ in used]


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Policy:
    name: str = "free_first"
    top_k: int = 3
    require_distinct_provider: bool = True
    require: dict[str, bool] = field(default_factory=dict)
    min_context: int = 0
    min_success_lb: float = 0.0
    # Buffer a short structured *streamed* answer and check it before emitting it,
    # so an arm that answered 200 with garbage is treated as failed and the next
    # candidate gets the request. Off by default: it costs the streaming UX.
    stream_verify: dict = field(default_factory=dict)
    # What to do when a source never reported a required capability. 'reject' is
    # safe and is the default; 'allow' trades correctness for coverage, which is
    # only reasonable when the caller can retry after a validation failure.
    on_unverified_capability: str = "reject"  # reject | allow
    max_cost_in: float | None = None
    max_cost_out: float | None = None
    max_cost_per_success: float | None = None
    allow_unknown_price: bool = False
    allow_subscription: bool = True
    # Whether a `subscription=True` deployment is treated as zero marginal cost.
    # Off by default: a monthly plan is a sunk cost, not a free tier, so pricing
    # it at zero makes a paid arm look free. Turn on only when the operator
    # actually pays for that subscription.
    subscription_is_free: bool = False
    # Hard ceiling on the tokens one session may spend before the proxy refuses
    # further calls for it. Enforced server-side because that is the only place
    # it can be: a client counter cannot stop anything, and it drifts on refresh,
    # retry and regenerate. 0 disables the cap. Overridable per deployment with
    # MI_SESSION_TOKEN_LIMIT for a stricter shared deployment.
    session_token_limit: int = 1_000_000
    # Hard ceiling on *money* one session may spend, in USD. Separate from the
    # token cap because the two bound different things: a token cap cannot see
    # tool spend at all, and a search costs ~$0.008 while a routed call on a free
    # arm costs $0 — so a token-only budget bounds the cheap half of a request and
    # ignores the expensive one. 0 disables it. Overridable with
    # MI_SESSION_COST_LIMIT.
    session_cost_limit: float = 0.0
    # A deployment whose provider we hold no key for cannot be called, so it is
    # not a candidate however cheap it looks. Default False keeps `Policy()` pure
    # for callers that only want the theoretical ranking; config/policy.yaml
    # turns it on so the shipped router only offers arms it can actually reach.
    require_callable: bool = False
    # A free arm may be *tried* even without benchmark evidence: being wrong costs
    # a retry, not money, and the outcome becomes the evidence. Hard constraints
    # (capability, context) still apply — those are knowable in advance and a free
    # model that cannot emit structured output fails every time, not just once.
    # Off by default so `Policy()` is conservative.
    free_floor_exempt: bool = False
    # How long that exemption lasts. A free arm gets a bounded trial: after this
    # many observations it is held to the quality floor like any other arm, so an
    # arm that keeps failing stops winning on price alone.
    free_trial_obs: int = 3
    # How many untried arms one shadow request trials *and judges together*. The
    # provider calls cannot be batched (different providers), but the judging is a
    # single model call for the whole batch — so trialling three arms costs three
    # provider calls and **one** judge call instead of three. A larger batch learns
    # more per request and pays less to judge it; a smaller one spends less on
    # trials nobody asked for.
    judge_batch_size: int = 3
    objective: str = "cost_per_success"  # | quality | latency | bandit
    prior_strength: float = 6.0
    # Ceiling on the weight the task-level posterior carries when it is used as the
    # prior for a difficulty cell. Without it the cell is drowned by total traffic
    # (`prior_strength + n_obs` grows without bound). Placeholder pending calibration.
    effort_shrink_k: float = 50.0
    # free arm with no configured quota: how much do we discount its success
    # probability? Not a calibrated number — a conservative haircut.
    unknown_headroom_factor: float = 0.90
    exploration_eps: float = 0.0
    # A deliberate exploration pick may not exceed the best deterministic arm's
    # cost by more than this fraction. 0.0 keeps exploration to free/cheapest arms.
    exploration_cost_slack: float = 0.0
    # UCB bonus for the *primary* pick: a coefficient on `sqrt(ln(N+1)/(n+1))`
    # added to p(success) before ranking. 0 keeps the pick purely deterministic.
    #
    # It can never cross a cost tier — cost-per-success is compared first — so it
    # only decides between arms that cost the same. That is exactly where the
    # "one free arm answers everything" problem lived: every free arm scored
    # cost-per-success 0, so the ranking collapsed to the benchmark prior and the
    # single best-prior arm took every request, at the cost of never testing the
    # other 56 free arms, concentrating load on one quota, and inheriting that
    # provider's outages unhedged.
    explore_c: float = 0.0
    # How far below the best p(success) an arm may sit and still receive the
    # exploration bonus. Without this the optimism term promoted *unknown* arms
    # (p_lb at the 0.30 floor) alongside genuinely good ones — the point is to
    # test the near-misses, not the dregs.
    explore_band: float = 0.25
    # Anti-fixation: maximum traffic share (0.0 to 1.0) any single arm may take
    # within a rolling window before traffic is distributed to the next capable arm.
    max_model_share_pct: float = 0.60
    # Maximum time in hours a manually pinned model remains active before expiring
    pin_ttl_hours: float = 24.0
    z: float = 1.96
    # Complexity estimation and reasoning-floor configuration
    complexity: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | pathlib.Path) -> tuple["Policy", dict[str, TaskProfile]]:
        import copy
        import yaml

        p_obj = pathlib.Path(path)
        resolved_str = str(p_obj.resolve())
        mtime = p_obj.stat().st_mtime if p_obj.exists() else 0.0
        cached = _POLICY_CACHE.get(resolved_str)
        if cached and cached[0] == mtime:
            # Return fresh copies so mutators don't poison cache
            return copy.deepcopy(cached[1]), copy.deepcopy(cached[2])

        raw = yaml.safe_load(p_obj.read_text()) or {}
        p = raw.get("policy", {})
        pol = cls(**{k: v for k, v in p.items() if k in cls.__dataclass_fields__})
        tasks: dict[str, TaskProfile] = {}
        for name, t in (raw.get("tasks") or {}).items():
            tasks[name] = TaskProfile(
                name=name,
                tokens_in=int(t.get("tokens_in", 1000)),
                tokens_out=int(t.get("tokens_out", 400)),
                require=dict(t.get("require") or {}),
                min_context=int(t.get("min_context", 0)),
                min_success_lb=float(t.get("min_success_lb", pol.min_success_lb)),
                benchmark_keys=tuple(t.get("benchmark_keys") or ()),
                benchmark_weights=dict(t.get("benchmark_weights") or ()),
                cues=tuple(t.get("cues") or ()),
                description=t.get("description", ""),
                on_unverified_capability=t.get("on_unverified_capability"),
            )
        _POLICY_CACHE[resolved_str] = (mtime, copy.deepcopy(pol), copy.deepcopy(tasks))
        return pol, tasks


_POLICY_CACHE: dict[str, tuple[float, Policy, dict[str, TaskProfile]]] = {}


# --------------------------------------------------------------------------- #
# candidate scoring
# --------------------------------------------------------------------------- #


# Gateways aggregate upstreams. A 429, a rate-limit change or an outage hits
# every endpoint behind that gateway, and several gateways resell the same
# upstream — so "distinct provider" needs both levels, or fallbacks are fake.
GATEWAYS = {"openrouter", "hf", "vercel"}

# The task a prompt falls back to when no cue matches at all. Defined once and
# imported, because it used to be spelled out in two files with the same default
# and changing one would silently desynchronise the proxy from the CLI.
#
# `general_chat`, not `code_edit`: when nothing matched, the prompt is by
# definition not obviously a coding task, and `code_edit` demands 32K of context
# and a 0.55 quality floor — so "Hello, how are you?" was routed as code.
DEFAULT_TASK = os.environ.get("MI_DEFAULT_TASK", "general_chat")

# Gateway-embedded upstreams that are really the same place as a direct provider.
_UPSTREAM_CANON = {
    "google-ai-studio": "google",
    "google-vertex": "google",
    "google-vertex/global": "google",
    "google-vertex/europe": "google",
    "amazon-bedrock": "amazon",
    "claude-on-aws": "anthropic",
    "azure": "azure",
    "azure/us": "azure",
    "deepinfra": "deepinfra",
    "fireworks-ai": "fireworks",
    "moonshotai": "moonshot",
    "zai-org": "zhipu",
}


def provider_axes(provider: str) -> tuple[str, str]:
    """`openrouter/azure/us` -> gateway 'openrouter', upstream 'azure'.

    `deepinfra` and `hf/deepinfra` must resolve to the same upstream, otherwise
    they get chosen as "independent" fallbacks while sharing a single point of
    failure — which is exactly the failure mode diversity is meant to prevent.
    """
    if provider in GATEWAYS:
        return provider, provider
    head, _, rest = provider.partition("/")
    if head in GATEWAYS and rest:
        upstream = rest.split("/")[0]
        return head, _UPSTREAM_CANON.get(rest, _UPSTREAM_CANON.get(upstream, upstream))
    return provider, _UPSTREAM_CANON.get(provider, provider)


def vendor_of(provider_model_id: str | None, upstream: str) -> str:
    """Who *made* the model, as distinct from who serves it.

    `google/gemini-3.8-flash` -> 'google'. The model id is the only place this is
    recorded: the `provider` column holds the gateway, so every OpenRouter row
    resolves to upstream 'openrouter' and a vendor axis built from it would be
    invisible (or worse, identical for all 400 rows). Falls back to the upstream
    when the id carries no namespace.
    """
    head, sep, rest = (provider_model_id or "").partition("/")
    if sep and rest:
        return _UPSTREAM_CANON.get(head, head)
    return upstream


def _free_kind(d: dict, policy: Policy) -> str | None:
    """Why this arm's marginal cost is zero *right now*, if it is.

    A subscription is deliberately excluded unless `subscription_is_free` says
    otherwise. "Marginal cost ~0" is only true for an operator who already pays
    the monthly fee, and the registry cannot know that. Treating a paid plan as
    free let Featherless's 22k subscription models win every comparison on a
    cost nobody was paying. Priced at list, a subscription arm competes on
    merit like any other paid arm.
    """
    if d.get("zero_price"):
        return "zero_price"
    if d.get("free_variant"):
        return "free_variant"
    if d.get("subscription"):
        return "subscription" if policy.subscription_is_free else None
    if d.get("trial_credits"):
        return "trial_credits"
    return None


def build_candidates(store: Store, task: TaskProfile, policy: Policy, *,
                     user_keys: dict[str, str] | None = None,
                     pushed_models: list[str] | None = None,
                     effort: str | None = None) -> list[Candidate]:
    stats = store.stats(task.name)
    # Difficulty-scoped evidence, when the caller routed on a difficulty. Empty when
    # it did not, which leaves every posterior below exactly as it was before.
    effort_stats = store.routing_stats_by_effort(task.name, effort) if effort else {}
    rf_stats = store.routing_approval_stats(task.name)
    norms = benchmark_norms(store)
    available = available_providers(user_keys=user_keys)
    # Warm-tier gate: a provider whose most recent probe failed (revoked key,
    # unreachable host) is not callable even though its key is present in the
    # environment. `unhealthy_providers` ignores stale verdicts, so this is a
    # no-op whenever the probe is switched off or has not run recently.
    available -= store.unhealthy_providers(ttl_seconds=probe_mod.HEALTH_TTL)
    if pushed_models is None:
        pushed_models = store.get_pushed_models(task.name, ttl_hours=policy.pin_ttl_hours)
    pushed_set = set(pushed_models or [])
    out: list[Candidate] = []

    total_task_obs = sum(int(s.get("n", 0) or 0) for s in stats.values())

    for d in store.deployments():
        try:
            benchmark = json.loads(d.get("benchmark") or "{}")
        except json.JSONDecodeError:
            benchmark = {}
        caps = json.loads(d.get("caps") or "{}")

        p_prior, prior_key, prior_keys = prior_success(
            benchmark, task.benchmark_keys, norms,
            weights=task.benchmark_weights)
        s = stats.get(d["deploy_id"], {})
        n_obs, wins = int(s.get("n", 0) or 0), int(s.get("wins", 0) or 0)
        # The difficulty cell is this arm's record *at this difficulty only*. It
        # cannot be stacked on top of `stats` — the task-level view already counts
        # those observations — so the task posterior's *mean* becomes the prior and
        # the cell is blended into it. See the branch below for the weight.
        eff_n = eff_wins = 0
        if effort:
            cell = effort_stats.get((d["deploy_id"], task.name, effort)) or {}
            eff_n = int(cell.get("n", 0) or 0)
            eff_wins = int(cell.get("wins", 0) or 0)
        task_n = float(policy.prior_strength + n_obs)
        p_task = (p_prior * policy.prior_strength + wins) / task_n
        if eff_n:
            # Shrinkage strength against a prior no heavier than `effort_shrink_k`.
            # It has to be *capped*: `prior_strength + n_obs` alone grows with total
            # traffic, so on a busy task a perfectly good 50-observation difficulty
            # cell would shift the mean by a fraction of a percent — difficulty
            # learning that only works on quiet tasks is not learning. The cell's
            # rows are still counted once (they are inside `stats`), so this blends
            # rather than stacks.
            k = min(task_n, float(policy.effort_shrink_k))
            cell_rate = eff_wins / eff_n
            p_eff = (eff_n * cell_rate + k * p_task) / (eff_n + k)
            p_lb = wilson_lb(p_eff * task_n, task_n, policy.z)
        else:
            # No cell: byte-for-byte the previous posterior, which is what makes
            # this change safe to land without re-tuning every existing registry.
            p_lb = wilson_lb(p_prior * policy.prior_strength + wins, task_n, policy.z)

        # Human routing decision RLHF: Beta(2, 2) conjugate prior
        rf = rf_stats.get(d["deploy_id"], {})
        approvals = int(rf.get("approvals", 0) or 0)
        disapprovals = int(rf.get("disapprovals", 0) or 0)
        routing_sentiment = (2.0 + approvals) / (4.0 + approvals + disapprovals)
        sentiment_multiplier = 0.5 + routing_sentiment

        free_kind = _free_kind(d, policy)
        headroom, hsrc = store.headroom(d["deploy_id"])
        n_429 = int(s.get("n_429", 0) or 0)
        n_timeout = int(s.get("n_timeout", 0) or 0)
        rate_429 = (n_429 / n_obs) if n_obs else 0.0
        rate_timeout = (n_timeout / n_obs) if n_obs else 0.0
        # Every observation that was not a win. `n - wins` needs no new
        # column, so this works on both engines and on historical rows.
        error_rate = ((n_obs - wins) / n_obs) if n_obs else 0.0
        availability = 1.0
        if d.get("uptime_1d") is not None:
            availability = max(0.0, min(1.0, float(d["uptime_1d"]) / 100.0))
        availability *= max(0.0, 1.0 - rate_429 - rate_timeout)
        if free_kind and headroom is None:
            availability *= policy.unknown_headroom_factor
        p_eff = p_lb * availability * sentiment_multiplier

        pin, pout = d.get("price_in"), d.get("price_out")
        cost_per_call = _cost_per_call(pin, pout, task)
        if free_kind:
            # Marginal cost is zero while the bucket has headroom, and the price
            # of the next-best option once it does not.
            if headroom is None or headroom > 0.05:
                if free_kind in ("subscription", "zero_price", "free_variant", "trial_credits"):
                    cost_per_call = 0.0

        traffic_share = (n_obs / total_task_obs) if total_task_obs > 0 else 0.0
        traffic_capped = bool(total_task_obs >= 5 and traffic_share > policy.max_model_share_pct)

        is_pushed = d["deploy_id"] in pushed_set
        c = Candidate(
            deploy_id=d["deploy_id"],
            provider=d["provider"],
            provider_family=provider_axes(d["provider"])[0],
            upstream=provider_axes(d["provider"])[1],
            vendor=vendor_of(d.get("provider_model_id"),
                             provider_axes(d["provider"])[1]),
            weights_id=d.get("weights_id_resolved") or d["weights_id"],
            display_name=d.get("display_name") or d["provider_model_id"],
            price_in=pin,
            price_out=pout,
            free_kind=free_kind,
            p_prior=p_prior,
            prior_key=prior_key,
            n_obs=n_obs,
            wins=wins,
            effort_n=eff_n,
            effort_wins=eff_wins,
            p_lb=p_lb,
            availability=availability,
            headroom=headroom,
            headroom_src=hsrc,
            cost_per_call=cost_per_call,
            cost_per_success=cost_per_call * task.estimated_calls(p_eff),
            context_window=d.get("context_window"),
            latency_ms=s.get("mean_latency_ms") or d.get("first_token_ms"),
            rate_429=rate_429,
            error_rate=error_rate,
            headroom_exhausted=headroom is not None and headroom <= 0.05,
            callable=(d["provider"].split("/")[0] in available) or is_pushed,
            pushed=is_pushed,
            prior_tags=lb.tags(prior_keys, benchmark),
            routing_approvals=approvals,
            routing_disapprovals=disapprovals,
            routing_sentiment=round(sentiment_multiplier, 3),
            traffic_share=round(traffic_share, 4),
            traffic_capped=traffic_capped,
        )
        c.rejected = _reject(c, d, caps, task, policy)
        out.append(c)

    # Second pass: the real cost of a "free" arm.
    #
    # Marginal cost is zero while its bucket has headroom, and the price of the
    # next-best option once it does not. We estimate bucket exhaustion two ways —
    # a configured bucket we have drained, and our own observed 429 rate — and
    # take the worse. Without this, an arm that 429s half the time still shows a
    # cost of zero and wins every ranking, which is the "free but effectively
    # useless" failure the plan calls out.
    # `floor_cost` is the price of the paid fallback an exhausted free arm
    # forces. It must be a *positive* paid price: a zero-priced arm that is not
    # classified free (a transcription model with `price_out=None`, say) would
    # otherwise set the floor to 0 and nullify the whole penalty — the
    # 100%-429 free arm then stayed at cost 0 and kept winning the ranking.
    paid = [c.cost_per_call for c in out
            if c.free_kind is None and math.isfinite(c.cost_per_call)
            and c.cost_per_call > 0]
    if paid:
        floor_cost = min(paid)
        for c in out:
            if not c.free_kind:
                continue
            # "Free but effectively useless" is not only about 429s. An arm
            # that 404s or 402s every time costs the price of the paid
            # fallback it forces, so the generic error rate belongs in this
            # estimate beside the quota one.
            p_exhausted = max(c.rate_429, c.error_rate,
                              1.0 if c.headroom_exhausted else 0.0)
            if p_exhausted > 0:
                c.cost_per_call = p_exhausted * floor_cost
                c.cost_per_success = c.cost_per_call * task.estimated_calls(c.p_lb * c.availability)
            c.quota_risk = p_exhausted

    # Deterministic best-first order. There is no ORDER BY on the deployments
    # read, so the row order differed between SQLite (insertion order) and
    # Postgres (whatever the planner returns) — and `rank_for_compare` takes
    # `ranked[0]` as arm 0, so the same registry picked a different arm 0 on each
    # engine. Sorting here makes the function's output a contract rather than a
    # coincidence of the storage engine; `route` sorts the filtered list again
    # with pushed-arm priority, so nothing downstream changes.
    out.sort(key=lambda c: _sort_key(c, policy.objective))
    # The exploration bonus needs a total-observation count for its logarithm, and
    # it is applied per candidate against this same list — so it stays a pure
    # function of the evidence.
    total_obs = sum(c.n_obs for c in out)
    best_by_cost = _best_p_by_cost(out)
    out.sort(key=lambda c: _sort_key(
        c, policy.objective, policy.explore_c, total_obs,
        best_by_cost.get(c.cost_per_success, 0.0), policy.explore_band))
    return out


def _cost_per_call(pin: float | None, pout: float | None, task: TaskProfile) -> float:
    """Unknown price is not free. Treat it as unknown and let policy decide."""
    if pin is None and pout is None:
        return float("inf")
    return ((pin or 0.0) * task.tokens_in + (pout or 0.0) * task.tokens_out) / 1_000_000


def _reject(c: Candidate, d: dict, caps: dict, task: TaskProfile, policy: Policy) -> str | None:
    # Reachability first: an arm behind an unkeyed provider is not a candidate,
    # however cheap it looks. Checked before capability/context so the reason is
    # never masked by a second, irrelevant rejection.
    # If the user explicitly pushed this model, do not reject for lack of configured key.
    if getattr(c, "pushed", False):
        pass
    elif policy.require_callable and not c.callable:
        return "no API key (provider not configured)"
    # `hibernated` is the human-review state: a deployment that was free and is
    # now charging. It is out of the ranking until an operator says otherwise.
    if d.get("status") in ("deprecated", "gated", "hibernated"):
        return f"status={d['status']}"
    for cap, needed in {**policy.require, **task.require}.items():
        if not needed:
            continue
        have = caps.get(cap)
        if have is None:
            # The task gets the last word: whether an unverified capability is
            # tolerable depends on what it is used for. A model that cannot emit a
            # tool call fails silently — the loop simply does nothing — while a
            # vision answer is visible to whoever asked for it.
            mode = (getattr(task, "on_unverified_capability", None)
                    or policy.on_unverified_capability)
            if mode == "reject":
                return f"unverified capability: {cap} (source never reported it)"
            continue
        if not have:
            return f"unsupported capability: {cap}"
    need_ctx = max(policy.min_context, task.min_context)
    if need_ctx and (c.context_window or 0) < need_ctx:
        return f"context {c.context_window or 0} < {need_ctx}"
    if not policy.allow_subscription and c.free_kind == "subscription":
        return "subscription not allowed"
    if math.isinf(c.cost_per_call) and not policy.allow_unknown_price:
        return "price unknown"
    if policy.max_cost_in is not None and c.price_in is not None and c.price_in > policy.max_cost_in:
        return f"price_in {c.price_in} > ceiling {policy.max_cost_in}"
    if policy.max_cost_out is not None and c.price_out is not None and c.price_out > policy.max_cost_out:
        return f"price_out {c.price_out} > ceiling {policy.max_cost_out}"
    floor = max(policy.min_success_lb, task.min_success_lb)
    if c.p_lb < floor:
        # Bounded trial: exempt while we have little evidence, then held to the
        # floor. Observations feed p_lb, so an arm that keeps failing walks out
        # of the exemption on its own — no separate "demote" step needed.
        if (policy.free_floor_exempt and c.free_kind is not None
                and c.n_obs < policy.free_trial_obs):
            return None
        if c.prior_key == "none":
            # No benchmark source covers this model at all. Different fix from a
            # model that was measured and found wanting: add a source, don't
            # lower the bar.
            return f"no benchmark evidence (p_lb {c.p_lb:.2f} < {floor:.2f})"
        return f"p(success) {c.p_lb:.2f} < {floor:.2f}"
    return None


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #


def _best_p_by_cost(cands: list[Candidate]) -> dict[float, float]:
    """Best p(success) within each cost-per-success tier.

    The exploration band has to be relative to the arms a candidate is actually
    competing with. Computed across every eligible arm it was useless: a
    high-prior *paid* arm (which ranks below every free arm on cost anyway) set
    the bar so high that no free arm ever qualified for the bonus, and the
    exploration silently did nothing.
    """
    out: dict[float, float] = {}
    for c in cands:
        if c.p_lb > out.get(c.cost_per_success, 0.0):
            out[c.cost_per_success] = c.p_lb
    return out


def _sort_key(c: Candidate, objective: str, explore_c: float = 0.0, total_obs: int = 0,
              best_p: float = 0.0, explore_band: float = 0.0):
    effective_p = c.p_lb * getattr(c, "routing_sentiment", 1.0)
    is_cold_start = bool(c.free_kind is not None and c.cost_per_call == 0.0 and c.n_obs < 3)
    if explore_c > 0 and (c.p_lb >= best_p - explore_band or is_cold_start):
        # An under-observed arm is *uncertain*, not proven bad. The bonus is a
        # UCB1 term, so it is largest for n=0 and decays as evidence arrives.
        n = max(0, c.n_obs)
        effective_p += explore_c * math.sqrt(math.log(max(1, total_obs) + 1.0) / (n + 1.0))
    # `c.deploy_id` is the final key in every branch, and it is what makes the
    # order *total*. Without it, two candidates with identical price, quality and
    # latency tie, and Python's stable sort falls back to the input order — which
    # comes from a `SELECT` with no `ORDER BY`, so SQLite (insertion order) and
    # Postgres (planner order) disagreed. The same registry then chose a
    # different arm 0 per engine, which is not reproducible and broke
    # `rank_for_compare`'s `ranked[0]`.
    if objective == "quality":
        return (-effective_p, c.cost_per_success, c.latency_ms or 9e9, c.deploy_id)
    if objective == "latency":
        return (c.latency_ms or 9e9, c.cost_per_success, -effective_p, c.deploy_id)
    return (c.cost_per_success, -effective_p, c.latency_ms or 9e9, c.deploy_id)


def ucb_score(c: Candidate, total_obs: int, c_explore: float = 0.6) -> float:
    """Upper Confidence Bound (UCB1) score for compare exploration.

    Balances exploited quality (effective_p) with exploration bonus
    inversely proportional to sqrt(observations). Under-tested models get
    a higher score, ensuring they receive trials in compare mode.
    """
    base_q = c.p_lb * getattr(c, "routing_sentiment", 1.0)
    n = max(0, c.n_obs)
    N = max(1, total_obs)
    bonus = c_explore * math.sqrt(math.log(N + 1.0) / (n + 1.0))
    return base_q + bonus


def is_free_arm(c: Candidate) -> bool:
    """A free arm with zero marginal cost right now.

    The same predicate the funnel reports as `free_eligible`, kept in one place so
    the ranking cannot disagree with the number an operator reads.
    """
    return c.free_kind is not None and c.cost_per_call == 0.0


def _compare_tier(c: Candidate) -> int:
    """Which cost class a comparison arm belongs to.

    0  free right now — has a free tier and is not currently costing the floor
    1  has a free tier, but is priced at the paid floor (drained bucket, or an
       observed failure rate high enough that a paid fallback is likely)
    2  paid only

    Tier 1 matters: charging a flaky free arm the paid floor is what stops a
    broken arm from winning, but a floored arm is still *cheaper than* and
    *closer to* a genuine free arm than an always-paid one, so collapsing the two
    would either re-offer a broken arm as a free comparison or drop a
    perfectly good free arm into the paid pool.
    """
    if c.free_kind is None:
        return 2
    return 0 if c.cost_per_call == 0.0 else 1


def rank_for_compare(ranked: list[Candidate], c_explore: float = 0.6) -> list[Candidate]:
    """Order candidates for a comparison pair: exploit arm 0, explore arm 1.

    Arm 0 keeps the deterministic winner. The rest are re-ordered by UCB1 so a
    model that has never been tried gets offered instead of the same two every
    time.

    Exploration happens *within* a cost tier. UCB1's bonus is largest for an arm
    with zero observations, and a paid arm almost always has zero — so an
    unqualified bonus handed arm 1 to `claude-opus-5.5` ($0.034/call) while 55
    free arms sat unused, which is the exact opposite of "free first; pay only
    when free isn't good enough". The tier is a constraint; exploration orders
    inside it, and a lower tier is only exhausted before moving up.
    """
    if len(ranked) <= 1:
        return ranked
    arm0, rest = ranked[0], ranked[1:]

    def by_ucb(pool: list[Candidate]) -> list[Candidate]:
        total = sum(c.n_obs for c in pool)
        return sorted(pool, key=lambda c: ucb_score(c, total, c_explore), reverse=True)

    tiers: dict[int, list[Candidate]] = {}
    for c in rest:
        tiers.setdefault(_compare_tier(c), []).append(c)
    ordered: list[Candidate] = []
    for tier in sorted(tiers):
        ordered.extend(by_ucb(tiers[tier]))
    return [arm0] + ordered


@dataclass(slots=True)
class Decision:
    task: str
    policy: str
    mode: str
    eligible: list[Candidate]
    chosen: list[Candidate]
    funnel: dict[str, int]
    rejected_sample: list[Candidate] = field(default_factory=list)
    diversity: str = "none"
    strategy: str = "deterministic"
    # The full ranking before `_diversify` trimmed it to top_k. Best-of-N needs
    # this: the diversified `chosen` can hold the same model twice behind
    # different upstreams, which is right for failover and useless for a second
    # opinion.
    ranked: list[Candidate] = field(default_factory=list)
    # Readable reasons this arm won, for the decision log. Computed here rather
    # than in a caller because routing is what makes the choice — the proxy, the
    # CLI and the dashboard all read the same explanation instead of each
    # inventing one from the funnel.
    why: list[dict] = field(default_factory=list)


def _why(chosen: list[Candidate], eligible: list[Candidate], policy: Policy,
         strategy: str = "deterministic") -> list[dict]:
    """Why the winning arm won, as tags an operator can scan."""
    if not chosen:
        return []
    top = chosen[0]
    out: list[dict] = []

    def tag(key: str, label: str, detail: str | None = None) -> None:
        out.append({"key": key, "label": label, "detail": detail})

    if getattr(top, "pushed", False):
        tag("pushed", "pushed to top 3", "user priority push")
    elif strategy == "anti_fixation_rotate" or getattr(top, "anti_fixation_rotated", False):
        tag("anti-fixation", f"traffic capped at {int(policy.max_model_share_pct * 100)}%",
            "rotated from dominant arm to distribute traffic")
    elif strategy == "explore_coldstart":
        tag("explore-coldstart", "cold-start exploration (5%)",
            f"trialling untried model ({top.display_name})")

    # Say when the pick was not the deterministic best: an operator reading the
    # log should see "exploring" rather than assume a worse arm out-scored a
    # better one.
    if policy.explore_c > 0 and len(eligible) > 1:
        deterministic = min(eligible, key=lambda c: _sort_key(c, policy.objective))
        if deterministic.deploy_id != top.deploy_id:
            tag("explore", "testing an under-observed arm",
                f"{top.n_obs} previous observation{'s' if top.n_obs != 1 else ''}")

    if len(eligible) == 1:
        tag("only-option", "only eligible arm")

    if policy.objective in ("cost_per_success", "bandit"):
        cheapest = min(c.cost_per_success for c in eligible)
        if top.cost_per_success <= cheapest:
            tag("cheapest", "cheapest per success", f"${top.cost_per_success:.4f}")
        best = max(c.p_lb for c in eligible)
        if top.p_lb >= best and len(eligible) > 1:
            tag("quality", "highest p(success)", f"{top.p_lb:.2f}")
    elif policy.objective == "quality":
        best = max(c.p_lb for c in eligible)
        if top.p_lb >= best:
            tag("quality", "highest p(success)", f"{top.p_lb:.2f}")
    elif policy.objective == "latency":
        known = [c for c in eligible if c.latency_ms is not None]
        if known and top.latency_ms is not None and \
                top.latency_ms <= min(c.latency_ms for c in known):
            tag("fastest", "fastest measured", f"{top.latency_ms:.0f} ms")

    if top.free_kind:
        tag("free", f"free ({top.free_kind})")
    if top.free_kind and top.n_obs < policy.free_trial_obs:
        tag("on-trial", "on trial",
            f"{top.n_obs} of {policy.free_trial_obs} observations")

    if top.prior_tags:
        t = top.prior_tags[0]
        value = t.get("value")
        detail = t.get("label") if value is None else f"{t.get('label')} {value}"
        tag("leaderboard", f"leaderboard: {detail}", t.get("source"))
        if len(top.prior_tags) > 1:
            others = ", ".join(
                f"{x.get('label')} {x.get('value')}" for x in top.prior_tags[1:4])
            out[-1]["also"] = others
    elif top.prior_key == "none":
        tag("unbenchmarked", "no benchmark evidence")

    if len(eligible) > 1:
        tag("objective", f"ranked by {policy.objective}")
    return out


def _diversify(ordered: list[Candidate], policy: Policy) -> tuple[list[Candidate], str]:
    """Greedy top-K with a distinct-provider constraint, progressively relaxed.

    Each relaxation level is tried in order and the level actually achieved is
    returned, so a starved fallback list is visible rather than silently presented
    as three independent options.
    """
    if not policy.require_distinct_provider:
        return ordered[:policy.top_k], "none"

    # Pass 1 — strict: every arm a distinct gateway *and* a distinct upstream.
    chosen: list[Candidate] = []
    gws: set[str] = set()
    ups: set[str] = set()
    for c in ordered:
        if len(chosen) >= policy.top_k:
            break
        if chosen and (c.provider_family in gws or c.upstream in ups):
            continue
        chosen.append(c)
        gws.add(c.provider_family)
        ups.add(c.upstream)
    if len(chosen) >= min(policy.top_k, len(ordered)):
        return chosen, "gateway+upstream"

    # Pass 2 — round-robin across gateways, repeating one only once every gateway
    # has an arm.
    #
    # This pass used to be "distinct upstream", which is not separation at all
    # when one gateway aggregates many upstreams: with fewer than `top_k` gateways
    # eligible (a small free-tier pool is the normal case) it returned three arms
    # of the *same* gateway. One expired key or one outage there then failed the
    # whole request, even though another gateway in the very same list was
    # callable — the chain looked like three options and behaved like one.
    buckets: dict[str, list[Candidate]] = {}
    for c in ordered:
        buckets.setdefault(c.provider_family, []).append(c)
    chosen = []
    seen: set[tuple[str, str]] = set()
    while len(chosen) < policy.top_k:
        progressed = False
        for fam, bucket in buckets.items():
            for c in bucket:
                if (fam, c.upstream) in seen:
                    continue
                chosen.append(c)
                seen.add((fam, c.upstream))
                progressed = True
                break
            if len(chosen) >= policy.top_k:
                break
        if not progressed:
            break
    if chosen:
        return chosen, "gateway round-robin"

    # Pass 3 — nothing to spread; take the ranked order as-is.
    return ordered[:policy.top_k], "none"


def route(store: Store, task: TaskProfile, policy: Policy, *, mode: str = "auto",
          manual_ids: list[str] | None = None, pool: list[Candidate] | None = None,
          rng: "random.Random | None" = None,
          user_keys: dict[str, str] | None = None,
          pushed_models: list[str] | None = None,
          effort: str | None = None) -> Decision:
    effective_pushed = pushed_models
    if effective_pushed is None:
        effective_pushed = store.get_pushed_models(task.name)
    pushed_set = set(effective_pushed or [])

    cands = pool if pool is not None else build_candidates(
        store, task, policy, user_keys=user_keys, pushed_models=effective_pushed,
        effort=effort)

    for c in cands:
        if c.deploy_id in pushed_set:
            c.pushed = True

    funnel = {"total": len(cands)}
    ok = [c for c in cands if c.rejected is None]
    funnel["after_hard_filter"] = len(ok)
    funnel["free_eligible"] = sum(1 for c in ok if is_free_arm(c))
    funnel["distinct_providers"] = len({c.upstream for c in ok})
    # Distinguishing "does not support it" from "never told us" is what tells an
    # operator which source to fix rather than which model to drop.
    funnel["rejected_unsupported"] = sum(
        1 for c in cands if c.rejected and c.rejected.startswith("unsupported"))
    funnel["rejected_unverified"] = sum(
        1 for c in cands if c.rejected and c.rejected.startswith("unverified"))
    funnel["rejected_quality"] = sum(
        1 for c in cands if c.rejected and c.rejected.startswith("p(success)"))
    funnel["rejected_no_evidence"] = sum(
        1 for c in cands if c.rejected and c.rejected.startswith("no benchmark evidence"))
    funnel["rejected_no_key"] = sum(
        1 for c in cands if c.rejected and c.rejected.startswith("no API key"))

    if mode == "manual":
        wanted = set(manual_ids or [])
        ok = [c for c in ok if c.deploy_id in wanted]
        funnel["manual_pool"] = len(ok)

    # Ranking. If a model is pushed, it receives priority so it rises to the top
    if policy.objective == "bandit":
        ordered, strategy = bandit_order(ok, task, policy, rng=rng)
        if pushed_set:
            ordered = sorted(ordered, key=lambda c: 0 if c.deploy_id in pushed_set else 1)
    else:
        # Same UCB bonus as `build_candidates`, applied here because this is the
        # sort `route` actually ranks on (it re-sorts to honour pushed arms).
        explore_total = sum(c.n_obs for c in ok)
        best_by_cost = _best_p_by_cost(ok)
        ordered = sorted(ok, key=lambda c: (
            0 if c.deploy_id in pushed_set else 1,
            _sort_key(c, policy.objective, policy.explore_c, explore_total,
                      best_by_cost.get(c.cost_per_success, 0.0), policy.explore_band)))
        deterministic = min(ok, key=lambda c: _sort_key(c, policy.objective)) if ok else None
        exploring = bool(policy.explore_c > 0 and ordered and deterministic
                         and ordered[0].deploy_id != deterministic.deploy_id)
        strategy = "explore" if exploring else "deterministic"

        if ordered and not pushed_set:
            # Cold-start exploration via exploration_eps (e.g. 5%)
            import random
            active_rng = rng if rng is not None else random
            if policy.exploration_eps > 0 and active_rng.random() < policy.exploration_eps:
                untried = [c for c in ok if is_free_arm(c) and c.n_obs < policy.free_trial_obs]
                if untried:
                    untried.sort(key=lambda c: (c.n_obs, -c.p_prior))
                    cold_pick = untried[0]
                    if cold_pick != ordered[0]:
                        ordered.remove(cold_pick)
                        ordered.insert(0, cold_pick)
                        strategy = "explore_coldstart"

            # Anti-fixation traffic concentration cap (e.g. 60%)
            if strategy != "explore_coldstart" and ordered[0].traffic_capped:
                uncapped_cand = next((c for c in ordered[1:] if not c.traffic_capped), None)
                if uncapped_cand:
                    setattr(uncapped_cand, "anti_fixation_rotated", True)
                    ordered.remove(uncapped_cand)
                    ordered.insert(0, uncapped_cand)
                    strategy = "anti_fixation_rotate"

    chosen, diversity = _diversify(ordered, policy)

    # Guarantee: any eligible pushed candidate is preserved inside the top 3 shortlist
    if pushed_set:
        for pid in reversed(effective_pushed):
            cand = next((c for c in ok if c.deploy_id == pid), None)
            if cand:
                if cand in chosen:
                    chosen.remove(cand)
                chosen.insert(0, cand)
        chosen = chosen[:policy.top_k]

    rejected = [c for c in cands if c.rejected][:5]
    return Decision(task.name, policy.name, mode, ok, chosen, funnel, rejected,
                    diversity, strategy, ordered, _why(chosen, ok, policy, strategy))
