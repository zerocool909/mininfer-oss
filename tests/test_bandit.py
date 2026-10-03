"""Tests for the contextual bandit (Phase 6).

The properties that matter: exploration is real but bounded, the bandit converges
to the arm that actually succeeds, and it can only *reorder* eligible arms — it
must never resurrect one the hard filter rejected.
"""
from __future__ import annotations

import random
from collections import Counter
from types import SimpleNamespace

import pytest

from mininfer.bandit import bandit_order, posterior
from mininfer.router import Policy, build_candidates, route
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store

TASK = TaskProfile("sql_generation", tokens_in=1000, tokens_out=100,
                   benchmark_keys=("coding",), min_success_lb=0.0)


def _two_arms(tmp_path, *, obs: dict | None = None, caps_a=None, caps_b=None,
              price_a=1.0, price_b=1.0, zero_a=False):
    obs = obs or {}
    s = Store(tmp_path / "b.db")
    s.upsert_weights(Weights("slug:m", "m", benchmark={"coding": 80.0}))
    s.upsert_deployment(Deployment("pa:x", "slug:m", "pa", "x", price_in=price_a,
                                   price_out=price_a, caps=caps_a or {},
                                   zero_price=zero_a))
    s.upsert_deployment(Deployment("pb:y", "slug:m", "pb", "y", price_in=price_b,
                                   price_out=price_b, caps=caps_b or {}))
    s.commit()
    for did, (wins, n) in obs.items():
        for i in range(n):
            s.observe(did, "sql_generation", ok=(i < wins),
                      ts=f"2026-01-01T00:00:{i:02d}+00:00")
    s.commit()
    return s


def _eligible(s: Store, policy: Policy):
    return [c for c in build_candidates(s, TASK, policy) if c.rejected is None]


# ------------------------------------------------------------------ posterior


def test_posterior_is_prior_plus_observations():
    c = SimpleNamespace(p_prior=0.6, n_obs=10, wins=7)
    p = posterior(c, prior_strength=20)
    assert p.alpha == pytest.approx(0.6 * 20 + 7)
    assert p.beta == pytest.approx(0.4 * 20 + 3)
    assert p.mean == pytest.approx(19 / 30)


def test_variance_shrinks_with_evidence():
    cold = posterior(SimpleNamespace(p_prior=0.6, n_obs=0, wins=0), 20)
    warm = posterior(SimpleNamespace(p_prior=0.6, n_obs=50, wins=35), 20)
    assert warm.variance < cold.variance


# ---------------------------------------------------------------- convergence


def test_thompson_prefers_the_proven_arm(tmp_path):
    s = _two_arms(tmp_path, obs={"pa:x": (20, 20), "pb:y": (0, 20)})
    policy = Policy()
    cands = _eligible(s, policy)
    rng = random.Random(0)
    picks: Counter = Counter()
    for _ in range(300):
        order, _ = bandit_order(cands, task=TASK, policy=policy, rng=rng)
        picks[order[0].deploy_id] += 1
    assert picks["pa:x"] > 240     # the arm that actually succeeds
    assert picks["pb:y"] < 60
    s.close()


def test_cold_arm_is_uncertain_not_assumed_bad(tmp_path):
    """An untried arm must still get tried — no rich-get-richer lock-in."""
    s = _two_arms(tmp_path, obs={"pa:x": (20, 20)})
    policy = Policy()
    cands = _eligible(s, policy)
    rng = random.Random(1)
    picks: Counter = Counter()
    for _ in range(300):
        order, _ = bandit_order(cands, task=TASK, policy=policy, rng=rng)
        picks[order[0].deploy_id] += 1
    assert picks["pb:y"] > 0
    s.close()


# ---------------------------------------------------------------- exploration


def test_exploration_picks_the_most_uncertain_arm(tmp_path):
    s = _two_arms(tmp_path, obs={"pa:x": (20, 20)})
    policy = Policy(exploration_eps=1.0, exploration_cost_slack=0.0)
    order, strategy = bandit_order(_eligible(s, policy), task=TASK, policy=policy,
                                   rng=random.Random(2))
    assert strategy == "explore"
    assert order[0].deploy_id == "pb:y"   # never observed -> highest variance
    s.close()


def test_exploration_spend_is_bounded(tmp_path):
    """A free arm is the ceiling; an expensive uncertain arm is never explored."""
    s = _two_arms(tmp_path, obs={"pa:x": (20, 20)}, zero_a=True, price_b=100.0)
    policy = Policy(exploration_eps=1.0, exploration_cost_slack=0.0)
    order, strategy = bandit_order(_eligible(s, policy), task=TASK, policy=policy,
                                   rng=random.Random(3))
    assert strategy == "explore"
    assert order[0].deploy_id == "pa:x"   # the only arm inside the cost ceiling
    s.close()


# ----------------------------------------------------------------- guardrail


def test_bandit_cannot_resurrect_a_rejected_arm(tmp_path):
    # pb:y cannot do tools, so the hard filter removes it before ranking.
    s = _two_arms(tmp_path, caps_a={"tools": True}, caps_b={"tools": False})
    task = TaskProfile("sql_generation", tokens_in=1000, tokens_out=100,
                       require={"tools": True}, benchmark_keys=("coding",))
    policy = Policy(objective="bandit", exploration_eps=0.5)
    dec = route(s, task, policy, rng=random.Random(4))
    assert dec.chosen
    assert all(c.deploy_id != "pb:y" for c in dec.chosen)
    assert all(c.rejected is None for c in dec.chosen)
    s.close()


def test_route_reports_bandit_strategy(tmp_path):
    s = _two_arms(tmp_path)
    policy = Policy(objective="bandit")
    dec = route(s, TASK, policy, rng=random.Random(5))
    assert dec.strategy in ("thompson", "explore")
    s.close()


def test_route_still_respects_diversity_under_bandit(tmp_path):
    s = _two_arms(tmp_path)
    policy = Policy(objective="bandit", top_k=2)
    dec = route(s, TASK, policy, rng=random.Random(6))
    ups = {c.upstream for c in dec.chosen}
    assert len(dec.chosen) == len(ups)   # distinct upstreams
    s.close()
