"""Tests for the OpenAI-compatible proxy and its fallback execution.

The contract that must not drift: a failed primary (429/5xx/timeout) must move to
the next candidate and record *both* attempts as observations — otherwise a 429
never demotes the arm that produced it.
"""
from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

from mininfer.execute import CallResult, Endpoint, call_body
from mininfer.proxy import try_fallbacks
from mininfer.schema import Deployment, Weights
from mininfer.store import Store


def _queue_runner(results: list[CallResult]):
    """Returns a factory whose runners pop one result per call."""
    it = iter(results)

    def factory(**kw):
        def run(deploy_id, messages, **kw2):
            return next(it)
        return run

    return factory


def test_try_fallbacks_moves_past_429(tmp_path):
    store = Store(tmp_path / "p.db")
    runner = _queue_runner([
        CallResult("a:1", error_class="429", latency_ms=10.0),
        CallResult("b:2", text="ok", ok=True, latency_ms=20.0,
                   tokens_in=5, tokens_out=3),
    ])()

    selected, res, attempts = try_fallbacks(
        ["a:1", "b:2"], [{"role": "user", "content": "hi"}], runner,
        task_name="sql_generation", store=store)

    assert selected == "b:2" and res.text == "ok"
    assert [a.deploy_id for a in attempts] == ["a:1", "b:2"]
    assert attempts[0].ok is False and attempts[1].ok is True

    # both attempts are recorded — the 429 is what demotes `a:1` tomorrow
    rows = store.conn.execute(
        "SELECT deploy_id, ok, error_class FROM observations ORDER BY id").fetchall()
    assert [(r["deploy_id"], r["ok"], r["error_class"]) for r in rows] == [
        ("a:1", 0, "429"), ("b:2", 1, None)]
    store.close()


def test_try_fallbacks_all_fail_returns_last_error(tmp_path):
    store = Store(tmp_path / "p.db")
    runner = _queue_runner([
        CallResult("a:1", error_class="429"),
        CallResult("b:2", error_class="timeout"),
    ])()
    selected, res, attempts = try_fallbacks(
        ["a:1", "b:2"], [{"role": "user", "content": "hi"}], runner,
        task_name="sql_generation", store=store)
    assert selected is None
    assert res.error_class == "timeout"
    assert len(attempts) == 2
    store.close()


def test_try_fallbacks_treats_empty_content_as_failure(tmp_path):
    store = Store(tmp_path / "p.db")
    runner = _queue_runner([
        CallResult("a:1", text="", ok=True, tokens_in=5, tokens_out=20),
        CallResult("b:2", text="ok", ok=True, tokens_in=5, tokens_out=1),
    ])()
    selected, res, attempts = try_fallbacks(
        ["a:1", "b:2"], [{"role": "user", "content": "hi"}], runner,
        task_name="sql_generation", store=store)
    assert selected == "b:2"
    assert attempts[0].error_class == "empty_content"
    rows = store.conn.execute(
        "SELECT deploy_id, ok, error_class FROM observations ORDER BY id").fetchall()
    assert [(r["deploy_id"], r["ok"], r["error_class"]) for r in rows] == [
        ("a:1", 0, "empty_content"), ("b:2", 1, None)]
    store.close()


def _seed(store: Store) -> None:
    store.upsert_weights(Weights("hf:test/m", "test-model", params_b=7.0))
    store.upsert_deployment(Deployment("test:model", "hf:test/m", "test", "model",
                                       price_in=1.0, price_out=1.0))
    store.commit()


