"""Comparison mode: which two arms get offered, and what a broken free arm costs.

Two properties, both of which were violated in production:

1. **Free first.** `rank_for_compare` explores with UCB1 so an untried model gets
   offered. UCB1's bonus is largest at zero observations, and a paid arm almost
   always has zero — so an unqualified bonus handed arm 1 to `claude-opus-5.5`
   ($0.034/call) while 55 free arms sat unused. Exploration orders *within* the
   free tier; it never overrides the cost rule.

2. **A broken free arm is not free.** `cost_per_success` for a free arm is
   `cost_per_call * calls`, and `cost_per_call` is zero, so it is identically
   zero however often the arm fails. Only `429` and `timeout` fed the exhaustion
   estimate, so an arm that 404s every single time kept ranking first — which is
   how `google:gemini-2.0-flash` was offered again after it had already failed.
"""
from __future__ import annotations

import pytest

from mininfer.router import (
    Policy,
    _compare_tier,
    build_candidates,
    is_free_arm,
    rank_for_compare,
)
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store

TASK = TaskProfile("t", tokens_in=1000, tokens_out=100, benchmark_keys=("coding",))


def _seed(tmp_path, rows):
    """`rows` is (provider, price_in, price_out, free_tier) per arm.

    Distinct providers so `_distinct_options` can see them as independent, and a
    benchmark so the arms clear the quality floor without special-casing.
    """
    s = Store(tmp_path / "c.db")
    for i, (provider, pin, pout, free) in enumerate(rows):
        wid = f"hf:{provider}/m{i}"
        s.upsert_weights(Weights(wid, f"m{i}", benchmark={"coding": 60.0}))
        s.upsert_deployment(Deployment(
            f"{provider}:m{i}", wid, provider, f"m{i}",
            price_in=pin, price_out=pout, zero_price=free, context_window=1000))
    s.commit()
    return s


def _cands(store, policy=None):
    return build_candidates(store, TASK, policy or Policy())


# ------------------------------------------------------------- free first


def test_exploration_stays_inside_the_free_tier(tmp_path):
    """With free arms available, neither arm of the pair may be a paid one."""
    s = _seed(tmp_path, [("prov", 0.0, 0.0, True),
                         ("other", 0.0, 0.0, True),
                         ("expensive", 5.0, 5.0, False)])
    ordered = rank_for_compare(_cands(s))
    assert is_free_arm(ordered[0]), ordered[0].deploy_id
    assert is_free_arm(ordered[1]), ordered[1].deploy_id
    s.close()


def test_a_never_tried_paid_arm_does_not_displace_a_free_one(tmp_path):
    """The exact production shape: 55 free arms sit behind an untried paid one.

    UCB1 alone prefers the paid arm — it has zero observations and therefore the
    maximum bonus, while the free alternative has a track record and so a smaller
    one. This asserts the premise first, so the test fails loudly if that ever
    stops being true, and then asserts the cost class wins anyway.
    """
    from mininfer.router import ucb_score

    s = _seed(tmp_path, [("prov", 0.0, 0.0, True),
                         ("other", 0.0, 0.0, True),
                         ("expensive", 5.0, 5.0, False)])
    # The current winner, and a free alternative with a little history.
    s.observe("prov:m0", "t", ok=True, ts="2026-01-01T00:00:00+00:00")
    for ok in (True, True, False):
        s.observe("other:m1", "t", ok=ok, ts="2026-01-01T00:00:00+00:00")
    s.commit()

    cands = _cands(s)
    free_alt = next(c for c in cands if c.deploy_id == "other:m1")
    paid = next(c for c in cands if c.deploy_id == "expensive:m2")
    total = sum(c.n_obs for c in cands)
    assert ucb_score(paid, total) > ucb_score(free_alt, total), \
        "premise broken: UCB1 no longer prefers the untried paid arm"

    ordered = rank_for_compare(cands)
    assert ordered[0].deploy_id == "prov:m0"
    # The comparison arm must have a free tier; whether it is tier 0 or tier 1
    # depends on its own failure history, which is a separate question.
    assert _compare_tier(ordered[1]) < 2, \
        f"paid-only arm displaced a free-tier one: {ordered[1].deploy_id}"
    s.close()


