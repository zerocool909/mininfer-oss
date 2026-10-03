"""Server-side transcripts: the content half of a session.

`sessions` is a spend ledger — it answers "what did this cost" and nothing about
what was said. This file pins the other half: only a *named* session is recorded,
the newest user message is what gets stored (not the re-sent context), and each
session is bounded so a long conversation cannot grow the store without limit.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mininfer.execute import CallResult, Endpoint
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


def _env(tmp_path, monkeypatch) -> TestClient:
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    store = Store(tmp_path / "p.db")
    store.upsert_weights(Weights("hf:m", "m", benchmark={"coding": 60.0}))
    store.upsert_deployment(Deployment(
        "openrouter:m", "hf:m", "openrouter", "m", price_in=1.0, price_out=1.0,
        context_window=1000))
    store.commit()
    store.close()
    return TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)


def _runner(**kw):
    return lambda deploy_id, messages, **kw2: CallResult(
        deploy_id, text="the answer", ok=True, tokens_in=7, tokens_out=3, latency_ms=5.0)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def test_messages_round_trip_in_order(tmp_path):
    s = Store(tmp_path / "t.db")
    s.record_messages("s1", [{"role": "user", "content": "first"}])
    s.record_messages("s1", [{"role": "assistant", "content": "second"}])
    s.record_messages("s1", [{"role": "user", "content": "third"}])
    s.commit()
    assert [m["content"] for m in s.session_messages("s1")] == ["first", "second", "third"]
    s.close()


def test_blank_content_and_unknown_roles_are_dropped(tmp_path):
    s = Store(tmp_path / "t.db")
    added = s.record_messages("s1", [
        {"role": "user", "content": "  "},         # blank
        {"role": "system", "content": "ok"},        # kept
        {"role": "wizard", "content": "no"},        # unknown role
        {"role": "assistant", "content": ""},       # blank
        "not a dict",
    ])
    s.commit()
    assert added == 1
    assert [m["role"] for m in s.session_messages("s1")] == ["system"]
    s.close()


def test_a_runaway_message_is_truncated(tmp_path):
    s = Store(tmp_path / "t.db")
    s.record_messages("s1", [{"role": "user", "content": "x" * (s.MESSAGE_MAX_CHARS + 500)}])
    s.commit()
    assert len(s.session_messages("s1")[0]["content"]) == s.MESSAGE_MAX_CHARS
    s.close()


def test_a_session_keeps_only_the_newest_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(Store, "TRANSCRIPT_LIMIT", 5)
    s = Store(tmp_path / "t.db")
    for i in range(12):
        s.record_messages("s1", [{"role": "user", "content": f"m{i}"}])
    s.commit()
    kept = [m["content"] for m in s.session_messages("s1")]
    assert kept == [f"m{i}" for i in range(7, 12)]
    s.close()


def test_no_session_writes_nothing(tmp_path):
    s = Store(tmp_path / "t.db")
    assert s.record_messages(None, [{"role": "user", "content": "x"}]) == 0
    assert s.session_messages(None) == []
    s.close()


def test_clear_removes_the_transcript_not_the_ledger(tmp_path):
    s = Store(tmp_path / "t.db")
    s.record_messages("s1", [{"role": "user", "content": "x"}])
    s.add_session_usage("s1", tokens_in=5, tokens_out=5)
    s.commit()
    assert s.clear_messages("s1") == 1
    s.commit()
    assert s.session_messages("s1") == []
    assert s.session_usage("s1")["calls"] == 1
    s.close()


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #


def test_messages_endpoint_requires_a_session(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)
    r = client.get("/v1/session/messages")
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "no_session"


def test_a_non_streamed_turn_is_recorded(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.proxy.Runner", _runner)
    r = client.post("/v1/chat/completions",
                    headers={"X-MI-Session": "s-1"},
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200

    gets = client.get("/v1/session/messages", headers={"X-MI-Session": "s-1"}).json()
    assert [m["role"] for m in gets["messages"]] == ["user", "assistant"]
    assert gets["messages"][0]["content"] == "hi"
    assert gets["messages"][1]["content"] == "the answer"
    assert gets["messages"][1]["deploy_id"] == "openrouter:m"


def test_only_the_newest_user_message_is_stored(tmp_path, monkeypatch):
    """The client re-sends context; the transcript must not duplicate it."""
    client = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.proxy.Runner", _runner)
    client.post("/v1/chat/completions", headers={"X-MI-Session": "s-2"},
                json={"model": "t", "messages": [{"role": "user", "content": "one"}]})
    client.post("/v1/chat/completions", headers={"X-MI-Session": "s-2"},
                json={"model": "t", "messages": [
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "the answer"},
                    {"role": "user", "content": "two"},
                ]})
    roles = [m["content"] for m in
             client.get("/v1/session/messages", headers={"X-MI-Session": "s-2"}).json()["messages"]]
    assert roles == ["one", "the answer", "two", "the answer"]


def test_no_session_means_no_transcript(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.proxy.Runner", _runner)
    client.post("/v1/chat/completions",
                json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    store = Store(tmp_path / "p.db")
    assert store.conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 0
    store.close()


def test_a_streamed_turn_records_the_relayed_answer(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)

    class _Sess:
        def __init__(self):
            self.status_code, self.error_class, self.headers = 200, None, {}

        def chunks(self):
            yield b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        def close(self):
            pass

    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))
    monkeypatch.setattr("mininfer.proxy.open_stream",
                        lambda ep, body, *, deploy_id, timeout=120.0: _Sess())
    r = client.post("/v1/chat/completions", headers={"X-MI-Session": "s-3"},
                    json={"model": "t", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    msgs = client.get("/v1/session/messages", headers={"X-MI-Session": "s-3"}).json()["messages"]
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "hi"), ("assistant", "hello")]


def test_delete_clears_only_the_transcript(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.proxy.Runner", _runner)
    client.post("/v1/chat/completions", headers={"X-MI-Session": "s-4"},
                json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    assert client.get("/v1/session", headers={"X-MI-Session": "s-4"}).json()["calls"] == 1

    r = client.delete("/v1/session/messages", headers={"X-MI-Session": "s-4"})
    assert r.status_code == 200 and r.json()["cleared"] == 2
    assert client.get("/v1/session/messages",
                      headers={"X-MI-Session": "s-4"}).json()["messages"] == []
    # The spend the call caused is the operator's accounting, not the caller's.
    assert client.get("/v1/session", headers={"X-MI-Session": "s-4"}).json()["calls"] == 1


def test_endpoints_reject_a_clear_without_a_session(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)
    r = client.delete("/v1/session/messages")
    assert r.status_code == 400
