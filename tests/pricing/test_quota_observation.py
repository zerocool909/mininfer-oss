"""P5 — quota intelligence: observed limits, 429 reasons, and the effective min.

Three properties, and each one is a bug the naive version has:

* **The effective headroom is `min(configured, observed)`.** A policy of 1000/day
  would spend a provider's whole observed 50/day free tier; a provider's observed
  20/min would be ignored whenever the policy was more generous. Either side alone
  gets one of those wrong.
* **A `429` does not mean "out of quota".** It can be a request rate, a token rate,
  concurrency or an account ceiling, and `unknown` is a usable answer — guessing
  `rpm` for all of them is how a token limit gets "fixed" by lowering RPM.
* **Exhaustion is an estimate, not a fact.** It travels with its basis and a
  confidence built from the number of observations behind it.
"""
from __future__ import annotations

import datetime as dt

import pytest

from mininfer import quota
from mininfer.schema import Deployment, Weights
from mininfer.store import Store

NOW = dt.datetime(2026, 10, 2, 0, 0, 30, tzinfo=dt.timezone.utc)


def _bucket(**over) -> dict:
    row = {"window": "minute", "limit_n": None, "observed_limit_n": 20,
           "observed_remaining_n": 3, "used_n": 15,
           "observed_reset_at": "2026-10-02T00:01:00+00:00", "reset_at": None}
    row.update(over)
    return row


# ------------------------------------------------------------- header parsing


def test_a_groq_style_family_parses_both_metrics():
    got = {r.metric: r for r in quota.parse_rate_limit_headers({
        "x-ratelimit-limit-requests": "20",
        "x-ratelimit-remaining-requests": "7",
        "x-ratelimit-reset-requests": "45s",
        "x-ratelimit-limit-tokens": "6000",
        "x-ratelimit-remaining-tokens": "1200",
        "x-ratelimit-reset-tokens": "12s",
    })}
    assert set(got) == {"requests", "tokens"}
    assert (got["requests"].limit_n, got["requests"].remaining_n) == (20, 7)
    assert got["requests"].reset_s == pytest.approx(45.0)
    assert (got["tokens"].limit_n, got["tokens"].remaining_n) == (6000, 1200)


def test_an_openrouter_style_bare_family_parses():
    got = quota.parse_rate_limit_headers({
        "x-ratelimit-limit": "60", "x-ratelimit-remaining": "59",
        "x-ratelimit-reset": "10",
    })
    assert len(got) == 1
    assert got[0].metric == "requests"
    assert (got[0].limit_n, got[0].remaining_n) == (60, 59)


def test_an_anthropic_style_family_parses():
    got = {r.metric: r for r in quota.parse_rate_limit_headers({
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-requests-remaining": "49",
        "anthropic-ratelimit-requests-reset": "2026-10-02T00:01:00Z",
    })}
    assert got["requests"].limit_n == 50


def test_a_compound_duration_parses():
    """Groq reports `2m59.56s`, which is not a number any parser gets by accident."""
    assert quota.duration_s("2m59.56s") == pytest.approx(179.56)
    assert quota.duration_s("45") == pytest.approx(45.0)
    assert quota.duration_s("0.5") == pytest.approx(0.5)
    assert quota.duration_s("3h") == pytest.approx(10800.0)


def test_an_http_date_retry_after_parses():
    """`retry-after` may be a date, not a delta. `duration_s` needs `now` to
    convert it, which is why it takes one."""
    when = NOW + dt.timedelta(seconds=120)
    got = quota.duration_s(when.strftime("%a, %d %b %Y %H:%M:%S GMT"), now=NOW)
    assert got == pytest.approx(120.0, abs=1.0)


def test_a_value_that_is_not_a_duration_is_unknown():
    for bad in (None, "", "soon", "later-ish"):
        assert quota.duration_s(bad, now=NOW) is None


def test_retry_after_attaches_to_every_metric():
    got = quota.parse_rate_limit_headers({
        "x-ratelimit-limit-requests": "20", "x-ratelimit-remaining-requests": "0",
        "retry-after": "30",
    })
    assert got and all(r.retry_after_s == pytest.approx(30.0) for r in got)