def test_healthz_and_models(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    assert client.get("/healthz").json()["ok"] is True
    ids = {m["id"] for m in client.get("/v1/models").json()["data"]}
    assert "auto" in ids and "sql_generation" in ids


def test_chat_direct_mode_records_observation(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    _seed(Store(tmp_path / "p.db"))

    def fake_runner(**kw):
        def run(deploy_id, messages, **kw2):
            return CallResult(deploy_id, text="hello", ok=True,
                              tokens_in=5, tokens_out=3)
        return run

    monkeypatch.setattr("mininfer.proxy.Runner", fake_runner)
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "test:model",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "hello"
    assert body["mi"]["selected_model"] == "test:model"

    store = Store(tmp_path / "p.db")
    assert store.counts()["observations"] == 1
    store.close()


def test_dashboard_and_stats(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    _seed(Store(tmp_path / "p.db"))
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)

    page = client.get("/legacy")
    assert page.status_code == 200
    assert "MinInfer" in page.text and "Deployments by provider" in page.text

    stats = client.get("/v1/stats").json()
    assert stats["counts"]["weights"] >= 1
    assert any(p["provider"] == "test" for p in stats["providers"])


def test_chat_auto_routes_the_default_task(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hi"}]})
    # empty registry -> routed, but nothing eligible
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "no_candidates"


def test_chat_model_name_is_a_task(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "sql_generation",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "no_candidates"


def test_tool_calls_pass_through(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")

    tc = [{"id": "call_1", "type": "function",
           "function": {"name": "search", "arguments": '{"q":"x"}'}}]

    def fake_runner(**kw):
        def run(deploy_id, messages, **kw2):
            # empty content + a tool call is a valid agent turn, not a failure
            return CallResult(deploy_id, text="", ok=True,
                              raw={"model": "m", "choices": [{
                                  "message": {"role": "assistant", "content": None,
                                              "tool_calls": tc},
                                  "finish_reason": "tool_calls"}]})
        return run

    monkeypatch.setattr("mininfer.proxy.Runner", fake_runner)
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "test:model",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    msg = r.json()["choices"][0]["message"]
    assert msg["tool_calls"] == tc
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"


def test_call_body_handles_tool_call_without_content(monkeypatch):
    """Regression: a tool-call turn has no `content` key at all.

    Reading it with `["content"]` raised KeyError and classified every tool call
    as bad_output — which silently disabled tool use for the whole proxy.
    """
    def handler(request):
        return httpx.Response(200, json={"choices": [{
            "message": {"role": "assistant", "tool_calls": [
                {"id": "1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]})

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler)))
    ep = Endpoint("https://x/v1", "m", "k", {"Authorization": "Bearer k"})
    res = call_body(ep, {"model": "m", "messages": []}, deploy_id="d")
    assert res.ok and res.text == ""
    assert res.raw["choices"][0]["message"]["tool_calls"]


class _FakeSession:
    def __init__(self, status_code, chunks, error_class=None):
        self.status_code = status_code
        self.error_class = error_class
        self.error_detail = None
        self.headers = {}
        self._chunks = chunks
        self.closed = False

    def chunks(self):
        return iter(self._chunks)

    def close(self):
        self.closed = True


def test_stream_relays_sse(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))
    monkeypatch.setattr("mininfer.proxy.open_stream",
                        lambda ep, body, *, deploy_id, timeout=120.0: _FakeSession(
                            200, [b'data: {"a":1}\n\n', b'data: [DONE]\n\n']))

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "openrouter:x", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-mi-deploy"] == "openrouter:x"
    assert "data: [DONE]" in r.text


# --------------------------------------------------------------------------- #
# usage on the streamed path
# --------------------------------------------------------------------------- #
# The relay used to forward bytes and nothing else, so a streamed call recorded
# `ok` and no tokens — every streamed request contributed zero tokens and zero
# cost to `routing_stats`, and the dashboard's spend was structurally $0.


_STREAM_POLICY = """\
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


def _stream_env(tmp_path, monkeypatch) -> None:
    """Its own policy: the shipped one has a quality floor that rejects an arm
    with no benchmark evidence, which is unrelated to what these tests assert."""
    (tmp_path / "policy.yaml").write_text(_STREAM_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    store = Store(tmp_path / "p.db")
    store.upsert_weights(Weights("hf:or/m", "or-model", params_b=7.0))
    store.upsert_deployment(Deployment("openrouter:m", "hf:or/m", "openrouter", "m",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    store.commit()
    store.close()


def test_sse_usage_reads_the_usage_frame():
    from mininfer.proxy import _sse_usage

    frame = (b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
             b'data: {"choices":[],"usage":{"prompt_tokens":11,'
             b'"completion_tokens":7,"cost":0.0021}}\n\n'
             b'data: [DONE]\n\n')
    usage = _sse_usage(frame)
    assert usage["prompt_tokens"] == 11
    assert usage["completion_tokens"] == 7
    assert _sse_usage(b"data: [DONE]\n\n") is None
    assert _sse_usage(b": keep-alive\n\n") is None
    assert _sse_usage(b'data: {"choices":[{"delta":{}}]}\n\n') is None
    # a partial frame must not raise
    assert _sse_usage(b'data: {"choices":[{"del') is None


def test_usage_tokens_accepts_both_provider_spellings():
    from mininfer.proxy import _usage_tokens

    assert _usage_tokens({"prompt_tokens": 5, "completion_tokens": 2}) == (5, 2, None)
    assert _usage_tokens({"input_tokens": 5, "output_tokens": 2}) == (5, 2, None)
    assert _usage_tokens({"prompt_tokens": 5, "total_cost": 0.5}) == (5, None, 0.5)
    assert _usage_tokens(None) == (None, None, None)
    assert _usage_tokens({}) == (None, None, None)


def test_streamed_call_records_tokens_and_cost(tmp_path, monkeypatch):
    """The observation behind `routing_stats` must carry the real numbers."""
    _stream_env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))
    monkeypatch.setattr("mininfer.proxy.open_stream",
                        lambda ep, body, *, deploy_id, timeout=120.0: _FakeSession(
                            200, [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                                  b'data: {"choices":[],"usage":{"prompt_tokens":13,'
                                  b'"completion_tokens":9,"cost":0.004}}\n\n',
                                  b'data: [DONE]\n\n']))

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "t", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "data: [DONE]" in r.text

    store = Store(tmp_path / "p.db")
    row = store.conn.execute(
        "SELECT deploy_id, ok, tokens_in, tokens_out, cost_usd FROM observations"
    ).fetchone()
    assert row["ok"] == 1
    assert (row["tokens_in"], row["tokens_out"]) == (13, 9)
    assert row["cost_usd"] == 0.004
    # and it reaches the view the dashboard sums
    tot = store.conn.execute(
        "SELECT total_cost_usd FROM routing_stats").fetchone()[0]
    assert tot == 0.004
    store.close()


def test_streamed_call_without_usage_still_records_the_outcome(tmp_path, monkeypatch):
    """Absent usage is not a failure — the observation still lands."""
    _stream_env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))
    monkeypatch.setattr("mininfer.proxy.open_stream",
                        lambda ep, body, *, deploy_id, timeout=120.0: _FakeSession(
                            200, [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                                  b'data: [DONE]\n\n']))

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "t", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    store = Store(tmp_path / "p.db")
    row = store.conn.execute(
        "SELECT ok, tokens_in, tokens_out FROM observations").fetchone()
    assert row["ok"] == 1 and row["tokens_in"] is None
    store.close()


# --------------------------------------------------------------------------- #
# multi-turn
# --------------------------------------------------------------------------- #
# The chat sent only the newest message, so a follow-up arrived with nothing to
# follow up on. The proxy always forwarded whatever `messages` it was handed —
# these pin that, so the fix cannot be undone on the server side by accident.


def _capture_env(tmp_path, monkeypatch):
    _stream_env(tmp_path, monkeypatch)

    def factory(**kw):
        def run(deploy_id, messages, **kw2):
            captured.append(messages)
            return CallResult(deploy_id, text="ok", ok=True, tokens_in=1,
                              tokens_out=1, latency_ms=1.0)
        return run

    captured: list[list[dict]] = []
    monkeypatch.setattr("mininfer.proxy.Runner", factory)
    return captured


def test_a_follow_up_reaches_the_upstream_with_its_context(tmp_path, monkeypatch):
    captured = _capture_env(tmp_path, monkeypatch)
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions", json={
        "model": "t",
        "messages": [
            {"role": "user", "content": "what is postgres?"},
            {"role": "assistant", "content": "a relational database"},
            {"role": "user", "content": "and how does it compare to supabase?"},
        ],
    })
    assert r.status_code == 200
    assert captured[0] == [
        {"role": "user", "content": "what is postgres?"},
        {"role": "assistant", "content": "a relational database"},
        {"role": "user", "content": "and how does it compare to supabase?"},
    ]


def test_a_system_message_survives_compaction(tmp_path, monkeypatch):
    """The client's elision note is a system turn; it must not be dropped."""
    captured = _capture_env(tmp_path, monkeypatch)
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions", json={
        "model": "t",
        "messages": [
            {"role": "system", "content": "6 earlier messages were omitted."},
            {"role": "user", "content": "so what changed?"},
        ],
    })
    assert r.status_code == 200
    assert captured[0][0]["role"] == "system"


_MULTI_POLICY = """\
policy:
  name: test
  top_k: 1
  require_distinct_provider: false
  objective: cost_per_success
  on_unverified_capability: allow
  require: {}
  min_context: 0
  min_success_lb: 0.0
  allow_unknown_price: true
  prior_strength: 20.0
tasks:
  general_chat:
    description: conversation
    tokens_in: 10
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
    cues: [hello, hi, thanks]
  sql_generation:
    description: SQL
    tokens_in: 10
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
    cues: [sql, query, "select from", "count rows"]
"""


def _multi_env(tmp_path, monkeypatch) -> None:
    """Two tasks, so which one wins is observable."""
    (tmp_path / "policy.yaml").write_text(_MULTI_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    store = Store(tmp_path / "p.db")
    store.upsert_weights(Weights("hf:or/m", "or-model", params_b=7.0))
    store.upsert_deployment(Deployment("openrouter:m", "hf:or/m", "openrouter", "m",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    store.commit()
    store.close()


@pytest.mark.parametrize("history,expected", [
    # The task must come from the *newest* user turn. Deciding on the first one is
    # the multi-turn equivalent of the bug this whole fix is about.
    ([("hello there", "hi"), ("write a sql query and count rows", None)], "sql_generation"),
    ([("write a sql query", "ok"), ("thanks, that helps", None)], "general_chat"),
])
def test_classification_follows_the_newest_user_turn(tmp_path, monkeypatch, history, expected):
    _multi_env(tmp_path, monkeypatch)

    def factory(**kw):
        return lambda deploy_id, messages, **kw2: CallResult(
            deploy_id, text="ok", ok=True, tokens_in=1, tokens_out=1, latency_ms=1.0)

    monkeypatch.setattr("mininfer.proxy.Runner", factory)
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    messages: list[dict] = []
    for user, assistant in history:
        messages.append({"role": "user", "content": user})
        if assistant:
            messages.append({"role": "assistant", "content": assistant})
    r = client.post("/v1/chat/completions", json={"model": "auto", "messages": messages})
    assert r.status_code == 200
    assert r.json()["mi"]["task"] == expected


def test_a_long_history_still_reserves_against_the_session(tmp_path, monkeypatch):
    """Compaction bounds what is sent; the reservation must still count it all."""
    _stream_env(tmp_path, monkeypatch)
    sent: list[list[dict]] = []

    def factory(**kw):
        def run(deploy_id, messages, **kw2):
            sent.append(messages)
            return CallResult(deploy_id, text="ok", ok=True, tokens_in=1,
                              tokens_out=1, latency_ms=1.0)
        return run

    monkeypatch.setattr("mininfer.proxy.Runner", factory)
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    history = [{"role": "user", "content": "x" * 300} for _ in range(5)]
    r = client.post("/v1/chat/completions",
                    headers={"X-MI-Session": "s-multi"},
                    json={"model": "t", "max_tokens": 8, "messages": history})
    assert r.status_code == 200
    assert len(sent[0]) == 5
    assert r.json()["mi"]["session"]["tokens"] == 2


def test_complexity_routing_and_logging_in_proxy(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    store = Store(tmp_path / "p.db")

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)

    # 1. High complexity prompt
    client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "Prove by induction that sum of cubes is square of sum"}]}
    )
    dec_high = store.recent_decisions(limit=1)[0]
    reason_high = json.loads(dec_high["reason"])
    assert "complexity" in reason_high
    assert reason_high["complexity"]["level"] == "high"
    assert reason_high["complexity"]["needs_reasoning"] is True

    # 2. Low complexity prompt
    client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "Extract all email addresses from: alice@example.com"}]}
    )
    dec_low = store.recent_decisions(limit=1)[0]
    reason_low = json.loads(dec_low["reason"])
    assert "complexity" in reason_low
    assert reason_low["complexity"]["level"] == "low"
    assert reason_low["complexity"]["needs_reasoning"] is False

    # 3. Explicit override
    client.post(
        "/v1/chat/completions",
        json={"model": "auto", "complexity": "high", "messages": [{"role": "user", "content": "hello"}]}
    )
    dec_override = store.recent_decisions(limit=1)[0]
    reason_override = json.loads(dec_override["reason"])
    assert reason_override["complexity"]["source"] == "override"
    assert reason_override["complexity"]["level"] == "high"
    store.close()


def test_complexity_escalation_on_low_model_validation_failure(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    store = Store(tmp_path / "p.db")
    store.upsert_weights(Weights("hf:low/m", "low-model", params_b=1.0, benchmark={"aa_intelligence": 10.0, "coding": 10.0}))
    store.upsert_weights(Weights("hf:cheap/m", "cheap-model", params_b=7.0, benchmark={"aa_intelligence": 95.0, "coding": 95.0}))
    store.upsert_deployment(Deployment("cheap:model", "hf:cheap/m", "cheap", "model",
                                       price_in=0.01, price_out=0.01, context_window=8000, caps={"structured": True}))
    store.upsert_weights(Weights("hf:reasoning/m", "reasoning-model", params_b=70.0, benchmark={"aa_intelligence": 100.0, "coding": 100.0}))
    store.upsert_deployment(Deployment("reasoning:model", "hf:reasoning/m", "reasoning", "model",
                                       price_in=50.0, price_out=50.0, context_window=8000, caps={"structured": True, "reasoning": True}))
    store.commit()
    store.close()

    monkeypatch.setattr("mininfer.router.available_providers", lambda **k: {"cheap", "reasoning"})
    monkeypatch.setattr("mininfer.proxy._keys_available", lambda did, *a, **k: True)

    from mininfer.router import Policy
    orig_load = Policy.load
    def patched_load(path):
        pol, tasks = orig_load(path)
        pol.top_k = 1
        return pol, tasks
    monkeypatch.setattr("mininfer.router.Policy.load", patched_load)

    def fake_runner(**kw):
        def run(deploy_id, messages, **kw2):
            if deploy_id == "cheap:model":
                return CallResult(deploy_id, text="", ok=False, error_class="empty_content",
                                  tokens_in=5, tokens_out=0)
            return CallResult(deploy_id, text="escalated success", ok=True,
                              tokens_in=10, tokens_out=5)
        return run

    monkeypatch.setattr("mininfer.proxy.Runner", fake_runner)
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)

    # Prompt that starts as low complexity
    r = client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "Extract all email addresses from snippet"}]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "escalated success"
    assert body["mi"]["selected_model"] == "reasoning:model"
    assert body["mi"]["reason"].get("complexity_escalated") is True
    assert body["mi"]["reason"]["complexity"]["source"] == "escalation"

    # Regression: the decision row is written *before* execution, so the escalation
    # has to be written back explicitly. It used to live only on the in-memory
    # reason, which made `mi complexity --report`'s escalation rate permanently 0.
    reopened = Store(tmp_path / "p.db")
    persisted = json.loads(reopened.recent_decisions(limit=1)[0]["reason"])
    assert persisted.get("complexity_escalated") is True
    assert persisted["complexity"]["source"] == "escalation"
    assert persisted["selected"][0]["deploy_id"] == "reasoning:model"
    reopened.close()


