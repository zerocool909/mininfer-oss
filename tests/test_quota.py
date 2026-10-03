"""Tests for Phase 3: declared quota limits, live usage, and demotion.

The invariant that matters: a free arm costs zero only while its bucket has
headroom. Without this the router happily sends everything to a drained free
endpoint that returns 429 forever (PLAN.md §2).
"""
from __future__ import annotations

import datetime as dt

import pytest

from mininfer import quota
from mininfer.proxy import try_fallbacks
from mininfer.router import Policy, build_candidates
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store


# --------------------------------------------------------------- window math


def test_next_reset_advances_utc():
    now = dt.datetime(2026, 9, 26, 10, 30, 15, tzinfo=dt.timezone.utc)
    assert Store.__init__  # module loads
    from mininfer.store import _next_reset

    assert _next_reset("minute", now).startswith("2026-09-26T10:31:00")
    assert _next_reset("day", now).startswith("2026-09-27T00:00:00")
    assert _next_reset("month", now).startswith("2026-10-01T00:00:00")


# --------------------------------------------------------------- limits/usage


def test_headroom_none_when_unconfigured(tmp_path):
    s = Store(tmp_path / "t.db")
    assert s.headroom("nope") == (None, "unknown")
    s.close()


def test_set_limit_and_headroom(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set_limit("d:1", "day", 100)
    head, src = s.headroom("d:1")
    assert head == 1.0 and src == "configured"
    s.record_usage("d:1", n=25)
    head, _ = s.headroom("d:1")
    assert head == pytest.approx(0.75)
    s.close()


def test_headroom_is_tightest_bucket(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set_limit("d:1", "minute", 10)
    s.set_limit("d:1", "day", 1000)
    s.record_usage("d:1", window="minute", n=9)  # minute now 10% left
    head, _ = s.headroom("d:1")
    assert head == pytest.approx(0.1)
    s.close()


def test_reseeding_limits_preserves_usage(tmp_path):
    """A `mi quota seed` must never refill a drained bucket."""
    s = Store(tmp_path / "t.db")
    s.set_limit("d:1", "day", 10)
    s.record_usage("d:1", n=4)
    s.set_limit("d:1", "day", 20)  # re-seed a larger limit
    used = s.conn.execute(
        "SELECT used_n FROM quota_buckets WHERE deploy_id='d:1'").fetchone()["used_n"]
    assert used == 4
    s.close()


def test_expired_window_rolls_over(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set_limit("d:1", "day", 10, reset_at="2020-01-01T00:00:00+00:00")
    s.conn.execute("UPDATE quota_buckets SET used_n=10 WHERE deploy_id='d:1'")
    s.commit()
    head, _ = s.headroom("d:1")  # checks expiry, rolls over
    assert head == 1.0
    s.close()


def test_exhaust_zeroes_headroom(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set_limit("d:1", "minute", 10)
    s.record_usage("d:1", n=3)
    assert s.exhaust("d:1", window="minute") == 1
    head, _ = s.headroom("d:1")
    assert head == 0.0
    s.close()


def test_exhaust_ignores_unknown_limits(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set_bucket("d:1", "day", None)  # limit unknown
    assert s.exhaust("d:1") == 0
    s.close()


# --------------------------------------------------------------- seeding


def test_seed_expands_provider_and_free_only(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("slug:m", "m"))
    s.upsert_deployment(Deployment("openrouter:free/x:free", "slug:m", "openrouter",
                                   "free/x:free", zero_price=True, free_variant=True))
    s.upsert_deployment(Deployment("openrouter:paid/y", "slug:m", "openrouter",
                                   "paid/y", price_in=1.0, price_out=1.0))
    s.upsert_deployment(Deployment("groq:qwen", "slug:m", "groq", "qwen"))
    s.commit()

    rep = quota.seed(s, [
        {"provider": "groq", "window": "minute", "limit": 30},
        {"provider": "openrouter", "free_only": True, "window": "day", "limit": 50},
    ])
    assert rep["buckets"] == 2  # one groq deploy, one *free* openrouter deploy
    rows = {r["deploy_id"] for r in s.conn.execute("SELECT deploy_id FROM quota_buckets")}
    assert rows == {"groq:qwen", "openrouter:free/x:free"}
    assert quota.describe(s)[0]["headroom"] == 1.0
    s.close()


def test_seed_dry_run_writes_nothing(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("slug:m", "m"))
    s.upsert_deployment(Deployment("groq:qwen", "slug:m", "groq", "qwen"))
    s.commit()
    quota.seed(s, [{"provider": "groq", "window": "minute", "limit": 30}], dry_run=True)
    assert s.conn.execute("SELECT COUNT(*) c FROM quota_buckets").fetchone()["c"] == 0
    s.close()


# --------------------------------------------------------------- demotion


def _seed_two_arms(s: Store) -> None:
    s.upsert_weights(Weights("slug:m", "m", benchmark={"coding": 80.0}))
    s.upsert_deployment(Deployment("prov:free", "slug:m", "prov", "free",
                                   price_in=0.0, price_out=0.0, zero_price=True))
    s.upsert_deployment(Deployment("paid:p", "slug:m", "paid", "p",
                                   price_in=1.0, price_out=1.0))
    s.commit()


def test_drained_free_arm_is_charged_the_paid_floor(tmp_path):
    s = Store(tmp_path / "t.db")
    _seed_two_arms(s)
    s.set_limit("prov:free", "day", 10)
    s.conn.execute("UPDATE quota_buckets SET used_n=10 WHERE deploy_id='prov:free'")
    s.commit()

    task = TaskProfile("t", tokens_in=1000, tokens_out=100,
                       benchmark_keys=("coding",), min_success_lb=0.0)
    cands = build_candidates(s, task, Policy())
    free_c = next(c for c in cands if c.deploy_id == "prov:free")
    paid_c = next(c for c in cands if c.deploy_id == "paid:p")

    assert free_c.headroom_exhausted is True
    assert free_c.quota_risk == 1.0
    # drained -> costs the paid alternative, not zero, so it stops winning
    assert free_c.cost_per_call == pytest.approx(paid_c.cost_per_call)
    assert free_c.cost_per_call > 0
    s.close()


def test_fresh_free_arm_is_free(tmp_path):
    s = Store(tmp_path / "t.db")
    _seed_two_arms(s)
    s.set_limit("prov:free", "day", 1000)  # plenty of headroom
    s.commit()
    task = TaskProfile("t", tokens_in=1000, tokens_out=100, benchmark_keys=("coding",))
    free_c = next(c for c in build_candidates(s, task, Policy())
                  if c.deploy_id == "prov:free")
    assert free_c.cost_per_call == 0.0 and free_c.quota_risk == 0.0
    s.close()


# --------------------------------------------------------------- proxy wiring


def _runner_once(result):
    def factory(**kw):
        return lambda deploy_id, messages, **kw2: result
    return factory


def test_proxy_charges_quota_on_success(tmp_path):
    from mininfer.execute import CallResult

    s = Store(tmp_path / "t.db")
    s.set_limit("a:1", "day", 10)
    s.commit()
    runner = _runner_once(CallResult("a:1", text="ok", ok=True,
                                     tokens_in=5, tokens_out=3))()
    try_fallbacks(["a:1"], [{"role": "user", "content": "hi"}], runner,
                  task_name="t", store=s)
    used = s.conn.execute(
        'SELECT used_n FROM quota_buckets WHERE deploy_id=\'a:1\' AND "window"=\'day\''
    ).fetchone()["used_n"]
    assert used == 1
    s.close()


def test_proxy_429_exhausts_minute_bucket(tmp_path):
    from mininfer.execute import CallResult

    s = Store(tmp_path / "t.db")
    s.set_limit("a:1", "minute", 10)
    s.commit()
    runner = _runner_once(CallResult("a:1", error_class="429"))()
    try_fallbacks(["a:1"], [{"role": "user", "content": "hi"}], runner,
                  task_name="t", store=s)
    head, _ = s.headroom("a:1")
    assert head == 0.0
    s.close()
