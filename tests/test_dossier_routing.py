"""The model dossier as a routing prior.

`mi scout`'s form guide may nudge a free arm's ranking toward the task it fits —
but only within bounds: it can reorder eligible arms, never lift one over the
quality floor, fade as real observations arrive, and do nothing at all when the
weight is 0 (the shipped default).
"""
from __future__ import annotations

import datetime as dt

from mininfer.router import Policy, build_candidates, dossier_fit
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store

NOW = dt.datetime.now(dt.timezone.utc).isoformat()


def _dossier(**kw) -> dict:
    base = {"best_for": [], "when_to_use": [], "when_not_to_use": [],
            "confidence": 0.9, "generated_at": NOW}
    base.update(kw)
    return base


# --- the pure fit function ---------------------------------------------------


def test_no_dossier_is_no_signal():
    assert dossier_fit("t", None, Policy(dossier_weight=1.0)) == (0.0, "", {})


def test_best_for_is_a_positive_fit():
    delta, reason, meta = dossier_fit(
        "sql_generation", _dossier(best_for=["sql_generation"]),
        Policy(dossier_weight=0.5))
    assert reason == "best_for" and delta > 0
    assert meta["confidence"] == 0.9


def test_when_not_to_use_is_a_negative_fit():
    delta, reason, _ = dossier_fit(
        "code_edit", _dossier(when_not_to_use=["code_edit"]),
        Policy(dossier_weight=0.5, dossier_penalty_weight=0.8))
    assert reason == "when_not_to_use" and delta < 0


def test_when_to_use_is_half_a_best_for():
    p = Policy(dossier_weight=0.5)
    best, _, _ = dossier_fit("t", _dossier(best_for=["t"]), p)
    half, _, _ = dossier_fit("t", _dossier(when_to_use=["t"]), p)
    assert half == best / 2


def test_low_confidence_gets_no_vote():
    delta, reason, _ = dossier_fit(
        "t", _dossier(best_for=["t"], confidence=0.2),
        Policy(dossier_weight=1.0, dossier_min_confidence=0.6))
    assert delta == 0.0 and reason == "low_confidence"


def test_a_stale_dossier_gets_no_vote():
    delta, reason, _ = dossier_fit(
        "t", _dossier(best_for=["t"], generated_at="2000-01-01T00:00:00+00:00"),
        Policy(dossier_weight=1.0, dossier_max_age_days=90))
    assert delta == 0.0 and reason == "stale"


def test_maturity_shrinks_the_prior_as_observations_arrive():
    p = Policy(dossier_weight=1.0, dossier_shrink_k=20.0)
    fresh, _, _ = dossier_fit("t", _dossier(best_for=["t"]), p, n_obs=0)
    seen, _, _ = dossier_fit("t", _dossier(best_for=["t"]), p, n_obs=20)
    assert seen == fresh / 2  # k / (k + n) == 0.5 at n == k


def test_weight_zero_is_a_no_op_even_for_a_perfect_dossier():
    delta, reason, _ = dossier_fit(
        "t", _dossier(best_for=["t"], confidence=1.0), Policy(dossier_weight=0.0))
    assert delta == 0.0 and reason == "best_for"


# --- through the candidate builder -------------------------------------------


def _seed(tmp_path) -> Store:
    s = Store(tmp_path / "d.db")
    s.upsert_weights(Weights("w1", "one"))
    s.upsert_weights(Weights("w2", "two"))
    s.upsert_deployment(Deployment("p1:f1", "w1", "p1", "f1",
                                   price_in=0.0, price_out=0.0, zero_price=True,
                                   context_window=8000))
    s.upsert_deployment(Deployment("p2:f2", "w2", "p2", "f2",
                                   price_in=0.0, price_out=0.0, zero_price=True,
                                   context_window=8000))
    s.upsert_dossier("p1:f1", weights_id="w1", display_name="one", provider="p1",
                     price_out=0.0, core_competency="c", summary="s",
                     strengths=[], weaknesses=[], when_to_use=[],
                     when_not_to_use=[], best_for=["t"],
                     search_sources=[], facts={}, confidence=0.9,
                     generated_at=NOW)
    s.commit()
    return s


def _cands(s: Store, policy: Policy):
    task = TaskProfile("t", tokens_in=100, tokens_out=100, min_success_lb=0.0)
    return {c.deploy_id: c for c in build_candidates(s, task, policy)}