def test_complexity_never_reaches_the_provider_body():
    """`complexity` is MinInfer's own field; providers reject unknown keys."""
    from mininfer.proxy import _extra

    forwarded = _extra({"model": "auto", "complexity": "high", "task": "code_edit",
                        "stream": True, "mi_options": 2, "x-mi-task": "sql_generation",
                        "messages": [{"role": "user", "content": "hi"}], "tools": []})
    for mininfer_only in ("model", "complexity", "task", "stream", "mi_options", "x-mi-task"):
        assert mininfer_only not in forwarded, mininfer_only
    assert forwarded["messages"] and "tools" in forwarded


def test_escalation_is_skipped_when_the_session_budget_refuses(tmp_path, monkeypatch):
    """The escalation is a second paid call and must clear the same ceiling.

    Stubbing the gate rather than tuning limits keeps this a test of the control
    flow: no upstream call may happen once the budget check refuses.
    """
    import json

    from mininfer import proxy as proxy_mod

    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    setup = Store(tmp_path / "p.db")
    # This weight has no deployment: it exists to anchor the benchmark norms.
    # `benchmark_norms` maps each key to its p5/p95 across the registry, so with
    # only two high-scoring arms cheap:model's 95 sits at p5 and its prior collapses
    # to the 0.30 floor — it would be rejected for quality and never tried, which
    # would make this test pass without exercising the escalation at all.
    setup.upsert_weights(Weights("hf:low/m", "low-model", params_b=1.0,
                                 benchmark={"aa_intelligence": 10.0, "coding": 10.0}))
    setup.upsert_weights(Weights("hf:cheap/m", "cheap-model", params_b=7.0,
                                 benchmark={"aa_intelligence": 95.0, "coding": 95.0}))
    setup.upsert_deployment(Deployment("cheap:model", "hf:cheap/m", "cheap", "model",
                                       price_in=0.01, price_out=0.01, context_window=8000,
                                       caps={"structured": True}))
    setup.upsert_weights(Weights("hf:reasoning/m", "reasoning-model", params_b=70.0,
                                 benchmark={"aa_intelligence": 100.0, "coding": 100.0}))
    setup.upsert_deployment(Deployment("reasoning:model", "hf:reasoning/m", "reasoning", "model",
                                       price_in=50.0, price_out=50.0, context_window=8000,
                                       caps={"structured": True, "reasoning": True}))
    setup.commit()
    setup.close()

    monkeypatch.setattr("mininfer.router.available_providers", lambda **k: {"cheap", "reasoning"})
    monkeypatch.setattr("mininfer.proxy._keys_available", lambda did, *a, **k: True)

    from mininfer.router import Policy as _Policy
    orig_load = _Policy.load

    def patched_load(path):
        pol, tasks = orig_load(path)
        pol.top_k = 1
        return pol, tasks

    monkeypatch.setattr("mininfer.router.Policy.load", patched_load)

    calls, gate_calls = [], {"n": 0}

    def fake_runner(**kw):
        def run(deploy_id, messages, **kw2):
            calls.append(deploy_id)
            return CallResult(deploy_id, text="", ok=False,
                              error_class="empty_content", tokens_in=5, tokens_out=0)
        return run

    def gate(*a, **k):
        gate_calls["n"] += 1
        if gate_calls["n"] == 1:
            return None                       # the first call is affordable
        return proxy_mod._error(429, "session_budget_exceeded", "no headroom for escalation")

    monkeypatch.setattr("mininfer.proxy.Runner", fake_runner)
    monkeypatch.setattr("mininfer.proxy._session_block", gate)

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "Extract all emails"}]})

    # 502 is the right answer here: the low arm failed and the escalation was
    # refused, so no arm answered. What matters is that no second call was made.
    assert r.status_code == 502, r.text
    assert calls == ["cheap:model"], f"escalated despite the budget gate: {calls}"
    assert gate_calls["n"] == 2, "the escalation must re-check the budget"

    reopened = Store(tmp_path / "p.db")
    persisted = json.loads(reopened.recent_decisions(limit=1)[0]["reason"])
    assert persisted.get("complexity_escalation_blocked") == "session_budget"
    assert not persisted.get("complexity_escalated")
    reopened.close()


