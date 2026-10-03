"""Exploration widens coverage without handing the request to a bad arm.

The failure this guards: with every free arm priced at cost-per-success 0, the
ranking collapsed to the benchmark prior, so one arm answered every request —
never testing the other ~56 free arms, concentrating load on a single quota, and
inheriting that provider's outages unhedged.
"""
from __future__ import annotations

from mininfer.router import Policy, _sort_key
from mininfer.schema import Candidate


def _cand(name: str, p_lb: float, n_obs: int, cost: float = 0.0) -> Candidate:
    return Candidate(
        deploy_id=name, provider=name, provider_family=name, upstream=name, vendor=name,
        weights_id=f"hf:{name}", display_name=name, price_in=0.0, price_out=0.0,
        free_kind="zero_price", p_prior=p_lb, prior_key="aa_intelligence",
        n_obs=n_obs, wins=int(p_lb * n_obs), p_lb=p_lb, availability=1.0,
        headroom=1.0, headroom_src="test", cost_per_call=cost, cost_per_success=cost,
        context_window=8000, latency_ms=100.0,
    )


def _first(cands, policy: Policy) -> str:
    total = sum(c.n_obs for c in cands)
    best_p = max(c.p_lb for c in cands)
    ordered = sorted(cands, key=lambda c: _sort_key(
        c, policy.objective, policy.explore_c, total, best_p, policy.explore_band))
    return ordered[0].deploy_id


def test_without_exploration_the_best_prior_wins():
    cands = [_cand("leader", 0.74, 50), _cand("near", 0.58, 0)]
    assert _first(cands, Policy()) == "leader"


def test_a_near_miss_is_tried_instead_of_hammering_the_leader():
    cands = [_cand("leader", 0.74, 50), _cand("near", 0.58, 0)]
    policy = Policy(explore_c=0.10, explore_band=0.25)
    assert _first(cands, policy) == "near"


def test_an_unknown_floor_arm_is_not_explored():
    """0.30 is the p_lb floor for 'no evidence' — outside the band, so ignored."""
    cands = [_cand("leader", 0.74, 50), _cand("floor", 0.30, 0)]
    policy = Policy(explore_c=0.10, explore_band=0.25)
    assert _first(cands, policy) == "leader"


def test_exploration_never_crosses_a_cost_tier():
    """However uncertain, a paid arm may not beat a free one on curiosity."""
    cands = [_cand("free", 0.50, 50, cost=0.0), _cand("paid", 0.95, 0, cost=0.01)]
    policy = Policy(explore_c=0.10, explore_band=0.25)
    assert _first(cands, policy) == "free"


def test_the_bonus_decays_as_evidence_arrives():
    """Once the near-miss is observed, the leader takes back the request."""
    cands = [_cand("leader", 0.74, 50), _cand("near", 0.58, 40)]
    policy = Policy(explore_c=0.10, explore_band=0.25)
    assert _first(cands, policy) == "leader"
