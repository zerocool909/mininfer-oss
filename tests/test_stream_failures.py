"""A failed stream must still produce a valid response.

The bug this pins was mine, introduced when the streaming path learned to record
rate-limit headers:

    reason = _record_rate_limits(...)      # `reason` was already a parameter

`_stream` takes a `reason` — the *routing* reason dict — and that assignment
overwrote it with a rate-limit string. The response headers then called
`reason.get("needs_approval")` on it and raised `AttributeError`, so a request
that recovered perfectly on its second arm returned **500 Internal Server Error**.
It was intermittent because it needed a first arm to fail *and* headers to be
built, which is why it survived the suite and surfaced under a live provider.

The two things named `reason` are unrelated: one explains why an arm was chosen,
the other why it was rate-limited. The test asserts both still work.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

import mininfer.proxy as proxy
from mininfer.store import Store

_POLICY = """\
policy:
  name: test
  top_k: 3
  require_distinct_provider: false
  objective: cost_per_success
  on_unverified_capability: allow
  require: {}
  min_context: 0
  min_success_lb: 0.0
  allow_unknown_price: true
  prior_strength: 20.0
  session_token_limit: 1000000
tasks:
  t:
    description: test task
    tokens_in: 10
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
"""


def _delta(text: str) -> bytes:
    return f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n".encode()


_DONE = b"data: [DONE]\n\n"


class _Sess:
    """A stream session, failing or succeeding."""

    def __init__(self, parts=(), *, status=200, error_class=None, headers=None,
                 error_detail=None):
        self.status_code = status
        self.error_class = error_class
        self.headers = headers or {}
        self.error_detail = error_detail
        self._parts = parts

    def chunks(self):
        yield from self._parts

    def close(self):
        pass


def _client(tmp_path, monkeypatch) -> TestClient:
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    store = Store(tmp_path / "p.db")
    store.conn.execute("INSERT INTO weights (weights_id, display_name) VALUES ('hf:m','m')")
    for did in ("openrouter:a", "openrouter:b"):
        store.conn.execute(
            "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
            f" price_in, price_out, context_window) VALUES ('{did}','hf:m',"
            "'openrouter','m',0.0,0.0,1000)")
    store.commit()
    store.close()
    return TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)


def test_a_rate_limited_first_arm_does_not_break_the_response(tmp_path, monkeypatch):
    """The bug: the request recovered on arm two, and still 500'd."""
    client = _client(tmp_path, monkeypatch)

    def fake_open(ep, body, *, deploy_id, timeout=120.0):
        if deploy_id == "openrouter:a":
            return _Sess(status=429, error_class="429",
                         headers={"x-ratelimit-limit-requests": "20",
                                  "x-ratelimit-remaining-requests": "0",
                                  "x-ratelimit-reset-requests": "45s"},
                         error_detail="rate limit exceeded")
        return _Sess([_delta("ok"), _DONE])

    monkeypatch.setattr(proxy, "open_stream", fake_open)
    r = client.post("/v1/chat/completions", headers={"X-MI-Session": "s1"},
                    json={"model": "t", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 200, r.text[:200]
    assert r.text.rstrip().endswith("[DONE]")
    # The header the crash happened on. It comes from the *routing* reason, which
    # must survive the rate-limit recording untouched.
    assert r.headers.get("X-MI-Needs-Approval") in ("true", "false")
    assert r.headers.get("X-MI-Deploy") == "openrouter:b"


def test_the_rate_limit_is_still_recorded_for_the_arm_that_failed(tmp_path, monkeypatch):
    """The fix must not trade one behaviour for the other: the point of the P5 code
    was to learn the provider's limit from a failure."""
    client = _client(tmp_path, monkeypatch)

    def fake_open(ep, body, *, deploy_id, timeout=120.0):
        if deploy_id == "openrouter:a":
            return _Sess(status=429, error_class="429",
                         headers={"x-ratelimit-limit-requests": "20",
                                  "x-ratelimit-remaining-requests": "0",
                                  "x-ratelimit-reset-requests": "45s"},
                         error_detail="rate limit exceeded")
        return _Sess([_delta("ok"), _DONE])

    monkeypatch.setattr(proxy, "open_stream", fake_open)
    client.post("/v1/chat/completions", headers={"X-MI-Session": "s1"},
                json={"model": "t", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]})

    s = Store(tmp_path / "p.db")
    obs = dict(s.conn.execute(
        "SELECT error_class, rate_limit_reason FROM observations"
        " WHERE deploy_id='openrouter:a' ORDER BY id DESC LIMIT 1").fetchone())
    assert obs["error_class"] == "429"
    # `rpm` because the declared reset is 45s, i.e. the minute bucket.
    assert obs["rate_limit_reason"] == "rpm"

    bucket = dict(s.conn.execute(
        "SELECT observed_limit_n, observed_remaining_n FROM quota_buckets"
        " WHERE deploy_id='openrouter:a' AND \"window\"='minute'").fetchone())
    assert bucket["observed_limit_n"] == 20
    assert bucket["observed_remaining_n"] == 0
    s.close()