def test_escalation_is_off_unless_the_policy_opts_in(tmp_path, monkeypatch):
    """Absent `complexity.escalation`, a missing block must mean off.

    Escalation spends a second, more expensive call, so the code default is off and
    the shipped policy opts in explicitly. Drop the block and no retry may happen.
    """
    import json

    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    setup = Store(tmp_path / "p.db")
    setup.upsert_weights(Weights("hf:low/m", "low-model", params_b=1.0,
                                 benchmark={"aa_intelligence": 10.0, "coding": 10.0}))
    setup.upsert_weights(Weights("hf:cheap/m", "cheap-model", params_b=7.0,
                                 benchmark={"aa_intelligence": 95.0, "coding": 95.0}))
    setup.upsert_deployment(Deployment("cheap:model", "hf:cheap/m", "cheap", "model",
                                       price_in=0.01, price_out=0.01, context_window=8000,
                                       caps={"structured": True}))
    setup.upsert_weights(Weights("hf:reasoning/m", "reasoning-model", params_b=70.0,
                                 benchmark={"aa_intelligence": 100.0, "coding": 100.0}))
    setup.upsert_deployment(Deployment("reasoning:model", "hf:reasoning/m", "reasoning", "model",
                                       price_in=50.0, price_out=50.0, context_window=8000,
                                       caps={"structured": True, "reasoning": True}))
    setup.commit()
    setup.close()

    monkeypatch.setattr("mininfer.router.available_providers", lambda **k: {"cheap", "reasoning"})
    monkeypatch.setattr("mininfer.proxy._keys_available", lambda did, *a, **k: True)

    from mininfer.router import Policy as _Policy
    orig_load = _Policy.load

    def patched_load(path):
        pol, tasks = orig_load(path)
        pol.top_k = 1
        pol.complexity = {k: v for k, v in pol.complexity.items() if k != "escalation"}
        return pol, tasks

    monkeypatch.setattr("mininfer.router.Policy.load", patched_load)

    calls: list[str] = []

    def fake_runner(**kw):
        def run(deploy_id, messages, **kw2):
            calls.append(deploy_id)
            return CallResult(deploy_id, text="", ok=False,
                              error_class="empty_content", tokens_in=5, tokens_out=0)
        return run

    monkeypatch.setattr("mininfer.proxy.Runner", fake_runner)

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "Extract all emails"}]})

    assert calls == ["cheap:model"], f"escalated without opting in: {calls}"
    assert r.status_code == 502


