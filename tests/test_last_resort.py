"""Always an answer: the reserved arm runs only after the chain fails.

The failure this prevents: every candidate 429s and the caller gets a 503. The
last resort is a different provider, never ranked, and tried last.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mininfer.execute import CallResult
from mininfer.store import Store

_POLICY = """\
policy:
  name: test
  top_k: 3
  require_distinct_provider: false
  require_callable: false
  objective: cost_per_success
  on_unverified_capability: allow
  require: {}
  min_context: 0
  min_success_lb: 0.0
  allow_unknown_price: true
  free_floor_exempt: true
  free_trial_obs: 3
  prior_strength: 20.0
  last_resort: groq:rescue
tasks:
  t:
    description: test task
    tokens_in: 10
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
"""


def _client(tmp_path, monkeypatch) -> TestClient:
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.delenv("MI_LAST_RESORT", raising=False)   # use the policy value
    store = Store(tmp_path / "p.db")
    store.conn.execute("INSERT INTO weights (weights_id, display_name) VALUES ('w','m')")
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " price_in, price_out, context_window, zero_price, status)"
        " VALUES ('openrouter:m:free','w','openrouter','m',0.0,0.0,1000,1,'live')")
    store.commit()
    store.close()
    from mininfer.proxy import app
    return TestClient(app)


def _runner(calls: list[str]):
    def factory(**kw):
        def run(deploy_id, messages, **kw2):
            calls.append(deploy_id)
            if "openrouter" in deploy_id:            # the whole free chain fails
                return CallResult(deploy_id, text="", ok=False, error_class="429")
            return CallResult(deploy_id, text="rescued", ok=True, tokens_in=5,
                              tokens_out=5, latency_ms=1.0)
        return run
    return factory


def test_the_last_resort_answers_when_the_chain_is_exhausted(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(calls))

    r = client.post("/v1/chat/completions",
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    # Tried the ranked chain, then the reserved arm — in that order. (A shadow
    # trial may append a further background call; only the first two matter here.)
    assert calls[:2] == ["openrouter:m:free", "groq:rescue"]
    assert r.json()["choices"][0]["message"]["content"] == "rescued"

    store = Store(tmp_path / "p.db")
    row = store.conn.execute("SELECT reason FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    store.close()
    assert json.loads(row["reason"])["last_resort"] == "groq:rescue"


def test_the_last_resort_is_not_in_the_ranked_chain(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(calls))
    # A healthy chain answers without ever touching the reserve.
    store = Store(tmp_path / "p.db")
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " price_in, price_out, context_window, zero_price, status)"
        " VALUES ('openrouter:ok:free','w','openrouter','ok',0.0,0.0,1000,1,'live')")
    store.commit()
    store.close()

    def factory(**kw):                       # this time the free arm succeeds
        def run(deploy_id, messages, **kw2):
            calls.append(deploy_id)
            return CallResult(deploy_id, text="fine", ok=True, tokens_in=1,
                              tokens_out=1, latency_ms=1.0)
        return run
    monkeypatch.setattr("mininfer.proxy.Runner", factory)

    r = client.post("/v1/chat/completions",
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "groq:rescue" not in calls