def test_a_bare_retry_after_still_produces_an_observation():
    """No limit family, but a back-off is still evidence — and it is the whole
    reason the classification exists."""
    got = quota.parse_rate_limit_headers({"retry-after": "30"})
    assert len(got) == 1
    assert got[0].remaining_n is None and got[0].retry_after_s == pytest.approx(30.0)


def test_a_family_reporting_nothing_is_skipped():
    assert quota.parse_rate_limit_headers({}) == ()
    assert quota.parse_rate_limit_headers({"content-type": "application/json"}) == ()


def test_a_metric_gets_one_vote_when_two_families_answer():
    """A provider sending both the specific and the bare header is reported once,
    or the same limit would be counted twice."""
    got = quota.parse_rate_limit_headers({
        "x-ratelimit-limit-requests": "20", "x-ratelimit-remaining-requests": "5",
        "x-ratelimit-limit": "999", "x-ratelimit-remaining": "999",
    })
    assert len(got) == 1
    assert got[0].limit_n == 20          # the more specific family wins


@pytest.mark.parametrize("reset_s,expected", [
    (45.0, "minute"), (None, "minute"), (120.0, "minute"),
    (600.0, "day"), (86_400.0, "day"), (26 * 3600.0, "day"),
    (30 * 86_400.0, "month"),
])
def test_the_window_is_inferred_from_the_reset(reset_s, expected):
    assert quota.window_for_reset(reset_s) == expected


# ------------------------------------------------------------ 429 reasons


def test_a_request_limit_with_a_short_reset_is_rpm():
    assert quota.classify_429({"x-ratelimit-remaining-requests": "0",
                               "x-ratelimit-reset-requests": "45s"}) == "rpm"


def test_a_request_limit_with_a_long_reset_is_rpd():
    assert quota.classify_429({"x-ratelimit-remaining-requests": "0",
                               "x-ratelimit-reset-requests": "3h"}) == "rpd"


def test_a_token_limit_is_tpm():
    assert quota.classify_429({"x-ratelimit-remaining-tokens": "0",
                               "x-ratelimit-limit-tokens": "6000"}) == "tpm"


def test_concurrency_is_read_from_the_body():
    assert quota.classify_429({}, "Concurrency limit reached for requests") == "concurrency"


def test_an_account_ceiling_is_read_from_the_body():
    assert quota.classify_429({}, "You exceeded your current quota, check billing") == "account"
    assert quota.classify_429({}, "insufficient credits") == "account"


def test_remaining_above_zero_is_not_a_rate_limit_hit():
    """A 429 with headroom left is something else, and saying `rpm` would send the
    operator to fix a limit that was not the problem."""
    assert quota.classify_429({"x-ratelimit-remaining-requests": "7"}) == "unknown"


def test_no_evidence_is_unknown_not_a_guess():
    for headers, body in (({}, None), ({"retry-after": "30"}, None), ({}, "too many requests")):
        assert quota.classify_429(headers, body) == "unknown"


def test_every_reason_is_a_declared_reason():
    for headers, body in (({"x-ratelimit-remaining-requests": "0"}, None),
                          ({}, "concurrency"), ({}, "quota"), ({}, None)):
        assert quota.classify_429(headers, body) in quota.RATE_LIMIT_REASONS


# ------------------------------------------------------- effective headroom


def test_the_provider_limit_overrides_a_generous_policy(tmp_path):
    """The rule P5 exists for: a policy must not spend a provider's whole free
    tier. Configured 1000/day, provider 20/min with 3 left -> 15%."""
    s = Store(tmp_path / "t.db")
    s.set_limit("groq:llama", "day", 1000)
    s.commit()
    assert s.headroom("groq:llama") == (1.0, "configured")

    quota.record_from_headers(s, "groq:llama", {
        "x-ratelimit-limit-requests": "20", "x-ratelimit-remaining-requests": "3",
        "x-ratelimit-reset-requests": "45s"}, observed_at="2026-10-02T00:00:00+00:00")
    s.commit()
    head, src = s.headroom("groq:llama")
    assert head == pytest.approx(0.15)
    assert src == "observed"
    s.close()