def test_the_judge_tier_runs_in_the_proxy_when_enabled(tmp_path, monkeypatch):
    """`judge.enabled: true` must actually reach the request path, pinned.

    Pinned to `judge.model` means the call never enters the router, so it cannot
    be classified, escalated, and recurse.
    """
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    setup = Store(tmp_path / "p.db")
    # Anchor weight with no deployment: it exists only to give `benchmark_norms` a
    # p5/p95 spread. With a single weight hi == lo, the prior collapses to 0.625,
    # p_lb lands under every quality floor, and nothing is ever eligible.
    setup.upsert_weights(Weights("hf:low/m", "low-model", params_b=1.0,
                                 benchmark={"aa_intelligence": 10.0}))
    setup.upsert_weights(Weights("hf:m", "m", params_b=7.0, benchmark={"aa_intelligence": 90.0}))
    # Generous on purpose: the ambiguous prompt may classify into a demanding task
    # (`code_edit` wants 32k + tools) and this test is about the judge, not routing.
    setup.upsert_deployment(Deployment("openrouter:m", "hf:m", "openrouter", "m",
                                       price_in=0.1, price_out=0.1, context_window=200000,
                                       caps={"tools": True, "structured": True, "vision": True}))
    setup.commit()
    setup.close()

    monkeypatch.setattr("mininfer.router.available_providers", lambda **k: {"openrouter"})
    monkeypatch.setattr("mininfer.proxy._keys_available", lambda did, *a, **k: True)

    from mininfer.router import Policy as _Policy
    orig_load = _Policy.load

    def patched_load(path):
        pol, tasks = orig_load(path)
        pol.complexity = {**pol.complexity,
                          "judge": {"enabled": True, "model": "groq:judge"}}
        return pol, tasks

    monkeypatch.setattr("mininfer.router.Policy.load", patched_load)

    # The judge cache is a module global keyed by prompt, so a prior test could
    # otherwise satisfy this request without ever calling the judge.
    from mininfer import complexity as cm
    cm._DEFAULT_CACHE._cache.clear()

    seen: list[str] = []

    def fake_runner(**kw):
        def run(deploy_id, messages, **kw2):
            seen.append(deploy_id)
            if deploy_id == "groq:judge":
                return CallResult(deploy_id, text='{"level":"high","needs_reasoning":true,"reason":"judged"}',
                                  ok=True, tokens_in=1, tokens_out=1)
            return CallResult(deploy_id, text="answer", ok=True, tokens_in=1, tokens_out=1)
        return run

    # make_judge imports Runner lazily from execute; the chat call uses proxy's.
    monkeypatch.setattr("mininfer.execute.Runner", fake_runner)
    monkeypatch.setattr("mininfer.proxy.Runner", fake_runner)

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "auto",
                          "messages": [{"role": "user",
                                        "content": "First refactor the module, then update the tests"}]})

    assert r.status_code == 200, r.text
    assert "groq:judge" in seen, f"judge never called: {seen}"
    assert r.json()["mi"]["reason"]["complexity"]["source"] == "judge"


