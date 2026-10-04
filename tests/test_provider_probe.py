"""`POST /v1/providers/test` must not fabricate a model.

When the registry held no deployment for a provider, the handler invented
`{provider}:test` and called a chat completion with it. No provider serves a
model named `test`, so the answer was always a 404 — surfaced in the dashboard as
"Connectivity failed … Model: test / The model `test` does not exist", which says
nothing about whether the key or the network was fine.

It now asks the provider to list its own models, which is what "test
connectivity" promises: reachability (DNS/TLS) and that the key is accepted.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

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


def _client(tmp_path, monkeypatch, *, seed_for: str | None = None) -> TestClient:
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    if seed_for:
        from mininfer.schema import Deployment, Weights
        from mininfer.store import Store

        s = Store(tmp_path / "p.db")
        s.upsert_weights(Weights(f"hf:{seed_for}/m", "m", params_b=7.0))
        s.upsert_deployment(Deployment(f"{seed_for}:real-model", f"hf:{seed_for}/m",
                                       seed_for, "real-model", price_in=0.0,
                                       price_out=0.0, context_window=1000))
        s.commit()
        s.close()
    import mininfer.proxy as proxy
    return TestClient(proxy.app)


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    def __init__(self, resp: _Resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return self._resp


def _stub_models(monkeypatch, resp: _Resp) -> None:
    """Point the probe's `httpx.AsyncClient` at a canned response."""
    import mininfer.proxy as proxy
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: _Client(resp))


# --------------------------------------------------------------------------- #
# no model in the registry -> probe the catalogue, never a fake chat model
# --------------------------------------------------------------------------- #


def test_no_ingested_model_is_never_reported_as_a_model_named_test(tmp_path, monkeypatch):
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)

    r = client.post("/v1/providers/test", json={"provider": "cerebras"})

    assert r.status_code == 200
    body = r.json()
    assert body["model"] != "test"
    assert body["deploy_id"] is None
    assert body["error_class"] == "no_api_key"
    # ...and it names the fix, because "no models for this provider" is otherwise
    # a dead end.
    assert "CEREBRAS_API_KEY" in body["error_detail"]
    assert "mi refresh" in body["error_detail"]


def test_the_probe_reports_a_rejected_key_from_the_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    _stub_models(monkeypatch, _Resp(401, text="unauthorized"))

    body = client.post("/v1/providers/test",
                       json={"provider": "cerebras", "api_key": "sk-bad"}).json()

    assert body["ok"] is False
    assert body["error_class"] == "auth_error"
    assert "401" in body["error_detail"]
    assert body["latency_ms"] is not None      # a real call was made


def test_a_good_key_connects_but_says_the_registry_is_still_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    _stub_models(monkeypatch, _Resp(200, {"data": [{"id": "a"}, {"id": "b"}]}))

    body = client.post("/v1/providers/test",
                       json={"provider": "cerebras", "api_key": "sk-good"}).json()

    assert body["ok"] is True
    # "Connected" with no usable model is the confusing half-state this replaces.
    assert "mi refresh" in body["reply"]
    assert "2 model" in body["reply"]


def test_a_provider_with_an_ingested_model_still_routes_normally(tmp_path, monkeypatch):
    """The probe is a fallback, not a replacement."""
    from mininfer.execute import CallResult

    client = _client(tmp_path, monkeypatch, seed_for="cerebras")
    import mininfer.proxy as proxy
    monkeypatch.setattr(proxy, "Runner", lambda **kw: (
        lambda did, msgs, **kw2: CallResult(did, text="pong", ok=True, tokens_in=1, tokens_out=1)))

    body = client.post("/v1/providers/test", json={"provider": "cerebras"}).json()

    assert body["ok"] is True
    assert body["model"] == "real-model"
    assert body["deploy_id"] == "cerebras:real-model"


