"""Difficulty-scoped evidence in the router (#complexity routing).

`observations.effort` records the difficulty a call was routed under. This pins
the four properties that make it worth having — and the one that keeps it safe:

  1. no difficulty cell reproduces the previous number *exactly* (identity);
  2. a cell with real evidence moves the posterior;
  3. shrinkage means one observation barely moves it, because a posterior that can
     be swung by a single call is the collapse `prior_strength` already documents;
  4. the counts travel with the candidate, so the blend is auditable.

The trap this guards: the task-level view *already contains* the effort cell, so
adding the cell on top would double-count. The task posterior's mean is the prior
and the cell is the evidence added to it — which is why (1) holds.
"""
from __future__ import annotations

import pytest

from mininfer.router import Policy, build_candidates
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store

TASK = TaskProfile(
    name="t",
    tokens_in=1000,
    tokens_out=400,
    require={"structured": True},
    min_context=8000,
    benchmark_keys=("aa_intelligence",),
    benchmark_weights={"aa_intelligence": 1.0},
)


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "p.db")
    # The anchor has no deployment: it exists so `benchmark_norms` has a p5/p95
    # spread. With a single weight hi == lo and every arm prior collapses equal.
    s.upsert_weights(Weights("hf:anchor", "anchor", params_b=1.0,
                             benchmark={"aa_intelligence": 10.0}))
    s.upsert_weights(Weights("hf:m", "m", params_b=7.0, benchmark={"aa_intelligence": 90.0}))
    s.upsert_deployment(Deployment("prov:m", "hf:m", "prov", "m",
                                   price_in=0.1, price_out=0.1, context_window=32000,
                                   caps={"structured": True}))
    s.commit()
    return s


def _observe(store: Store, effort: str | None, ok: bool, n: int) -> None:
    for i in range(n):
        store.observe("prov:m", "t", ok=ok, ts=f"2026-01-0{n}T00:00:{i:02d}Z", effort=effort)
    store.commit()


def _p_lb(store: Store, effort: str | None = None) -> float:
    cands = build_candidates(store, TASK, Policy(), effort=effort)
    return next(c for c in cands if c.deploy_id == "prov:m").p_lb


def test_no_difficulty_evidence_reproduces_the_previous_posterior(tmp_path):
    """Identity: an empty cell must not move a single number."""
    s = _store(tmp_path)
    _observe(s, None, True, 4)

    base = _p_lb(s)
    for effort in ("low", "medium", "high"):
        assert _p_lb(s, effort) == pytest.approx(base), effort
    s.close()


def test_a_difficulty_cell_moves_the_posterior(tmp_path):
    """A model that loses at one difficulty must score worse *at that difficulty*."""
    s = _store(tmp_path)
    _observe(s, None, True, 6)        # fine overall
    _observe(s, "high", False, 12)    # but loses whenever it is routed as hard

    overall = _p_lb(s)
    at_high = _p_lb(s, "high")
    assert at_high < overall - 0.1, (overall, at_high)
    # The other difficulties are untouched: their cells are empty.
    assert _p_lb(s, "low") == pytest.approx(overall)
    s.close()


def test_shrinkage_keeps_one_observation_from_swinging_the_ranking(tmp_path):
    """One bad call must not outvote a posterior backed by `prior_strength`."""
    s = _store(tmp_path)
    _observe(s, None, True, 6)
    base = _p_lb(s)

    _observe(s, "high", False, 1)
    after_one = _p_lb(s, "high")
    _observe(s, "high", False, 19)
    after_twenty = _p_lb(s, "high")

    assert after_one < base, (base, after_one)
    # Shrinkage, stated as a ratio rather than an absolute bound: the single
    # observation moves things a *fraction* of what a sustained record does. An
    # absolute bound would be wrong here because the cell's own rows are part of
    # the task-level baseline too, so the first failure moves both.
    assert (base - after_one) < (base - after_twenty) / 2, (base, after_one, after_twenty)
    assert after_twenty < after_one - 0.1, "twenty observations should dominate"
    s.close()


def test_the_candidate_reports_the_cell_it_blended(tmp_path):
    """The blend has to be auditable: the counts travel with the candidate."""
    s = _store(tmp_path)
    _observe(s, "high", True, 3)
    _observe(s, "high", False, 1)

    c = next(x for x in build_candidates(s, TASK, Policy(), effort="high")
             if x.deploy_id == "prov:m")
    assert (c.effort_n, c.effort_wins) == (4, 3)

    # Without a difficulty it reports nothing rather than the stale cell.
    c0 = next(x for x in build_candidates(s, TASK, Policy()) if x.deploy_id == "prov:m")
    assert (c0.effort_n, c0.effort_wins) == (0, 0)
    s.close()


def test_quality_floor_can_reject_at_one_difficulty_only(tmp_path):
    """The point of the blend: a floor can bite on hard prompts and not on easy ones."""
    s = _store(tmp_path)
    _observe(s, None, True, 20)     # a good record overall
    _observe(s, "high", False, 8)  # with a bad one specifically at `high`

    policy = Policy()

    # Same arm, same task: a strong lower bound on an easy prompt, a weak one on a
    # hard prompt — so a single floor can accept one and reject the other.
    easy = next(x for x in build_candidates(s, TASK, policy, effort="low")
                if x.deploy_id == "prov:m")
    hard = next(x for x in build_candidates(s, TASK, policy, effort="high")
                if x.deploy_id == "prov:m")
    assert easy.p_lb > 0.5 > hard.p_lb, (easy.p_lb, hard.p_lb)
    s.close()


def test_a_busy_task_does_not_drown_the_difficulty_cell(tmp_path):
    """The cap on the prior's weight, which is what makes this work at scale.

    Un-capped, the prior weighs `prior_strength + n_obs`, which grows without
    bound — so on a busy task a 50-observation difficulty cell would shift the mean
    by a fraction of a percent and difficulty learning would only ever work on
    quiet tasks. Under that formula this test fails: the drop is ~0.08, not >0.1.
    """
    s = _store(tmp_path)
    _observe(s, None, True, 500)      # a busy task
    base = _p_lb(s)

    _observe(s, "high", False, 50)    # a solid difficulty-specific record
    at_high = _p_lb(s, "high")

    assert base - at_high > 0.1, (base, at_high)
    s.close()
