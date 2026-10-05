"""Contextual bandit — the self-learning half of the router (Phase 6).

The deterministic router (`objective: cost_per_success`) is explainable but
static: it trusts the benchmark prior until observations accumulate, and it never
deliberately tries an uncertain arm. The bandit changes *ordering only*, and only
among arms that already cleared the hard filters — it can never override a
capability or price constraint (cheap-but-incapable can't be learned into
correctness).

  * **Thompson sampling.** p(success) is drawn from a Beta posterior seeded by the
    benchmark prior, so an untried arm is *uncertain*, not assumed bad. This is
    what prevents rich-get-richer lock-in where an arm wins only because it was
    tried first.
  * **Reward is verified success, penalty is cost.** Arms are ordered by *sampled*
    cost-per-success, so the bandit explores where exploring is cheap.
  * **Exploration is bounded.** With probability `exploration_eps` it deliberately
    takes the most uncertain eligible arm — but only among arms no more expensive
    than the best deterministic choice (`exploration_cost_slack`). That is the
    spend cap, enforced at decision time.

Observations stay append-only and all statistics are derived, so the posterior is
always re-derivable from history.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from .schema import Candidate, TaskProfile


@dataclass(slots=True)
class Posterior:
    candidate: Any
    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        n = self.alpha + self.beta
        return (self.alpha * self.beta) / (n * n * (n + 1.0))

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(self.alpha, self.beta)


def posterior(c: "Candidate", prior_strength: float) -> Posterior:
    """Beta(prior pseudo-counts + observations).

    The prior contributes `prior_strength` pseudo-observations split by the
    benchmark-derived `p_prior`, so a cold arm has a real, non-zero belief.
    """
    a0 = max(1e-3, float(c.p_prior)) * prior_strength
    b0 = max(1e-3, 1.0 - float(c.p_prior)) * prior_strength
    wins = max(0, int(c.wins))
    losses = max(0, int(c.n_obs) - wins)
    return Posterior(c, a0 + wins, b0 + losses)


def sampled_cost_per_success(c: "Candidate", p: float, task: "TaskProfile") -> float:
    p_eff = max(1e-6, p * float(c.availability))
    return float(c.cost_per_call) * task.estimated_calls(p_eff)


def bandit_order(
    cands: list["Candidate"],
    task: "TaskProfile",
    policy,
    *,
    rng: random.Random | None = None,
) -> tuple[list["Candidate"], str]:
    """Order eligible candidates; returns (ordered, strategy).

    `strategy` is 'explore' when the ε-greedy branch fired, else 'thompson'.
    """
    if not cands:
        return [], "thompson"
    rng = rng or random.Random()
    posts = [posterior(c, policy.prior_strength) for c in cands]

    if policy.exploration_eps > 0 and rng.random() < policy.exploration_eps:
        best_cost = min(float(c.cost_per_call) for c in cands)
        ceiling = best_cost * (1.0 + max(0.0, policy.exploration_cost_slack)) + 1e-12
        pool = [p for p in posts if float(p.candidate.cost_per_call) <= ceiling]
        if pool:
            pick = max(pool, key=lambda p: (p.variance,
                                            -float(p.candidate.cost_per_call),
                                            p.mean))
            rest = [p for p in posts if p is not pick]
            rest.sort(key=lambda p: sampled_cost_per_success(p.candidate, p.sample(rng), task))
            return [pick.candidate] + [p.candidate for p in rest], "explore"

    scored = [(sampled_cost_per_success(p.candidate, p.sample(rng), task), p.candidate)
              for p in posts]
    scored.sort(key=lambda t: t[0])
    return [c for _, c in scored], "thompson"


def describe(cands: list["Candidate"], task: "TaskProfile", policy) -> list[dict]:
    """Posterior table for the operator (`mi bandit`). Deterministic — uses means."""
    rows = []
    for c in cands:
        p = posterior(c, policy.prior_strength)
        rows.append({
            "deploy_id": c.deploy_id,
            "n_obs": int(c.n_obs),
            "wins": int(c.wins),
            "p_prior": round(float(c.p_prior), 4),
            "prior_key": c.prior_key,
            "mean": round(p.mean, 4),
            "variance": round(p.variance, 6),
            "alpha": round(p.alpha, 3),
            "beta": round(p.beta, 3),
            "cost_per_call": float(c.cost_per_call),
            "expected_cost_per_success": sampled_cost_per_success(c, p.mean, task),
        })
    rows.sort(key=lambda r: r["expected_cost_per_success"])
    return rows
