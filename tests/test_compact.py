"""`/v1/compact` — the summariser that keeps a long thread cheap to continue.

Two properties matter: it returns a brief, and it does so **without polluting the
router's own learning** — the summarisation call is a throwaway, so it must not
become an observation or a decision that later ranks models.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import mininfer.auth as auth
import mininfer.proxy as proxy
from mininfer.execute import CallResult
from mininfer.store import Store

_POLICY = """\
policy:
  name: test
  top_k: 2
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
  general_chat:
    description: test task
    tokens_in: 10
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
"""


class _FakeRunner:
    """Stands in for `Runner`: returns a canned brief, records what it was asked."""

    calls: list[dict] = []

    def __init__(self, *a, **k):
        pass

    def __call__(self, deploy_id, messages, **kw):
        _FakeRunner.calls.append({"deploy_id": deploy_id, "messages": messages, "kw": kw})
        return CallResult(deploy_id, text="- goal: build a planet\n- constraint: no glue",
                          ok=True, finish_reason="stop")


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    store = Store(tmp_path / "p.db")
    store.conn.execute("INSERT INTO weights (weights_id, display_name, benchmark) "
                       "VALUES ('hf:m','m','{\"coding\": 60}')")
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " price_in, price_out, context_window, zero_price) VALUES ('openrouter:m','hf:m',"
        "'openrouter','m',0.0,0.0,1000,1)")
    store.commit()
    store.close()
    _FakeRunner.calls = []
    monkeypatch.setattr(proxy, "Runner", _FakeRunner)
    return TestClient(proxy.app), tmp_path / "p.db"


def test_compact_returns_a_summary(client):
    c, _db = client
    r = c.post("/v1/compact", json={"messages": [
        {"role": "user", "content": "how do I make a planet"},
        {"role": "assistant", "content": "crumple the paper, then layer"},
        {"role": "user", "content": "and without glue?"},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert "planet" in body["summary"]
    assert body["compacted"] == 3
    assert body["model"]


def test_compact_does_not_record_observations_or_decisions(client):
    """The summariser must not train the router about the model it borrowed."""
    c, db = client
    store = Store(db)
    before = (
        store.conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"],
        store.conn.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"],
    )
    store.close()

    c.post("/v1/compact", json={"messages": [{"role": "user", "content": "hello"}]})

    store = Store(db)
    after = (
        store.conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"],
        store.conn.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"],
    )
    store.close()
    assert after == before


def test_compact_merges_a_previous_summary_into_the_prompt(client):
    c, _db = client
    c.post("/v1/compact", json={
        "messages": [{"role": "user", "content": "more detail please"}],
        "summary": "- goal: build a planet",
    })
    ask = _FakeRunner.calls[-1]["messages"][-1]["content"]
    assert "Previous summary" in ask and "build a planet" in ask


def test_compact_is_empty_safe(client):
    c, _db = client
    r = c.post("/v1/compact", json={"messages": [], "summary": "- keep me"})
    assert r.json() == {"summary": "- keep me", "model": None, "compacted": 0}


def test_compact_is_a_tenant_surface():
    """Under access control a tenant key may summarise, but it is not public."""
    assert auth.classify("/v1/compact") == "tenant"
