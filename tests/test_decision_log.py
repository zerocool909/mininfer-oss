"""The decision log is the request log.

Every request the proxy handles has to leave a `decisions` row, whatever the
outcome. Recording only the successful `auto` path made the log a highlight
reel — the only `mode` a mature registry held was `auto`, so a direct
`provider/model` call, a refused task and a capped session were invisible.

These run against a stubbed caller: the point is what gets written *before*
anything is called, so no upstream is contacted.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mininfer.execute import CallResult
from mininfer.schema import Deployment, Weights
from mininfer.store import Store

_POLICY = """\
policy:
  name: test
  top_k: 3
  require_distinct_provider: false
  on_unverified_capability: allow
  require: {}
  min_context: 0
  min_success_lb: 0.0
  allow_unknown_price: true
  prior_strength: 25.0
tasks:
  alpha:
    description: alpha task
    tokens_in: 100
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
    benchmark_keys: [coding]
    cues: [alpha]
"""


def _client(tmp_path, monkeypatch) -> TestClient:
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("MI_SESSION_TOKEN_LIMIT", raising=False)
    # A single eligible arm, so `auto` has something to pick.
    store = Store(tmp_path / "p.db")
    store.upsert_weights(Weights("hf:t/m", "test-model", params_b=7.0,
                                 benchmark={"coding": 50.0}))
    store.upsert_deployment(Deployment("openrouter:m", "hf:t/m", "openrouter", "m",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    store.commit()
    store.close()

    import mininfer.proxy as proxy
    # Never touch the network: return a canned failure. The decision row is
    # written before this is reached, which is the behaviour under test.
    monkeypatch.setattr(proxy, "try_fallbacks", lambda deploys, messages, runner, **kw: (
        None, CallResult(deploys[0] if deploys else "", error_class="test"), []))
    return TestClient(proxy.app)


def _rows(tmp_path) -> list[dict]:
    store = Store(tmp_path / "p.db")
    rows = [dict(r) for r in store.conn.execute(
        "SELECT mode, task, chosen, candidates, reason FROM decisions ORDER BY id").fetchall()]
    store.close()
    for r in rows:
        r["reason"] = json.loads(r["reason"])
        r["candidates"] = json.loads(r["candidates"] or "[]")
    return rows


def test_every_request_shape_is_recorded(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    post = lambda body, **kw: client.post("/v1/chat/completions", json=body, **kw)  # noqa: E731

    post({"model": "auto", "messages": [{"role": "user", "content": "just alpha"}]})
    post({"model": "openrouter:m", "messages": [{"role": "user", "content": "hi"}]})
    # No cue matches, so `auto` falls back to the default task, which this
    # policy does not define — a refusal, but still a routing attempt.
    post({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    post({"model": "auto"})  # malformed: no messages
    monkeypatch.setenv("MI_SESSION_TOKEN_LIMIT", "10")
    post({"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
         headers={"X-MI-Session": "s-cap"})

    rows = _rows(tmp_path)
    assert [r["mode"] for r in rows] == ["auto", "direct", "rejected", "rejected", "blocked"]
    assert rows[0]["chosen"] == "openrouter:m"
    assert rows[0]["candidates"] == ["openrouter:m"]
    assert rows[1]["chosen"] == "openrouter:m"
    assert rows[1]["reason"]["funnel"] == {"direct": True}
    assert rows[2]["reason"]["error"] == "unknown_task"
    assert rows[3]["reason"]["error"] == "bad_request"
    assert rows[4]["reason"]["error"] == "session_limit"


def test_a_refusal_is_recorded_before_the_error_is_returned(tmp_path, monkeypatch):
    """Ordering matters: the row must survive the early return that produced it."""
    client = _client(tmp_path, monkeypatch)
    r = client.post("/v1/chat/completions", json={"model": "auto"})
    assert r.status_code == 400
    assert len(_rows(tmp_path)) == 1