def test_paid_arms_are_still_reachable_when_nothing_is_free(tmp_path):
    """The rule is a preference, not a ban: with no free arm, compare paid ones."""
    s = _seed(tmp_path, [("a", 1.0, 1.0, False), ("b", 2.0, 2.0, False)])
    ordered = rank_for_compare(_cands(s))
    assert len(ordered) == 2
    assert not any(is_free_arm(c) for c in ordered)
    s.close()


def test_a_single_candidate_is_returned_unchanged(tmp_path):
    s = _seed(tmp_path, [("prov", 0.0, 0.0, True)])
    ordered = rank_for_compare(_cands(s))
    assert [c.deploy_id for c in ordered] == ["prov:m0"]
    s.close()


# ------------------------------------------------------- broken free arms


def test_a_free_arm_that_hard_fails_is_charged_the_paid_floor(tmp_path):
    """A 404 is not a quota signal, but it still costs the paid fallback."""
    s = _seed(tmp_path, [("prov", 0.0, 0.0, True), ("paid", 1.0, 1.0, False)])
    for _ in range(4):
        s.observe("prov:m0", "t", ok=False, ts="2026-01-01T00:00:00+00:00",
                  error_class="http_404")
    s.commit()

    cands = _cands(s)
    free_c = next(c for c in cands if c.deploy_id == "prov:m0")
    paid_c = next(c for c in cands if c.deploy_id == "paid:m1")

    assert free_c.error_rate == 1.0          # n - wins, no new column needed
    assert free_c.quota_risk == 1.0
    assert free_c.cost_per_call == pytest.approx(paid_c.cost_per_call)
    assert free_c.cost_per_call > 0, "a broken free arm must not cost zero"
    s.close()


def test_a_429_alone_still_drives_the_same_estimate(tmp_path):
    """The original path must not regress while adding the general one."""
    s = _seed(tmp_path, [("prov", 0.0, 0.0, True), ("paid", 1.0, 1.0, False)])
    s.observe("prov:m0", "t", ok=False, ts="2026-01-01T00:00:00+00:00",
              error_class="429")
    s.commit()
    free_c = next(c for c in _cands(s) if c.deploy_id == "prov:m0")
    assert free_c.rate_429 == 1.0
    assert free_c.cost_per_call > 0
    s.close()


def test_a_healthy_free_arm_stays_free(tmp_path):
    """The regression this could most easily cause: everything looks paid."""
    s = _seed(tmp_path, [("prov", 0.0, 0.0, True)])
    for _ in range(3):
        s.observe("prov:m0", "t", ok=True, ts="2026-01-01T00:00:00+00:00")
    s.commit()
    free_c = next(c for c in _cands(s) if c.deploy_id == "prov:m0")
    assert free_c.error_rate == 0.0
    assert free_c.cost_per_call == 0.0
    assert is_free_arm(free_c)
    s.close()


def test_the_funnel_and_the_ranking_agree_on_free(tmp_path):
    """`free_eligible` is what an operator reads; it must use the same predicate."""
    s = _seed(tmp_path, [("prov", 0.0, 0.0, True), ("paid", 1.0, 1.0, False)])
    from mininfer.router import route
    dec = route(s, TASK, Policy())
    eligible = [c for c in dec.ranked if c.rejected is None]
    assert dec.funnel["free_eligible"] == sum(1 for c in eligible if is_free_arm(c))
    s.close()


def test_a_floored_free_arm_outranks_an_always_paid_one(tmp_path):
    """Tier 1 sits between the two: worse than free, better than paid-only.

    A free arm with a bad failure rate is priced at the floor, so it is no longer
    tier 0 — but it is still the cheaper, more comparable choice than an arm that
    is paid by nature.
    """
    s = _seed(tmp_path, [("prov", 0.0, 0.0, True),
                         ("flaky", 0.0, 0.0, True),
                         ("expensive", 5.0, 5.0, False)])
    for _ in range(4):
        s.observe("flaky:m1", "t", ok=False, ts="2026-01-01T00:00:00+00:00",
                  error_class="http_500")
    s.commit()

    cands = _cands(s)
    flaky = next(c for c in cands if c.deploy_id == "flaky:m1")
    assert flaky.cost_per_call > 0, "a failing free arm must be priced at the floor"

    ordered = rank_for_compare(cands)
    assert ordered[0].deploy_id == "prov:m0"
    assert ordered[1].deploy_id != "expensive:m2", \
        "an always-paid arm must not outrank a floored free-tier arm"
    s.close()