def test_outcomes_are_tagged_with_the_effort_they_were_routed_under(tmp_path, monkeypatch):
    """#3: without the tag, difficulty can never be recalibrated from outcomes.

    Observations were keyed `(deploy_id, task)`, so a model that is good at easy
    prompts and bad at hard ones averaged into one mediocre number and the
    difficulty signal had no feedback loop at all.
    """
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    setup = Store(tmp_path / "p.db")
    setup.upsert_weights(Weights("hf:low/m", "low-model", params_b=1.0,
                                 benchmark={"aa_intelligence": 10.0, "coding": 10.0}))
    setup.upsert_weights(Weights("hf:m", "m", params_b=7.0,
                                 benchmark={"aa_intelligence": 90.0, "coding": 90.0}))
    setup.upsert_deployment(Deployment("openrouter:m", "hf:m", "openrouter", "m",
                                       price_in=0.1, price_out=0.1, context_window=200000,
                                       caps={"tools": True, "structured": True, "vision": True,
                                             "reasoning": True}))
    setup.commit()
    setup.close()

    monkeypatch.setattr("mininfer.router.available_providers", lambda **k: {"openrouter"})
    monkeypatch.setattr("mininfer.proxy._keys_available", lambda did, *a, **k: True)

    def fake_runner(**kw):
        def run(deploy_id, messages, **kw2):
            return CallResult(deploy_id, text="ok", ok=True, tokens_in=1, tokens_out=1)
        return run

    monkeypatch.setattr("mininfer.proxy.Runner", fake_runner)

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    for prompt in ("Extract all email addresses from the snippet",
                   "Prove by induction that the sum of cubes is a square"):
        assert client.post("/v1/chat/completions",
                           json={"model": "auto",
                                 "messages": [{"role": "user", "content": prompt}]}).status_code == 200

    reopened = Store(tmp_path / "p.db")
    by_effort = reopened.routing_stats_by_effort()
    efforts = {key[2] for key in by_effort}
    assert {"low", "high"} <= efforts, f"outcomes not tagged per effort: {efforts}"
    for (deploy_id, _task, effort), row in by_effort.items():
        assert deploy_id == "openrouter:m"
        assert row["n"] >= 1 and row["wins"] >= 1, (effort, row)
    reopened.close()





