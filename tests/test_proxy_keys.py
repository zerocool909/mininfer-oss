"""The proxy must not spend an attempt on a provider it has no key for.

`route()` ranks on price and quality, so it will happily put a *free* arm first
whose provider is uncredentialed. Without a filter the call burns an attempt,
falls through to a paid arm, and reports a failure that looks like the free
model was broken. `mi route` intentionally keeps showing the true ranking (it
answers "what is cheapest", not "what can I call"), so the filter lives in the
proxy.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from mininfer.execute import CallResult
from mininfer.proxy import _keys_available
from mininfer.schema import Deployment, Weights
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
tasks:
  t:
    description: test task
    tokens_in: 10
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
"""


def _setup(tmp_path, monkeypatch) -> TestClient:
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    store = Store(tmp_path / "p.db")
    store.upsert_weights(Weights("hf:or/m", "or-model", params_b=7.0))
    store.upsert_deployment(Deployment("openrouter:m", "hf:or/m", "openrouter", "m",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    store.commit()
    store.close()
    return TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)


def _fake_runner(**kw):
    def run(deploy_id, messages, **kw2):
        return CallResult(deploy_id, text="ok", ok=True)
    return run


def test_keys_available_reflects_the_environment(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert _keys_available("openrouter:m") is False
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert _keys_available("openrouter:m") is True
    assert _keys_available("not-a-real-provider:x") is False


def test_routed_request_skips_keyless_candidates(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    r = client.post("/v1/chat/completions",
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 503
    err = r.json()["error"]
    assert err["type"] == "no_api_key"
    # the error must name the env var to set
    assert "OPENROUTER_API_KEY" in err["message"]


def test_routed_request_proceeds_and_reports_no_skips(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr("mininfer.proxy.Runner", _fake_runner)

    r = client.post("/v1/chat/completions",
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 200
    body = r.json()
    assert body["mi"]["selected_model"] == "openrouter:m"
    assert body["mi"]["reason"]["skipped_no_key"] == []


def test_router_marks_callable_and_rejects_when_required(tmp_path, monkeypatch):
    """`build_candidates` tags reachability; `require_callable` turns it into a
    rejection with a distinct reason (so it is counted separately in the funnel)."""
    from mininfer.router import Policy, build_candidates
    from mininfer.schema import TaskProfile

    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    store = Store(tmp_path / "p.db")
    store.upsert_weights(Weights("hf:or/m", "or-model", params_b=7.0))
    store.upsert_deployment(Deployment("openrouter:m", "hf:or/m", "openrouter", "m",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    store.commit()

    task = TaskProfile(name="t", tokens_in=10, tokens_out=10)
    pol = Policy(require_callable=True, min_success_lb=0.0, allow_unknown_price=True)

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    c = build_candidates(store, task, pol)[0]
    assert c.callable is False
    assert c.rejected == "no API key (provider not configured)"

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    c = build_candidates(store, task, pol)[0]
    assert c.callable is True
    assert c.rejected is None

    # without the policy requirement the arm is still ranked (theoretical view)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    c = build_candidates(store, task, Policy(min_success_lb=0.0,
                                             allow_unknown_price=True))[0]
    assert c.callable is False
    assert c.rejected is None
    store.close()


def test_free_arms_exempt_from_the_quality_floor(tmp_path, monkeypatch):
    """A free, callable arm with no benchmark must still be tryable.

    "Free first" is unreachable if the quality floor drops every free model that
    no benchmark source has covered yet — the arm is free, so the cost of being
    wrong is a retry, and the attempt becomes the evidence.
    """
    from mininfer.router import Policy, build_candidates
    from mininfer.schema import TaskProfile

    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    store = Store(tmp_path / "p.db")
    store.upsert_weights(Weights("hf:or/free", "free-model", params_b=7.0))
    store.upsert_deployment(Deployment("openrouter:free", "hf:or/free", "openrouter",
                                       "free", price_in=0.0, price_out=0.0,
                                       zero_price=True, context_window=1000))
    store.commit()

    task = TaskProfile(name="t", tokens_in=10, tokens_out=10, min_success_lb=0.55)

    strict = build_candidates(store, task, Policy(min_success_lb=0.55))[0]
    assert strict.rejected and strict.rejected.startswith("no benchmark evidence")

    exempt = build_candidates(store, task,
                              Policy(min_success_lb=0.55, free_floor_exempt=True))[0]
    assert exempt.free_kind == "zero_price"
    assert exempt.rejected is None
    store.close()


# ------------------------------------------------------------------ BYO keys
#
# A user may bring their own provider key instead of the operator setting one in
# the environment. The dashboard keeps such keys in the browser and sends them
# per request in `X-User-API-Keys`; the proxy treats that provider as callable
# for that request only. There was no test for this path, so a regression would
# have silently broken "use your own key" without failing anything.


def test_a_user_supplied_key_makes_an_uncredentialed_provider_callable(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("mininfer.proxy.Runner", _fake_runner)

    r = client.post("/v1/chat/completions",
                    headers={"X-User-API-Keys": '{"openrouter": "sk-user-supplied"}'},
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["mi"]["selected_model"] == "openrouter:m"
    assert body["mi"]["reason"]["skipped_no_key"] == []


def test_byo_keys_are_scoped_to_the_request_that_sends_them(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("mininfer.proxy.Runner", _fake_runner)
    payload = {"model": "t", "messages": [{"role": "user", "content": "hi"}]}

    ok = client.post("/v1/chat/completions",
                     headers={"X-User-API-Keys": '{"openrouter": "sk-user-supplied"}'},
                     json=payload)
    assert ok.status_code == 200
    # The next caller without the header is back to "no key": the key is never
    # stored server-side, only forwarded from the browser per request.
    nope = client.post("/v1/chat/completions", json=payload)
    assert nope.status_code == 503
    assert nope.json()["error"]["type"] == "no_api_key"


def test_a_malformed_user_keys_header_is_ignored(tmp_path, monkeypatch):
    """A bad header must degrade to "no user keys", not 500 the request."""
    client = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    r = client.post("/v1/chat/completions",
                    headers={"X-User-API-Keys": "not-json"},
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "no_api_key"


# ------------------------------------------------------- blank keys are absent
#
# An empty string is not a credential. A browser's stored keys (or any caller)
# can send `api_key=""`; treating it as an explicit override produced a request
# with no `Authorization` header at all — the provider answers "Missing
# Authentication header", which reads as a *bad* key rather than a missing one,
# and silently shadowed the key configured in the environment.


def test_a_blank_key_falls_back_to_the_environment(monkeypatch):
    from mininfer.execute import resolve_endpoint

    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    for blank in (None, "", "   "):
        ep = resolve_endpoint("openrouter:m", api_key=blank)
        assert ep.api_key == "env-key", blank
        assert ep.headers["Authorization"] == "Bearer env-key"


def test_a_real_key_still_overrides_the_environment(monkeypatch):
    from mininfer.execute import resolve_endpoint

    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    ep = resolve_endpoint("openrouter:m", api_key=" mine ")
    assert ep.api_key == "mine"          # stripped, and it wins


def test_a_blank_key_with_no_environment_key_is_simply_absent(monkeypatch):
    from mininfer.execute import resolve_endpoint

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ep = resolve_endpoint("openrouter:m", api_key="")
    assert ep.api_key is None
    assert "Authorization" not in ep.headers
