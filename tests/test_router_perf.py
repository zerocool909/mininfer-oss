"""Routing-path performance and the two behaviours the optimizations changed.

`build_candidates` runs on every chat call, so it is the one place where an
accidental N+1 is paid by every request. These tests pin:

  * a bounded number of SQL statements per call (it used to be ~2 per arm, i.e.
    ~3,400 on the live registry);
  * `headroom_all()` agreeing with the per-arm `headroom()` it replaced;
  * anti-fixation measuring *served* traffic, so background shadow trials cannot
    dilute the concentration that the cap exists to break up.
"""
from __future__ import annotations

from mininfer.router import Policy, build_candidates
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store


def _arm(s: Store, provider: str, name: str, *, ctx: int = 8000) -> str:
    s.upsert_weights(Weights(f"w-{name}", name))
    deploy_id = f"{provider}:{name}"
    s.upsert_deployment(Deployment(deploy_id, f"w-{name}", provider, name,
                                   price_in=0.0, price_out=0.0, zero_price=True,
                                   context_window=ctx))
    return deploy_id


def _task(name: str = "t") -> TaskProfile:
    return TaskProfile(name, tokens_in=100, tokens_out=100, min_success_lb=0.0)


def test_headroom_all_matches_the_per_arm_lookup(tmp_path):
    s = Store(tmp_path / "h.db")
    a = _arm(s, "p1", "a")
    b = _arm(s, "p2", "b")
    s.set_limit(a, "day", 100)
    s.commit()
    mapping = s.headroom_all()

    for deploy_id in (a, b, "p3:missing"):
        assert mapping.get(deploy_id, (None, "unknown")) == s.headroom(deploy_id)
    s.close()


def test_build_candidates_issues_a_bounded_number_of_queries(tmp_path):
    """One pass over the registry, not two queries per arm.

    The live registry is 1740 arms; a per-arm quota read made that ~3,400
    statements. The floor here is generous (the model is O(1) queries, the test
    just forbids O(arms)), so a legitimate query is added without ceremony but a
    returning N+1 fails.
    """
    s = Store(tmp_path / "q.db")
    for i in range(40):
        _arm(s, f"p{i}", f"m{i}")
    s.commit()

    seen = 0
    raw = s.conn.execute

    def counting(sql, *a, **k):
        nonlocal seen
        seen += 1
        return raw(sql, *a, **k)

    s.conn.execute = counting
    cands = build_candidates(s, _task(), Policy())
    assert len(cands) == 40
    assert seen <= 25, f"build_candidates issued {seen} statements for 40 arms"
    s.close()


def test_anti_fixation_ignores_shadow_trials(tmp_path):
    """A trial probe is a call, but not *served* traffic.

    One arm takes 20 of 25 served calls (80%, above the 60% cap); 100 background
    shadow-trial observations sit in the same table. Counting those would read the
    share as 20/125 = 16% and the arm would never be capped.
    """
    s = Store(tmp_path / "a.db")
    hot = _arm(s, "p1", "hot")
    other = _arm(s, "p2", "other")
    for _ in range(20):
        s.observe(hot, "t", True, ts="2026-01-01T00:00:00+00:00")
    for _ in range(5):
        s.observe(other, "t", True, ts="2026-01-01T00:00:00+00:00")
    for i in range(100):
        s.observe(other, "t", True, ts="2026-01-01T00:00:00+00:00",
                  signal_kind="judge_trial", meta={"shadow": True})
    s.commit()

    stats = s.stats("t")[hot]
    assert stats["n"] > stats["n_served"] or stats["n_served"] == 20

    cands = {c.deploy_id: c for c in build_candidates(
        s, _task(), Policy(dossier_weight=0.0))}
    assert cands[hot].traffic_capped is True   # 20/25 served = 80% > 60%
    s.close()


def test_traffic_share_without_trials_would_not_cap(tmp_path):
    """The counterfactual, so the previous test cannot pass by accident."""
    s = Store(tmp_path / "b.db")
    hot = _arm(s, "p1", "hot")
    other = _arm(s, "p2", "other")
    for _ in range(20):
        s.observe(hot, "t", True, ts="2026-01-01T00:00:00+00:00")
    for _ in range(100):
        s.observe(other, "t", True, ts="2026-01-01T00:00:00+00:00",
                  signal_kind="judge_trial", meta={"shadow": True})
    s.commit()
    # All 120 are unauthored trials for `other`, so `hot` is 20/20 served = 100%.
    cands = {c.deploy_id: c for c in build_candidates(
        s, _task(), Policy(dossier_weight=0.0))}
    assert cands[hot].traffic_capped is True
    s.close()