def test_the_streaming_route_frame_carries_complexity(tmp_path, monkeypatch):
    """The dashboard always streams, and the full `reason` block is non-streaming
    only. Without `complexity` on the route frame the decision that drove the
    choice is invisible in the UI — which is exactly the state this fixed.
    """
    import json

    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    setup = Store(tmp_path / "p.db")
    # Anchor weight (no deployment) so `benchmark_norms` has a p5/p95 spread; with
    # a single weight hi == lo and nothing clears a quality floor.
    setup.upsert_weights(Weights("hf:low/m", "low-model", params_b=1.0,
                                 benchmark={"aa_intelligence": 10.0, "coding": 10.0}))
    setup.upsert_weights(Weights("hf:m", "m", params_b=7.0,
                                 benchmark={"aa_intelligence": 90.0, "coding": 90.0}))
    setup.upsert_deployment(Deployment("openrouter:m", "hf:m", "openrouter", "m",
                                       price_in=0.1, price_out=0.1, context_window=200000,
                                       caps={"tools": True, "structured": True,
                                             "vision": True, "reasoning": True}))
    setup.commit()
    setup.close()

    monkeypatch.setattr("mininfer.router.available_providers", lambda **k: {"openrouter"})
    monkeypatch.setattr("mininfer.proxy._keys_available", lambda did, *a, **k: True)
    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))
    monkeypatch.setattr("mininfer.proxy.open_stream",
                        lambda ep, body, *, deploy_id, timeout=120.0: _FakeSession(
                            200, [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                                  b'data: [DONE]\n\n']))

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "auto", "stream": True,
                          "messages": [{"role": "user",
                                        "content": "Prove by induction that sum(i^2) = n(n+1)(2n+1)/6"}]})
    assert r.status_code == 200, r.text

    frames = [json.loads(line[len("data: "):]) for line in r.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    route = next((f["mi"] for f in frames
                  if isinstance(f.get("mi"), dict) and f["mi"].get("event") == "route"), None)
    assert route is not None, r.text
    assert route["complexity"]["level"] == "high"
    assert route["complexity"]["needs_reasoning"] is True
    assert route["complexity"]["signals"], "the fired cues must reach the client"
    assert route["complexity_escalated"] is False

    # The streamed path must tag its outcome too: the dashboard always streams, so
    # an untagged stream would leave effort-learning with almost no data.
    s = Store(tmp_path / "p.db")
    efforts = [r["effort"] for r in
               s.conn.execute("SELECT effort FROM observations").fetchall()]
    assert "high" in efforts, efforts
    s.close()


def _escalation_registry(tmp_path):
    """two tiers: a cheap arm and a reasoning arm, plus a benchmark norm anchor."""
    setup = Store(tmp_path / "p.db")
    setup.upsert_weights(Weights("hf:low/m", "low-model", params_b=1.0,
                                 benchmark={"aa_intelligence": 10.0, "coding": 10.0}))
    setup.upsert_weights(Weights("hf:cheap/m", "cheap-model", params_b=7.0,
                                 benchmark={"aa_intelligence": 95.0, "coding": 95.0}))
    setup.upsert_deployment(Deployment("cheap:model", "hf:cheap/m", "cheap", "model",
                                       price_in=0.01, price_out=0.01, context_window=8000,
                                       caps={"structured": True}))
    setup.upsert_weights(Weights("hf:reasoning/m", "reasoning-model", params_b=70.0,
                                 benchmark={"aa_intelligence": 100.0, "coding": 100.0}))
    setup.upsert_deployment(Deployment("reasoning:model", "hf:reasoning/m", "reasoning", "model",
                                       price_in=50.0, price_out=50.0, context_window=8000,
                                       caps={"structured": True, "reasoning": True}))
    setup.commit()
    setup.close()


def _one_arm_per_pass(monkeypatch):
    from mininfer.router import Policy as _Policy
    orig_load = _Policy.load

    def patched_load(path):
        pol, tasks = orig_load(path)
        pol.top_k = 1          # one arm per pass, so the retry is unambiguous
        return pol, tasks

    monkeypatch.setattr("mininfer.router.Policy.load", patched_load)


def test_streaming_escalates_when_every_arm_fails_before_the_first_byte(tmp_path, monkeypatch):
    """The streaming escalation, before the first frame.

    Escalation used to live only on the non-streaming path, so the dashboard —
    which always streams — could never show it. It has to run before any byte is
    emitted, or the caller is already committed to a dead stream.
    """
    import json

    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    _escalation_registry(tmp_path)
    monkeypatch.setattr("mininfer.router.available_providers", lambda **k: {"cheap", "reasoning"})
    monkeypatch.setattr("mininfer.proxy._keys_available", lambda did, *a, **k: True)
    _one_arm_per_pass(monkeypatch)
    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))

    tried: list[str] = []

    def fake_open_stream(ep, body, *, deploy_id, timeout=120.0):
        tried.append(deploy_id)
        if deploy_id == "cheap:model":
            return _FakeSession(500, [], error_class="http_500")
        return _FakeSession(200, [b'data: {"choices":[{"delta":{"content":"escalated"}}]}\n\n',
                                  b'data: [DONE]\n\n'])

    monkeypatch.setattr("mininfer.proxy.open_stream", fake_open_stream)

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "auto", "stream": True,
                          "messages": [{"role": "user", "content": "Extract all emails"}]})

    assert r.status_code == 200, r.text
    assert tried == ["cheap:model", "reasoning:model"], tried
    assert "escalated" in r.text

    frames = [json.loads(line[len("data: "):]) for line in r.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    route = next(f["mi"] for f in frames
                 if isinstance(f.get("mi"), dict) and f["mi"].get("event") == "route")
    assert route["deploy"] == "reasoning:model"
    assert route["complexity_escalated"] is True, route
    assert route["complexity"]["source"] == "escalation"

    # Persisted, not just streamed — otherwise the report shows 0% forever.
    reopened = Store(tmp_path / "p.db")
    persisted = json.loads(reopened.recent_decisions(limit=1)[0]["reason"])
    assert persisted.get("complexity_escalated") is True
    assert persisted["selected"][0]["deploy_id"] == "reasoning:model"

    # The failed pass is tagged `low` and the retry `high`. Tagging both the same
    # would destroy the very signal effort-learning is built on.
    rows = reopened.conn.execute(
        "SELECT deploy_id, effort FROM observations ORDER BY id").fetchall()
    assert [(r["deploy_id"], r["effort"]) for r in rows] == [
        ("cheap:model", "low"), ("reasoning:model", "high")
    ], [dict(r) for r in rows]
    reopened.close()


def test_streaming_escalation_is_blocked_by_the_session_budget(tmp_path, monkeypatch):
    """The streaming retry is a second paid call, so it clears the same ceiling."""
    import json

    from mininfer import proxy as proxy_mod

    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    _escalation_registry(tmp_path)
    monkeypatch.setattr("mininfer.router.available_providers", lambda **k: {"cheap", "reasoning"})
    monkeypatch.setattr("mininfer.proxy._keys_available", lambda did, *a, **k: True)
    _one_arm_per_pass(monkeypatch)
    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))

    # Allow the initial check (or the request never starts) and refuse only the
    # escalation's re-check — a blanket patch would 429 the request upfront and
    # this test would pass without exercising the retry gate at all.
    gate_calls = {"n": 0}

    def gate(*a, **k):
        gate_calls["n"] += 1
        if gate_calls["n"] == 1:
            return None
        return proxy_mod._error(429, "session_budget_exceeded", "no headroom for escalation")

    monkeypatch.setattr("mininfer.proxy._session_block", gate)

    tried: list[str] = []

    def fake_open_stream(ep, body, *, deploy_id, timeout=120.0):
        tried.append(deploy_id)
        return _FakeSession(500, [], error_class="http_500")

    monkeypatch.setattr("mininfer.proxy.open_stream", fake_open_stream)

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "auto", "stream": True,
                          "messages": [{"role": "user", "content": "Extract all emails"}]})

    assert r.status_code == 502, r.text
    assert tried == ["cheap:model"], f"escalated despite the budget gate: {tried}"
    assert gate_calls["n"] == 2, "the escalation must re-check the budget"

    reopened = Store(tmp_path / "p.db")
    persisted = json.loads(reopened.recent_decisions(limit=1)[0]["reason"])
    assert persisted.get("complexity_escalation_blocked") == "session_budget"
    assert not persisted.get("complexity_escalated")
    reopened.close()