def test_weight_zero_leaves_the_ranking_unchanged(tmp_path):
    s = _seed(tmp_path)
    cands = _cands(s, Policy())
    for c in cands.values():
        assert c.dossier_reason == ""
        assert c.p_lb == c.p_lb_evidence  # nothing applied
    s.close()


def test_a_best_for_dossier_lifts_the_ranking_but_not_the_evidence(tmp_path):
    s = _seed(tmp_path)
    cands = _cands(s, Policy(dossier_weight=0.5, dossier_min_confidence=0.6))
    boosted = cands["p1:f1"]
    other = cands["p2:f2"]
    assert boosted.dossier_reason == "best_for" and boosted.dossier_fit > 0
    assert boosted.p_lb > boosted.p_lb_evidence       # ranking moved
    assert other.dossier_reason == "" and other.p_lb == other.p_lb_evidence
    s.close()


def test_shadow_records_the_fit_without_applying_it(tmp_path):
    s = _seed(tmp_path)
    cands = _cands(s, Policy(dossier_weight=0.5, dossier_shadow=True,
                             dossier_min_confidence=0.6))
    c = cands["p1:f1"]
    assert c.dossier_reason == "best_for" and c.dossier_fit > 0
    assert c.p_lb == c.p_lb_evidence  # computed, not applied
    s.close()


def test_a_dossier_cannot_lift_an_arm_over_the_quality_floor(tmp_path):
    """The floor is judged on evidence, so a flattering dossier cannot smuggle in
    an arm that does not actually clear it."""
    s = _seed(tmp_path)
    policy = Policy(dossier_weight=1.0, dossier_min_confidence=0.6,
                    min_success_lb=0.5, free_floor_exempt=False)
    cands = _cands(s, policy)
    c = cands["p1:f1"]
    assert c.p_lb_evidence < 0.5 <= c.p_lb   # the dossier *would* have cleared it
    assert c.rejected is not None            # and it is rejected anyway
    s.close()


# --- shadow mode & the calibration report ------------------------------------


def test_shadow_mode_records_the_counterfactual_pick(tmp_path):
    from mininfer.router import route

    s = _seed(tmp_path)
    # Give p2 real wins so it wins the served ranking; p1's dossier then makes a
    # different arm the shadow winner — the counterfactual the report scores.
    for _ in range(20):
        s.observe("p2:f2", "t", True, ts="2026-01-01T00:00:00+00:00")
    s.commit()

    task = TaskProfile("t", tokens_in=100, tokens_out=100, min_success_lb=0.0)
    # max_model_share_pct=1.0 keeps the anti-fixation rotation out of the way;
    # with all traffic on p2 it would otherwise rotate to p1 by design.
    policy = Policy(dossier_weight=0.9, dossier_shadow=True,
                    dossier_min_confidence=0.6, require_distinct_provider=False,
                    max_model_share_pct=1.0)
    dec = route(s, task, policy)

    assert dec.chosen[0].deploy_id == "p2:f2"        # served: the observed winner
    assert dec.shadow_chosen == "p1:f1"              # would-be: the dossier pick
    assert dec.chosen[0].p_lb == dec.chosen[0].p_lb_evidence  # never applied
    s.close()


def test_dossier_report_scores_the_shadow_pick(tmp_path, capsys):
    import types

    from mininfer.cli import cmd_dossier_report

    s = _seed(tmp_path)
    # shadow arm (p1) is perfect; served arm (p2) is 50% on the same task.
    s.observe("p1:f1", "t", True, ts="2026-01-01T00:00:00+00:00")
    s.observe("p1:f1", "t", True, ts="2026-01-01T00:00:01+00:00")
    s.observe("p2:f2", "t", True, ts="2026-01-01T00:00:02+00:00")
    s.observe("p2:f2", "t", False, ts="2026-01-01T00:00:03+00:00")
    s.record_decision(
        task="t", policy="p", mode="auto", chosen="p2:f2",
        candidates=["p2:f2", "p1:f1"],
        reason={"selected": [{"deploy_id": "p2:f2",
                               "dossier": {"reason": "best_for", "fit": 0.4}}],
                "shadow_chosen": "p1:f1"})
    s.commit()
    s.close()

    rc = cmd_dossier_report(types.SimpleNamespace(
        limit=50, json=False, db=str(tmp_path / "d.db")))
    out = capsys.readouterr().out
    assert rc == 0
    assert "shadow counterfactuals:   1" in out
    assert "shadow better: 1 (100.0%)" in out
    assert "aligned" in out
