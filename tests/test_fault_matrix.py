"""The fault matrix: what the caller sees when the upstream misbehaves.

The guarantee this file pins is P1 from the release plan: **a request ends in
a 200 with an answer, or a coherent error — never a bare 500 and never a
hang.** Two of the defects found by live testing broke exactly that promise,
and neither was visible to a unit test:

* a provider's absurd rate-limit header (`…reset: 99999999999999s`) overflowed
  `datetime` inside the rate-limit bookkeeping, and the `OverflowError`
  propagated out of the request path — so a *working* model's answer became a
  500;
* a write transaction held across an upstream call starved every concurrent
  request into `database is locked`, and the request died with a 500.

So the matrix is table-driven: each row is a way an upstream can misbehave,
and the column is what the caller must get.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mininfer import proxy
from mininfer.execute import CallResult
from mininfer.schema import Deployment, Weights
from mininfer.store import Store

ARM = "test:model"


def _client(monkeypatch, tmp_path):
    db = tmp_path / "f.db"
    s = Store(db)
    s.upsert_weights(Weights("hf:test/m", "test-model", params_b=7.0))
    s.upsert_deployment(Deployment(ARM, "hf:test/m", "test", "model",
                                   price_in=1.0, price_out=1.0))
    s.commit()
    s.close()
    monkeypatch.setenv("MI_DB", str(db))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    monkeypatch.setattr(proxy, "_db", lambda: str(db))
    return TestClient(proxy.app), db


def _post(client):
    return client.post("/v1/chat/completions",
                        json={"model": ARM,
                              "messages": [{"role": "user", "content": "hi"}],
                              "max_tokens": 16})


# --------------------------------------------------------------------------- #
# the matrix
# --------------------------------------------------------------------------- #


#: (name, the upstream's answer, must the caller get a body?)
FAULTS = [
    ("rate limited", CallResult(ARM, error_class="429"), False),
    ("rate limited with headers",
     CallResult(ARM, error_class="429",
                rate_limit_headers={"x-ratelimit-limit-requests": "20",
                                    "x-ratelimit-remaining-requests": "0",
                                    "x-ratelimit-reset-requests": "60s"}), False),
    ("an absurd reset header",
     CallResult(ARM, error_class="429",
                rate_limit_headers={"x-ratelimit-limit-requests": "20",
                                    "x-ratelimit-remaining-requests": "0",
                                    "x-ratelimit-reset-requests": "99999999999999s"}),
     False),
    ("a server error", CallResult(ARM, error_class="http_500"), False),
    ("a not-found route", CallResult(ARM, error_class="http_404"), False),
    ("a timeout", CallResult(ARM, error_class="timeout"), False),
    ("a tls failure", CallResult(ARM, error_class="tls_error"), False),
    ("a network failure", CallResult(ARM, error_class="network_error"), False),
    ("an empty body", CallResult(ARM, text="", ok=True), False),
    ("not callable", CallResult(ARM, error_class="not_api_callable",
                                 error_detail="only available on agentic harnesses"),
     False),
]

#: The statuses a caller may legitimately receive for a failed upstream.
COHERENT = {400, 404, 429, 502, 503, 504}


@pytest.mark.parametrize("name,result,expect_body", FAULTS,
                         ids=[f[0] for f in FAULTS])
def test_an_upstream_fault_never_becomes_a_bare_500(monkeypatch, tmp_path,
                                                     name, result, expect_body):
    """The guarantee, row by row."""
    client, db = _client(monkeypatch, tmp_path)

    def runner(**kw):
        def run(deploy_id, messages, **kw2):
            return result
        return run

    monkeypatch.setattr(proxy, "Runner", runner)
    r = _post(client)

    assert r.status_code != 500, (
        f"{name}: the caller got a bare 500 — {r.text[:200]}")
    assert r.status_code in COHERENT, (
        f"{name}: unexpected status {r.status_code}: {r.text[:200]}")
    body = r.json()
    assert body.get("error", {}).get("message"), (
        f"{name}: the error has no message — {body}")
    assert body["error"].get("type"), f"{name}: the error has no type — {body}"


@pytest.mark.parametrize("name,result,expect_body", FAULTS,
                         ids=[f[0] for f in FAULTS])
def test_an_upstream_fault_is_still_recorded(monkeypatch, tmp_path,
                                              name, result, expect_body):
    """A failure the router cannot see is a failure it will repeat.

    The observation is how a bad arm gets demoted, so a request that failed
    and left no trace teaches the router nothing.
    """
    client, db = _client(monkeypatch, tmp_path)

    def runner(**kw):
        def run(deploy_id, messages, **kw2):
            return result
        return run

    monkeypatch.setattr(proxy, "Runner", runner)
    _post(client)

    s = Store(db)
    try:
        rows = s.conn.execute(
            "SELECT ok, error_class FROM observations").fetchall()
        assert rows, f"{name}: the attempt was not recorded at all"
        assert any(not r["ok"] for r in rows), (
            f"{name}: recorded as a success — {rows}")
    finally:
        s.close()


def test_an_absurd_reset_header_does_not_fail_the_request(monkeypatch, tmp_path):
    """The regression, on its own: the header, and nothing else.

    It is here separately because it is the one fault that used to produce a
    500 from an otherwise *successful* upstream answer.
    """
    client, db = _client(monkeypatch, tmp_path)

    def runner(**kw):
        def run(deploy_id, messages, **kw2):
            return CallResult(
                deploy_id, text="the answer", ok=True,
                rate_limit_headers={
                    "x-ratelimit-limit-requests": "20",
                    "x-ratelimit-remaining-requests": "19",
                    "x-ratelimit-reset-requests": "99999999999999s"})
        return run

    monkeypatch.setattr(proxy, "Runner", runner)
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "the answer"


def test_a_successful_answer_survives_a_hostile_header_set(monkeypatch, tmp_path):
    """Several odd headers at once, and the answer still comes through."""
    client, db = _client(monkeypatch, tmp_path)

    def runner(**kw):
        def run(deploy_id, messages, **kw2):
            return CallResult(
                deploy_id, text="still fine", ok=True,
                rate_limit_headers={
                    "x-ratelimit-limit-requests": "-1",
                    "x-ratelimit-remaining-requests": "not-a-number",
                    "x-ratelimit-reset-requests": "0s",
                    "x-ratelimit-reset-tokens": "999999999999999999s",
                    "x-ratelimit-remaining-tokens": "∞"})
        return run

    monkeypatch.setattr(proxy, "Runner", runner)
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "still fine"


# --------------------------------------------------------------------------- #
# the request side
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("body,status", [
    ({}, 400),
    ({"messages": []}, 400),
    ({"messages": "not a list"}, 400),
])
def test_a_malformed_request_is_a_400_not_a_500(monkeypatch, tmp_path,
                                                 body, status):
    """Bad input is the caller's problem, and it is answered as such."""
    client, _db = _client(monkeypatch, tmp_path)
    r = client.post("/v1/chat/completions", json=body)
    assert r.status_code == status, f"{body} -> {r.status_code}: {r.text[:200]}"