# --------------------------------------------------------------------------- #
# stream verification (opt-in): a 200 that produced garbage is a failed arm
# --------------------------------------------------------------------------- #

_PROMPT = "Extract the email and the phone number from this snippet"
_VALID = '{"email": "a@b.com", "phone": "+1 555 0100"}'


def test_should_verify_gates_on_policy_and_the_shape_of_the_task():
    from mininfer.proxy import _should_verify
    from mininfer.router import Policy
    from mininfer.schema import TaskProfile

    structured = TaskProfile(name="extraction", tokens_in=2500, tokens_out=300,
                             require={"structured": True})
    prose = TaskProfile(name="general_chat", tokens_in=800, tokens_out=1200, require={})

    assert not _should_verify(Policy(), structured, {}), "opt-in"
    on = Policy(stream_verify={"enabled": True})
    assert _should_verify(on, structured, {})

    assert not _should_verify(on, prose, {}), "nothing verifiable in prose"
    assert not _should_verify(on, structured, {"tools": [{}]}), "tool calls are not replayable"
    assert not _should_verify(on, None, {}), "no task profile, no check"

    long_form = TaskProfile(name="extraction", tokens_in=2500, tokens_out=4000,
                            require={"structured": True})
    assert not _should_verify(on, long_form, {}), \
        "buffering a long answer trades the streaming UX for a check nobody asked for"


def test_structured_verdict_accepts_json_and_rejects_prose():
    from mininfer.proxy import _structured_verdict

    assert _structured_verdict(_PROMPT, _VALID, "stop") is None
    assert _structured_verdict(_PROMPT, f"```json\n{_VALID}\n```", "stop") is None

    assert _structured_verdict(_PROMPT, "Sure! Here is what I found in the text.", "stop") == "bad_output"
    assert _structured_verdict(_PROMPT, "", "stop") == "bad_output"
    # A truncated structured answer cannot parse, and the provider said so.
    assert _structured_verdict(_PROMPT, _VALID, "length") == "bad_output"


def test_stream_verify_skips_an_arm_that_answers_with_prose(tmp_path, monkeypatch):
    """The verifier's whole point.

    Without it the first arm to accept the connection wins a streaming request
    regardless of what it produced, and the caller discovers the problem when they
    try to parse it.
    """
    import json

    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    setup = Store(tmp_path / "p.db")
    setup.upsert_weights(Weights("hf:anchor", "anchor", params_b=1.0,
                                 benchmark={"aa_intelligence": 10.0, "coding": 10.0}))
    setup.upsert_weights(Weights("hf:cheap/m", "cheap-model", params_b=7.0,
                                 benchmark={"aa_intelligence": 95.0, "coding": 95.0}))
    setup.upsert_deployment(Deployment("cheap:model", "hf:cheap/m", "cheap", "model",
                                       price_in=0.01, price_out=0.01, context_window=32000,
                                       caps={"structured": True}))
    setup.upsert_weights(Weights("hf:good/m", "good-model", params_b=8.0,
                                 benchmark={"aa_intelligence": 99.0, "coding": 99.0}))
    setup.upsert_deployment(Deployment("good:model", "hf:good/m", "good", "model",
                                       price_in=0.02, price_out=0.02, context_window=32000,
                                       caps={"structured": True}))
    setup.commit()
    setup.close()

    monkeypatch.setattr("mininfer.router.available_providers", lambda **k: {"cheap", "good"})
    monkeypatch.setattr("mininfer.proxy._keys_available", lambda did, *a, **k: True)
    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))

    from mininfer.router import Policy as _Policy
    orig_load = _Policy.load

    def patched_load(path):
        pol, tasks = orig_load(path)
        pol.stream_verify = {"enabled": True, "max_output_tokens": 800}
        return pol, tasks

    monkeypatch.setattr("mininfer.router.Policy.load", patched_load)

    def sse(payload):
        return ("data: " + json.dumps(payload) + "\n\n").encode()

    prose = (sse({"choices": [{"delta": {"content": "Sure! Here is what I found in the text."}}]})
             + b"data: [DONE]\n\n")
    valid = (sse({"choices": [{"delta": {"content": _VALID}}]})
             + sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
             + b"data: [DONE]\n\n")

    tried: list[str] = []

    def fake_open_stream(ep, body, *, deploy_id, timeout=120.0):
        tried.append(deploy_id)
        return _FakeSession(200, list(_chunks(prose if deploy_id == "cheap:model" else valid)))

    monkeypatch.setattr("mininfer.proxy.open_stream", fake_open_stream)

    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)
    r = client.post("/v1/chat/completions",
                    json={"model": "auto", "stream": True,
                          "messages": [{"role": "user", "content": _PROMPT}]})

    assert r.status_code == 200, r.text
    # The prose arm was tried, rejected, and skipped — nothing of it was emitted.
    assert tried[:2] == ["cheap:model", "good:model"], tried
    assert "Sure! Here is what I found" not in r.text, "the rejected answer leaked to the caller"

    frames = [json.loads(line[len("data: "):]) for line in r.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    route = next(f["mi"] for f in frames
                 if isinstance(f.get("mi"), dict) and f["mi"].get("event") == "route")
    assert route["deploy"] == "good:model"
    assert "a@b.com" in r.text

    reopened = Store(tmp_path / "p.db")
    rows = {r["deploy_id"]: r for r in reopened.conn.execute(
        "SELECT deploy_id, ok, error_class FROM observations").fetchall()}
    assert rows["cheap:model"]["ok"] == 0
    assert rows["cheap:model"]["error_class"] == "bad_output"
    assert rows["good:model"]["ok"] == 1
    reopened.close()


def _chunks(blob: bytes) -> list[bytes]:
    """Split an SSE blob into frames so the fake session looks like a real one."""
    return [b"data: " + part for part in blob.split(b"data: ")[1:]]
