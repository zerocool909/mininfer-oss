"""A truncated answer must not be shown as a complete one.

The upstream failure this pins is real and reproducible: `qwen/qwen3.8-27b` on
Groq intermittently emits EOS mid-answer — always right after a markdown heading,
a line ending in `:`, or an unclosed `**` — and reports `finish_reason: "stop"`.
The provider therefore *cannot* tell us it was cut, so the relay showed a
half-answer as if it were whole.

MinInfer now recognises those shapes (and `finish_reason == "length"`), asks the
same model to continue once, and emits a single `[DONE]` at the very end. These
tests pin the detector, the continuation on both stream paths, and the
`finish_reason` capture that makes `length` visible at all.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import mininfer.proxy as proxy
from mininfer.execute import Endpoint, Runner, call_body
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


# --------------------------------------------------------------------------- #
# the detector
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,finish,expected", [
    ("### Step", "stop", True),                 # heading with nothing under it
    ("Here is the plan:", "stop", True),        # a line that promised a list
    ("**Create the Core", "stop", True),        # unclosed bold
    ("use a `code", "stop", True),              # unclosed backtick
    ("", "stop", False),
    ("A complete answer.", "stop", False),
    ("Ends with a word", "stop", False),
    ("anything at all", "length", True),        # the provider tells us
    ("tool output", "tool_calls", False),       # not a truncation
])
def test_looks_truncated(text, finish, expected):
    assert proxy._looks_truncated(text, finish) is expected


def test_strip_done_removes_only_the_terminator():
    assert proxy._strip_done(_DONE).strip() == b""
    mixed = _delta("x") + _DONE
    out = proxy._strip_done(mixed)
    assert b"[DONE]" not in out and b'"content": "x"' in out


# --------------------------------------------------------------------------- #
# non-stream: finish_reason is captured, and there is no implicit cap
# --------------------------------------------------------------------------- #

def _mock(monkeypatch, payload, captured=None):
    def handler(request):
        if captured is not None:
            captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler)))


def test_call_body_captures_finish_reason(monkeypatch):
    _mock(monkeypatch, {"choices": [{"message": {"content": "half"},
                                     "finish_reason": "length"}]})
    ep = Endpoint("https://e/v1", "m", "k", {"Authorization": "Bearer k"})
    res = call_body(ep, {"model": "m", "messages": []}, deploy_id="d")
    assert res.ok and res.finish_reason == "length"


def test_runner_does_not_implicitly_cap_tokens(monkeypatch):
    """A default of 1024 silently truncated every long non-streamed answer."""
    captured = {}
    _mock(monkeypatch, {"choices": [{"message": {"content": "x"},
                                     "finish_reason": "stop"}]}, captured)
    runner = Runner(api_key="k")
    runner("openrouter:m", [{"role": "user", "content": "hi"}])
    assert "max_tokens" not in captured["body"]

    # ...but an explicit cap is still honoured.
    runner("openrouter:m", [{"role": "user", "content": "hi"}], max_tokens=64)
    assert captured["body"]["max_tokens"] == 64


# --------------------------------------------------------------------------- #
# the single stream
# --------------------------------------------------------------------------- #

class _Sess:
    def __init__(self, parts):
        self.status_code, self.error_class, self.headers = 200, None, {}
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
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " price_in, price_out, context_window) VALUES ('openrouter:m','hf:m',"
        "'openrouter','m',0.0,0.0,1000)")
    store.commit()
    store.close()
    return TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)


def _streamed_text(resp) -> str:
    out = []
    for line in resp.text.splitlines():
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body == "[DONE]":
            continue
        try:
            j = json.loads(body)
        except ValueError:
            continue
        for ch in j.get("choices") or []:
            out.append((ch.get("delta") or {}).get("content") or "")
    return "".join(out)


def test_a_truncated_single_stream_is_continued(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = {"n": 0}

    def fake_open(ep, body, *, deploy_id, timeout=120.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Sess([_delta("### Step"), _DONE])
        # The continuation carries the partial answer and a nudge, on the same model.
        assert body["messages"][-1]["role"] == "user"
        assert "Continue" in body["messages"][-1]["content"]
        assert body["messages"][-2]["content"] == "### Step"
        return _Sess([_delta(" 1. Fold the paper."), _DONE])

    monkeypatch.setattr(proxy, "open_stream", fake_open)
    r = client.post("/v1/chat/completions", headers={"X-MI-Session": "s1"},
                    json={"model": "t", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert calls["n"] == 2
    text = _streamed_text(r)
    assert text == "### Step 1. Fold the paper."
    assert r.text.rstrip().endswith("[DONE]")
    assert r.text.count("[DONE]") == 1, "the client must see exactly one terminator"


def test_a_complete_single_stream_is_not_continued(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = {"n": 0}

    def fake_open(ep, body, *, deploy_id, timeout=120.0):
        calls["n"] += 1
        return _Sess([_delta("A complete answer."), _DONE])

    monkeypatch.setattr(proxy, "open_stream", fake_open)
    client.post("/v1/chat/completions", headers={"X-MI-Session": "s2"},
                json={"model": "t", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]})
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# the comparison stream (an arm continues; it is not replaced)
# --------------------------------------------------------------------------- #

def _run_multiplex(tmp_path, monkeypatch, fake_open, no_continuation=False):
    import asyncio

    real_resolve = proxy.resolve_endpoint

    def fake_resolve(did, **kw):
        ep = real_resolve(did, **kw)
        ep.error, ep.api_key, ep.model = None, "k", did
        return ep

    monkeypatch.setattr(proxy, "resolve_endpoint", fake_resolve)
    monkeypatch.setattr(proxy, "open_stream", fake_open)
    monkeypatch.setenv("MI_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")
    opts = ["openrouter:one", "vercel:two", "openrouter:three"]
    info = {d: {"deploy_id": d} for d in opts}

    async def run():
        resp = await proxy._multiplex_stream(
            opts, {"messages": [{"role": "user", "content": "hi"}]},
            task_name="general_chat", policy="free_first", reason={}, info=info,
            extra={}, max_arms=2)
        events = []
        async for chunk in resp.body_iterator:
            for line in chunk.splitlines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    continue
                events.append(json.loads(payload))
        return events

    return asyncio.run(run())


def test_a_truncated_arm_is_continued_not_replaced(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_open(ep, body, *, deploy_id, timeout=120.0):
        calls["n"] += 1
        if deploy_id == "openrouter:one" and calls["n"] == 1:
            return _Sess([_delta("### Step"), _DONE])
        if deploy_id == "openrouter:one":
            return _Sess([_delta(" 1. Fold."), _DONE])
        return _Sess([_delta("ok"), _DONE])

    events = _run_multiplex(tmp_path, monkeypatch, fake_open)
    text = {}
    for j in events:
        m = j.get("mi") or {}
        if m.get("event") == "delta" and m.get("arm") == 0:
            text[0] = text.get(0, "") + (((j.get("choices") or [{}])[0].get("delta") or {}).get("content") or "")
    assert text.get(0) == "### Step 1. Fold."
    assert not any((j.get("mi") or {}).get("event") == "error" for j in events)
    assert sum(1 for j in events if (j.get("mi") or {}).get("event") == "end") == 2


def test_auto_continuation_never_fires_on_a_tool_call(tmp_path, monkeypatch):
    """An empty-text tool-call turn is complete, not truncated."""
    tool = [f"data: {json.dumps({'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'f', 'arguments': '{}'}}]}}]})}\n\n".encode(), _DONE]
    calls = {"n": 0}

    def fake_open(ep, body, *, deploy_id, timeout=120.0):
        calls["n"] += 1
        if deploy_id == "openrouter:one":
            return _Sess(tool)
        return _Sess([_delta("ok"), _DONE])

    events = _run_multiplex(tmp_path, monkeypatch, fake_open)
    assert calls["n"] == 2, "the tool-call arm must not be continued"
    assert not any((j.get("mi") or {}).get("event") == "error" for j in events)