def test_an_odd_but_accepted_request_is_never_a_500(monkeypatch, tmp_path):
    """`max_tokens: 0` is coerced to the default rather than refused.

    Which is fine — the property under test is that an odd request never
    becomes a bare 500 on the way through.
    """
    client, _db = _client(monkeypatch, tmp_path)

    def runner(**kw):
        def run(deploy_id, messages, **kw2):
            return CallResult(deploy_id, text="ok", ok=True, latency_ms=1.0)
        return run

    monkeypatch.setattr(proxy, "Runner", runner)
    r = client.post("/v1/chat/completions",
                    json={"model": ARM,
                          "messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 0})
    assert r.status_code != 500, r.text[:200]
    assert r.status_code in (200, 400, 413), r.status_code


def test_a_huge_body_is_refused_not_absorbed(monkeypatch, tmp_path):
    """The body cap is an abuse guard; it must refuse, not truncate.

    And it must refuse *whether or not access control is on* — the cap used
    to sit behind `cfg.enabled`, so an open deployment (no `MI_API_KEYS`, no
    admin token) accepted an unbounded body. Abuse does not require
    authentication.
    """
    client, _db = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("MI_MAX_BODY_BYTES", "2048")
    r = client.post("/v1/chat/completions",
                    json={"model": ARM,
                          "messages": [{"role": "user", "content": "x" * 8000}]})
    assert r.status_code in (400, 413), f"{r.status_code}: {r.text[:200]}"
    assert r.json()["error"]["type"], r.text[:200]
    assert "exceeds" in r.json()["error"]["message"]