def test_the_policy_limit_overrides_a_generous_provider(tmp_path):
    """The mirror case: the provider says plenty, the policy says stop."""
    s = Store(tmp_path / "t.db")
    s.set_limit("groq:llama", "minute", 10)
    s.record_usage("groq:llama", window="minute", n=8)
    quota.record_from_headers(s, "groq:llama", {
        "x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "990",
        "x-ratelimit-reset-requests": "45s"}, observed_at="2026-10-02T00:00:00+00:00")
    s.commit()
    head, src = s.headroom("groq:llama")
    assert head == pytest.approx(0.2)      # the policy side, 2 of 10 left
    assert src == "hybrid"
    s.close()


def test_neither_side_known_is_unknown(tmp_path):
    """Unknown is not 'plenty of room' — the router treats it conservatively."""
    s = Store(tmp_path / "t.db")
    assert s.headroom("nope") == (None, "unknown")
    s.close()


def test_an_observed_only_bucket_is_a_bucket(tmp_path):
    """A provider that reports a limit we never configured still gets one."""
    s = Store(tmp_path / "t.db")
    quota.record_from_headers(s, "x:m", {
        "x-ratelimit-limit-requests": "20", "x-ratelimit-remaining-requests": "20",
        "x-ratelimit-reset-requests": "60s"}, observed_at="2026-10-02T00:00:00+00:00")
    s.commit()
    row = s.conn.execute(
        "SELECT limit_n, observed_limit_n FROM quota_buckets WHERE deploy_id='x:m'").fetchone()
    assert row["limit_n"] is None            # never configured
    assert row["observed_limit_n"] == 20
    assert s.headroom("x:m") == (1.0, "observed")
    s.close()


def test_recording_a_limit_never_writes_the_configured_one(tmp_path):
    """An observation is not a policy: the provider's number must not overwrite a
    ceiling a human set."""
    s = Store(tmp_path / "t.db")
    s.set_limit("d:1", "minute", 5)
    s.record_rate_limit("d:1", window="minute", limit_n=1000, remaining_n=999,
                        observed_at="2026-10-02T00:00:00+00:00")
    s.commit()
    row = s.conn.execute(
        "SELECT limit_n, observed_limit_n FROM quota_buckets WHERE deploy_id='d:1'").fetchone()
    assert row["limit_n"] == 5 and row["observed_limit_n"] == 1000
    s.close()


def test_remaining_is_stored_exactly_as_reported(tmp_path):
    """Clamping would hide a misconfigured bucket behind a plausible number."""
    s = Store(tmp_path / "t.db")
    s.record_rate_limit("d:1", window="minute", limit_n=20, remaining_n=-3,
                        observed_at="2026-10-02T00:00:00+00:00")
    s.commit()
    assert s.conn.execute(
        "SELECT observed_remaining_n r FROM quota_buckets WHERE deploy_id='d:1'"
    ).fetchone()["r"] == -3
    s.close()


# ------------------------------------------------------- exhaustion projection


def test_exhaustion_is_projected_with_a_basis_and_a_confidence():
    ex = quota.project_exhaustion(_bucket(), now=NOW)
    # 15 calls in 30s -> 0.5/s; 3 left -> 6s from now.
    assert ex["estimated_exhaustion_at"] == "2026-10-02T00:00:36+00:00"
    assert ex["confidence"] == pytest.approx(1.0)
    assert ex["basis"]["observed_rate_per_s"] == pytest.approx(0.5)
    assert ex["basis"]["remaining"] == 3


def test_confidence_rises_with_the_number_of_observations():
    """A rate from two calls is a coincidence, and the number says so."""
    assert quota.project_exhaustion(_bucket(used_n=1), now=NOW)["confidence"] == pytest.approx(0.1)
    assert quota.project_exhaustion(_bucket(used_n=5), now=NOW)["confidence"] == pytest.approx(0.5)
    assert quota.project_exhaustion(_bucket(used_n=50), now=NOW)["confidence"] == 1.0


def test_no_estimate_when_the_window_refills_first():
    """Predicting exhaustion past the reset would be predicting a bucket that
    refills before it empties."""
    ex = quota.project_exhaustion(_bucket(observed_remaining_n=100), now=NOW)
    assert ex["estimated_exhaustion_at"] is None
    assert ex["basis"]["remaining"] == 100


