"""Core entities.

The central decision: identity (Weights) is separated from economics (Deployment).

  Weights    = the artifact. Benchmarks and true capabilities live here.
  Deployment = a callable endpoint. Price, limits, latency, uptime live here.

Both `weights_id` and `deploy_id` are content-derived, so a provider silently
repointing `-latest` changes a *field*, never the identity of a row.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

# Every `error_class` that is **not evidence about the model**. A call that never
# reached the model — no key, a TLS/DNS failure on our side, a malformed registry
# entry — says nothing about that model's quality, so `routing_stats` excludes it
# from `n` and `wins` (see `store._ROUTING_STATS_VIEW`).
#
# What is deliberately *kept* as a trial: `429`, `timeout`, `5xx` (the deployment
# under load — availability signal the router also tracks as `n_429` / `n_timeout`),
# `bad_output` / `empty_content` (the model answered badly), and every `http_4xx`
# (the provider answered *about this deployment*: a 404 is a stale id and still
# costs the paid fallback). Those are properties of the deployment, which is
# exactly what the router ranks.
#
# The failure this prevents: a container that could not verify a provider's
# certificate recorded 32 `network_error`s as model losses, dragging down the very
# arms that worked.
NON_MODEL_ERRORS: frozenset[str] = frozenset({
    "no_api_key",   # our environment has no key for this provider
    "auth_error",   # our key is rejected/revoked (401/403)
    "network_error",  # DNS/connection failure before the model answered
    "tls_error",    # certificate verification failed (our CA bundle)
    "bad_deploy_id",  # a registry row with no parseable model id
    "no_candidates",  # nothing was selected to call
    # The provider serves this deployment only to allowlisted apps (OpenRouter's
    # agentic harnesses return 401/403 with "only available on agentic
    # harnesses"). It says nothing about the model's *quality* — the arm may be
    # excellent — so it must not drag the success rate down; what it says is that
    # the deployment is not callable, which the runtime retirement handles.
    "not_api_callable",
})

# Declared confidence per ingest source, for the fields it reports. A price read
# from a provider's own JSON is high confidence; a price scraped from a marketing
# page is not. Quarantined values never enter the registry unsourced.
SOURCE_CONFIDENCE: dict[str, float] = {
    "provider_api": 0.95,
    "aggregator_api": 0.85,
    "provider_reported_probe": 0.7,
    "our_probe": 0.9,
    "benchmark_site": 0.6,
    "llm_extraction": 0.4,
    "manual": 1.0,
}

# The capability vocabulary every ingest path normalises to, and the explorer
# filters on. One definition: it had been spelled out in `agent_ingest` (for
# extraction) and again in `store` (for the explorer's facet counts), which is
# two lists to update for one provider field. `true` means confirmed, `null`
# means unknown — a distinction the router depends on.
CAP_KEYS = ("tools", "structured", "vision", "reasoning", "caching", "audio")


@dataclass(slots=True)
class Evidence:
    entity_kind: str  # "weights" | "deployment"
    entity_id: str
    field: str
    value: Any
    source: str
    url: str | None = None
    fetched_at: str | None = None
    confidence: float = 0.5
    observed_via: str = "aggregator_api"

    def to_row(self) -> tuple:
        return (
            self.entity_kind,
            self.entity_id,
            self.field,
            json.dumps(self.value, default=str),
            self.source,
            self.url,
            self.fetched_at,
            self.confidence,
            self.observed_via,
        )


# --------------------------------------------------------------------------- #
# Weights — the artifact
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Weights:
    weights_id: str  # "hf:qwen/qwen3-30b-a3b-instruct-2507" | "slug:..."
    display_name: str
    hf_repo: str | None = None
    hf_revision: str | None = None
    family: str | None = None
    params_b: float | None = None
    arch: str | None = None  # dense | moe | unknown
    license: str | None = None
    released_at: str | None = None
    modalities: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    benchmark: dict[str, float] = field(default_factory=dict)
    benchmark_source: str | None = None

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["modalities"] = json.dumps(list(self.modalities))
        d["aliases"] = json.dumps(list(self.aliases))
        d["benchmark"] = json.dumps(self.benchmark)
        return d


# --------------------------------------------------------------------------- #
# Deployment — a callable thing
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Deployment:
    deploy_id: str  # "<provider>:<provider_model_id>"
    weights_id: str
    provider: str
    provider_model_id: str
    context_window: int | None = None
    max_output: int | None = None
    quantization: str | None = None

    # USD per 1,000,000 tokens. None = unknown (NOT the same as 0).
    price_in: float | None = None
    price_out: float | None = None
    price_cached_in: float | None = None

    # "Free" is four distinct things and they are modelled separately.
    zero_price: bool = False  # list price is literally $0
    free_variant: bool = False  # e.g. `:free` / `-free` id suffix
    subscription: bool = False  # flat monthly fee -> marginal cost ~0
    trial_credits: bool = False  # free credits that deplete

    discount: float | None = None
    caps: dict[str, bool] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)  # rpm/rpd/tpm/tpd/concurrency
    limits_confirmed: bool = False  # did we read these from an authoritative source?

    uptime_1d: float | None = None
    first_token_ms: float | None = None
    throughput: float | None = None

    status: str = "live"
    source: str = ""
    source_url: str | None = None

    @property
    def is_free_capable(self) -> bool:
        return self.zero_price or self.free_variant or self.subscription or self.trial_credits

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["caps"] = json.dumps(self.caps)
        d["limits"] = json.dumps(self.limits)
        return d


def make_deploy_id(provider: str, provider_model_id: str) -> str:
    return f"{provider}:{provider_model_id}"


def content_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


# --------------------------------------------------------------------------- #
# Task profile — what a workload needs, and what it costs to run
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TaskProfile:
    name: str
    tokens_in: int
    tokens_out: int
    require: dict[str, bool] = field(default_factory=dict)
    min_context: int = 0
    min_success_lb: float = 0.0
    # benchmark keys consulted for the prior, in priority order
    benchmark_keys: tuple[str, ...] = ()
    # Optional per-leaderboard weights for the prior blend. Keys absent here
    # default to 1.0, so a task can add a leaderboard without re-weighting the
    # others. Keys with weight 0 are ignored.
    benchmark_weights: dict[str, float] = field(default_factory=dict)
    # Keyword cues for `auto` intent classification. Multi-word cues match as
    # phrases and count for more than a single word.
    cues: tuple[str, ...] = ()
    description: str = ""
    # Whether a required capability that no source has ever reported is fatal for
    # *this* task. None inherits the policy default.
    #
    # Per-task because the cost of the strict answer is wildly uneven. Measured on
    # the shipped registry, `reject` costs 5 eligible arms for `tools` and
    # `structured` (212 -> 207, 178 -> 173) but 108 of 161 for `vision` — and left
    # the top pick for every one of those tasks unchanged. So the capability being
    # gated decides the answer, not a house style.
    on_unverified_capability: str | None = None
    #: Model *roles* this task will accept. Empty (the default) means
    #: "general-purpose text generators only": a safety classifier, an embedding
    #: model, a TTS/ASR or an image/music generator is excluded. A task that
    #: genuinely wants one names it (`allow_roles: [safety]`).
    allow_roles: tuple[str, ...] = ()

    def estimated_calls(self, p_success: float) -> float:
        """Expected calls to get one success, capped at 4 to avoid absurd values."""
        if p_success <= 0:
            return 4.0
        return min(4.0, 1.0 / p_success)


@dataclass(slots=True)
class Candidate:
    """One deployment, scored for one task.

    A domain shape rather than a routing detail: `mi.bandit` needs it to order
    arms, `mi.proxy` needs it to explain a comparison, and both used to reach into
    `mi.router` for it — which is what made `router` and `bandit` mutually
    dependent and forced a `TYPE_CHECKING` guard on one side and a function-level
    import on the other. It lives here because this module imports nothing from
    the rest of `mi`, so either can import it without a cycle.
    """

    deploy_id: str
    provider: str
    provider_family: str
    upstream: str
    # Who made the model (`google`, `z-ai`, `qwen`) — not who serves it. The two
    # answers a comparison offers should not be two variants from one maker.
    vendor: str
    weights_id: str
    display_name: str
    price_in: float | None
    price_out: float | None
    free_kind: str | None
    p_prior: float
    prior_key: str
    n_obs: int
    wins: int
    p_lb: float
    availability: float
    headroom: float | None
    headroom_src: str
    cost_per_call: float
    cost_per_success: float
    context_window: int | None
    latency_ms: float | None
    rejected: str | None = None
    rate_429: float = 0.0
    # Fraction of observations that failed for any reason other than a
    # quota signal. `rate_429`/`rate_timeout` cover quota and latency; a
    # 404/402/500 is neither, and for a free arm it is otherwise invisible:
    # its list price is zero, so cost_per_success is identically zero no
    # matter how often it fails.
    error_rate: float = 0.0
    headroom_exhausted: bool = False
    quota_risk: float = 0.0
    callable: bool = True
    # Which leaderboards fed the prior, as `{key, label, source, value}` tags.
    prior_tags: list[dict] = field(default_factory=list)
    routing_approvals: int = 0
    routing_disapprovals: int = 0
    routing_sentiment: float = 1.0
    pushed: bool = False
    traffic_share: float = 0.0
    traffic_capped: bool = False
    anti_fixation_rotated: bool = False
    #: This arm's observations *at the difficulty the request was routed under*, and
    #: how many succeeded. Both zero when no difficulty was supplied. They are a
    #: subset of `n_obs`/`wins`, not an addition: `p_lb` blends the cell with the
    #: task-level posterior, it does not stack the two.
    effort_n: int = 0
    effort_wins: int = 0
    #: The evidence-only posterior (benchmark prior + observations), before any
    #: model-dossier adjustment. `p_lb` is what ranks; this is what the quality
    #: floor is judged on, so a dossier can reorder eligible arms but can never
    #: lift one over the floor. `None` means "same as p_lb" (no dossier applied).
    p_lb_evidence: float | None = None
    #: The task-fit adjustment `mi scout`'s dossier contributed to the ranking,
    #: and the inputs behind it, so a decision can explain itself.
    dossier_fit: float = 0.0
    dossier_reason: str = ""  # best_for | when_to_use | when_not_to_use | ""
    dossier_confidence: float = 0.0
    dossier_age_days: float | None = None