def test_a_saved_key_from_the_header_is_used(tmp_path, monkeypatch):
    """The dashboard sends saved keys in `X-User-API-Keys`, like the chat path.

    This endpoint read only the request body, so a key showing as "Custom Key
    Active" in the UI was invisible to Test: it exercised the *environment* key
    instead, and with none configured sent no credential at all — which the
    provider reports as "Missing Authentication header".
    """
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    _stub_models(monkeypatch, _Resp(200, {"data": [{"id": "a"}, {"id": "b"}]}))

    body = client.post(
        "/v1/providers/test",
        headers={"X-User-API-Keys": json.dumps({"cerebras": "sk-saved-in-browser"})},
        json={"provider": "cerebras"},
    ).json()

    assert body["key_source"] == "custom"
    assert body["ok"] is True


def test_a_body_key_still_wins_over_the_header(tmp_path, monkeypatch):
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    _stub_models(monkeypatch, _Resp(200, {"data": []}))

    body = client.post(
        "/v1/providers/test",
        headers={"X-User-API-Keys": json.dumps({"cerebras": "header-key"})},
        json={"provider": "cerebras", "api_key": "typed-key"},
    ).json()
    assert body["key_source"] == "custom"


def test_a_rate_limited_free_arm_is_retried_not_reported_as_failure(tmp_path, monkeypatch):
    """A 429 proves connectivity — authenticated, and the provider answered.

    The endpoint used to call a single arm, so a shared `:free` model being
    rate-limited read as "Connectivity failed" for a provider that was fine.
    """
    from mininfer.execute import CallResult
    from mininfer.schema import Deployment, Weights
    from mininfer.store import Store

    client = _client(tmp_path, monkeypatch)
    s = Store(tmp_path / "p.db")
    s.upsert_weights(Weights("hf:cerebras/m", "m", params_b=7.0))
    for i in range(3):
        s.upsert_deployment(Deployment(f"cerebras:m{i}", "hf:cerebras/m", "cerebras",
                                       f"m{i}", price_in=0.0, price_out=0.0,
                                       context_window=1000))
    s.commit()
    s.close()

    import mininfer.proxy as proxy
    calls: list[str] = []

    class _Runner:
        def __init__(self, **kw):
            pass

        def __call__(self, did, msgs, **kw):
            calls.append(did)
            if len(calls) < 3:
                return CallResult(did, error_class="429", error_detail="Provider returned error")
            return CallResult(did, text="pong", ok=True)

    monkeypatch.setattr(proxy, "Runner", _Runner)

    body = client.post("/v1/providers/test", json={"provider": "cerebras"}).json()

    assert body["ok"] is True
    assert len(calls) == 3          # it walked past the two rate-limited arms


def test_the_trial_endpoint_reads_the_header_everything_else_does(tmp_path, monkeypatch):
    """`x-user-api-keys`, not `x-user-keys`.

    The Models tab's Test button posts here, and the dashboard sends
    `X-User-API-Keys` — the header every other path reads. This endpoint alone
    asked for `x-user-keys`, so a saved key was invisible and the trial silently
    used the environment key instead.
    """
    from mininfer.execute import CallResult
    from mininfer.schema import Deployment, Weights
    from mininfer.store import Store

    client = _client(tmp_path, monkeypatch)
    s = Store(tmp_path / "p.db")
    s.upsert_weights(Weights("hf:cerebras/m", "m", params_b=7.0))
    s.upsert_deployment(Deployment("cerebras:m0", "hf:cerebras/m", "cerebras", "m0",
                                   price_in=0.0, price_out=0.0, context_window=1000))
    s.commit()
    s.close()

    import mininfer.proxy as proxy
    seen: dict = {}

    class _Runner:
        def __init__(self, **kw):
            seen["user_keys"] = kw.get("user_keys")

        def __call__(self, did, msgs, **kw):
            return CallResult(did, text="a trial answer", ok=True)

    monkeypatch.setattr(proxy, "Runner", _Runner)
    client.post("/v1/models/trial",
                headers={"X-User-API-Keys": json.dumps({"cerebras": "sk-byo"})},
                json={"deploy_id": "cerebras:m0"})

    assert seen["user_keys"] == {"cerebras": "sk-byo"}