def test_an_empty_bucket_estimates_now():
    ex = quota.project_exhaustion(_bucket(observed_remaining_n=0), now=NOW)
    assert ex["estimated_exhaustion_at"] == NOW.isoformat(timespec="seconds")


def test_no_rate_no_estimate():
    ex = quota.project_exhaustion(_bucket(used_n=0), now=NOW)
    assert ex["estimated_exhaustion_at"] is None
    assert ex["confidence"] == 0.0


def test_a_bucket_with_no_limit_at_all_has_no_estimate():
    ex = quota.project_exhaustion({"window": "minute", "limit_n": None,
                                   "observed_limit_n": None, "observed_remaining_n": None,
                                   "used_n": 0, "observed_reset_at": None, "reset_at": None},
                                  now=NOW)
    assert ex["estimated_exhaustion_at"] is None
    assert ex["confidence"] == 0.0


# --------------------------------------------------------------- the view


def test_describe_orders_by_effective_headroom_and_reports_the_source(tmp_path):
    s = Store(tmp_path / "t.db")
    # A: generous policy, but the provider says nearly empty.
    s.set_limit("a:m", "minute", 1000)
    quota.record_from_headers(s, "a:m", {
        "x-ratelimit-limit-requests": "20", "x-ratelimit-remaining-requests": "1",
        "x-ratelimit-reset-requests": "60s"}, observed_at="2026-10-02T00:00:00+00:00")
    # B: a tight policy, plenty observed.
    s.set_limit("b:m", "minute", 10)
    s.record_usage("b:m", window="minute", n=5)
    s.commit()

    rows = quota.describe(s, limit=10)
    assert rows[0]["deploy_id"] == "a:m"          # 5% beats 50%
    assert rows[0]["headroom"] == pytest.approx(0.05)
    # Both limits are known, so the source is `hybrid` — and the *observed* side
    # is the one that set the value, which the number itself shows against the
    # configured limit the CLI prints beside it.
    assert rows[0]["headroom_source"] == "hybrid"
    assert rows[0]["limit_n"] == 1000 and rows[0]["observed_limit_n"] == 20
    assert rows[1]["headroom_source"] == "configured"
    s.close()


def test_a_429_reason_is_recorded_on_the_observation(tmp_path):
    s = Store(tmp_path / "t.db")
    s.observe("d:1", "t", ok=False, ts="2026-10-02T00:00:00+00:00", error_class="429",
              rate_limit_reason=quota.classify_429(
                  {"x-ratelimit-remaining-requests": "0",
                   "x-ratelimit-reset-requests": "45s"}))
    s.commit()
    assert s.conn.execute(
        "SELECT rate_limit_reason r FROM observations WHERE deploy_id='d:1'"
    ).fetchone()["r"] == "rpm"
    s.close()


def test_the_router_ranks_on_the_observed_headroom(tmp_path):
    """The point of the whole phase: an arm the provider has nearly exhausted must
    demote, even though the configured policy would have let it run."""
    from mininfer.router import Policy, build_candidates
    from mininfer.schema import TaskProfile

    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "w", benchmark={"coding": 80.0}))
    s.upsert_deployment(Deployment("prov:free", "w", "prov", "free",
                                   price_in=0.0, price_out=0.0, zero_price=True))
    s.upsert_deployment(Deployment("paid:p", "w", "paid", "p",
                                   price_in=1.0, price_out=1.0))
    # The policy is generous; the provider says one call left.
    s.set_limit("prov:free", "minute", 1000)
    quota.record_from_headers(s, "prov:free", {
        "x-ratelimit-limit-requests": "20", "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-reset-requests": "60s"}, observed_at="2026-10-02T00:00:00+00:00")
    s.commit()

    task = TaskProfile("t", tokens_in=1000, tokens_out=100,
                       benchmark_keys=("coding",), min_success_lb=0.0)
    free = next(c for c in build_candidates(s, task, Policy()) if c.deploy_id == "prov:free")
    paid = next(c for c in build_candidates(s, task, Policy()) if c.deploy_id == "paid:p")
    assert free.headroom == pytest.approx(0.0)
    assert free.headroom_exhausted is True
    assert free.cost_per_call == pytest.approx(paid.cost_per_call)
    s.close()
