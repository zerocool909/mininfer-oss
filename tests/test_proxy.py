"""Tests for the OpenAI-compatible proxy and its fallback execution.

The contract that must not drift: a failed primary (429/5xx/timeout) must move to
the next candidate and record *both* attempts as observations — otherwise a 429
never demotes the arm that produced it (PLAN.md §3).
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