def test_tiers_are_ordered_free_then_floored_then_paid(tmp_path):
    s = _seed(tmp_path, [("prov", 0.0, 0.0, True),
                         ("flaky", 0.0, 0.0, True),
                         ("expensive", 5.0, 5.0, False)])
    for _ in range(4):
        s.observe("flaky:m1", "t", ok=False, ts="2026-01-01T00:00:00+00:00",
                  error_class="http_404")
    s.commit()
    cands = {c.deploy_id: c for c in _cands(s)}
    assert _compare_tier(cands["prov:m0"]) == 0
    assert _compare_tier(cands["flaky:m1"]) == 1
    assert _compare_tier(cands["expensive:m2"]) == 2
    s.close()


# ------------------------------------------- a failing arm is replaced (stream)


class _FakeSession:
    def __init__(self, status_code, chunks, error_class=None):
        self.status_code = status_code
        self.error_class = error_class
        self.headers = {}
        self._chunks = chunks

    def chunks(self):
        return iter(self._chunks)

    def close(self):
        pass


def _sse_text(text: str) -> bytes:
    import json as _json
    frame = {"choices": [{"index": 0, "delta": {"content": text}}]}
    return f"data: {_json.dumps(frame)}\n\n".encode()


def test_a_failing_arm_is_replaced_by_a_spare(tmp_path, monkeypatch):
    """The chat window streams, so it got exactly the arms it asked for.

    A streamed comparison ran `n_options` arms and no more, which made it exactly
    as reliable as its first choice: one 404 and the caller was left with a single
    answer and an error, having asked for a choice. The spare takes over the
    failed arm's index, so the caller still gets the number it asked for.
    """
    import asyncio
    import json
    import sys

    sys.path.insert(0, ".")
    import mininfer.proxy as proxy

    real_resolve = proxy.resolve_endpoint
    real_open = proxy.open_stream

    def fake_resolve(did, **kw):
        ep = real_resolve(did, **kw)
        # Every arm resolves, including the invented provider names, so the test
        # exercises the multiplexer rather than endpoint configuration.
        ep.error = None
        ep.api_key = "k"
        ep.model = did
        return ep

    def fake_open(endpoint, body, **kw):
        did = kw.get("deploy_id", "")
        if did.startswith("bad:"):
            raise proxy._ArmFailed("http_404")
        return _FakeSession(200, [_sse_text("ok")])

    monkeypatch.setattr(proxy, "resolve_endpoint", fake_resolve)
    monkeypatch.setattr(proxy, "open_stream", fake_open)
    monkeypatch.setenv("MI_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")

    opts = ["bad:first", "good:one", "good:two"]
    info = {d: {"deploy_id": d} for d in opts}

    async def run():
        resp = await proxy._multiplex_stream(
            opts, {"messages": [{"role": "user", "content": "hi"}]},
            task_name="general_chat", policy="free_first", reason={}, info=info,
            extra={}, max_arms=2)
        events = []
        async for chunk in resp.body_iterator:
            for line in chunk.splitlines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    continue
                m = (json.loads(payload).get("mi") or {})
                events.append((m.get("event"), m.get("arm"),
                               m.get("deploy_id"), m.get("error"), m.get("replaces")))
        return events

    events = asyncio.run(run())

    # arm 0's first choice failed...
    assert ("error", 0, None, "http_404", None) not in events, \
        f"the failure was surfaced instead of replaced: {events}"
    # ...and a spare took over its index.
    assert any(e[0] == "arm" and e[1] == 0 and e[4] is True for e in events), events
    assert sum(1 for e in events if e[0] == "end") == 2, events
    assert not any(e[0] == "error" for e in events), events
