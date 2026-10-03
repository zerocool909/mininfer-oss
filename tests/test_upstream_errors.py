"""A gateway can fail inside a **200**.

OpenRouter (among others) reports an upstream failure as
`HTTP 200` with `error` in the body — including mid-stream, as a frame with an
empty `choices` list:

    {"id":"gen-…","error":{"message":"Upstream error from Nvidia: Service
     temporarily overloaded","code":503,"metadata":{…}}}

Read `choices[0].message.content` and it is simply absent, so the call looked
like a model that answered nothing: the comparison drew a blank pane and the arm
was recorded as a *success*. These tests pin the fix on both paths — the
non-streaming `call()` and the multiplexed comparison stream.
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest

import mininfer.proxy as proxy
from mininfer.execute import (
    CallResult,
    Endpoint,
    call,
    call_body,
    classify_error_payload,
)

# The exact payload from the bug report.
OPENROUTER_ERROR = {
    "id": "gen-1790848094-pIsPdA7EiY2R5735OYU8",
    "error": {"message": "Upstream error from Nvidia: Service temporarily overloaded",
              "code": 503, "metadata": {"error_type": "provider_overloaded"}},
}


# --------------------------------------------------------------------------- #
# the mapping
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("err,expected", [
    ({"message": "overloaded", "code": 503}, "5xx"),
    ({"code": 500}, "5xx"),
    ({"code": 429}, "429"),
    ({"code": 404, "message": "model not found"}, "http_404"),
    ({"code": "insufficient_quota"}, "429"),
    ({"code": "rate_limit_exceeded"}, "429"),
    ({"message": "Service temporarily overloaded"}, "5xx"),
    ({"message": "upstream unavailable"}, "5xx"),
    ({"message": "something else"}, "bad_output"),
    ("not a dict", "bad_output"),
])
def test_error_payloads_map_to_router_classes(err, expected):
    assert classify_error_payload(err) == expected


def test_an_overload_is_a_trial_not_an_infrastructure_error():
    """503 must stay in `n`, or the router never learns the arm is unusable."""
    from mininfer.schema import NON_MODEL_ERRORS
    assert classify_error_payload(OPENROUTER_ERROR["error"]) not in NON_MODEL_ERRORS


# --------------------------------------------------------------------------- #
# non-streaming call()
# --------------------------------------------------------------------------- #

def _mock(monkeypatch, response_json, status=200):
    def handler(request):
        return httpx.Response(status, json=response_json)
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler)))


def test_call_reads_a_200_with_an_error_body(monkeypatch):
    _mock(monkeypatch, OPENROUTER_ERROR)
    ep = Endpoint("https://example.com/v1", "m", "k", {"Authorization": "Bearer k"})
    res = call(ep, [{"role": "user", "content": "hi"}], deploy_id="d")
    assert res.ok is False
    assert res.error_class == "5xx", "a 200-with-error must not be a blank success"
    assert res.text == ""


def test_call_still_parses_a_normal_200(monkeypatch):
    _mock(monkeypatch, {"choices": [{"message": {"content": "42"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    ep = Endpoint("https://example.com/v1", "m", "k", {"Authorization": "Bearer k"})
    res = call(ep, [{"role": "user", "content": "hi"}], deploy_id="d")
    assert res.ok and res.text == "42"


# --------------------------------------------------------------------------- #
# SSE frame detection
# --------------------------------------------------------------------------- #

def _frame(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def test_sse_error_reads_a_frame_with_no_choices():
    assert proxy._sse_error(_frame(OPENROUTER_ERROR)) == "5xx"


def test_sse_error_is_none_for_a_content_frame():
    assert proxy._sse_error(_frame({"choices": [{"delta": {"content": "hi"}}]})) is None


def test_sse_error_is_none_for_done_and_garbage():
    assert proxy._sse_error(b"data: [DONE]\n\n") is None
    assert proxy._sse_error(b"data: {not json}\n\n") is None
    assert proxy._sse_error(b": keep-alive\n\n") is None


# --------------------------------------------------------------------------- #
# the provider's own message survives the classification
# --------------------------------------------------------------------------- #

def test_call_body_keeps_the_providers_error_message(monkeypatch):
    """`http_404` alone cannot distinguish 'retired model' from 'API not enabled'."""
    _mock(monkeypatch, {"error": {
        "message": "models/gemini-2.0-flash is not found for API version v1beta",
        "code": 404}}, status=404)
    ep = Endpoint("https://e/v1", "m", "k", {"Authorization": "Bearer k"})
    res = call_body(ep, {"model": "m", "messages": []}, deploy_id="d")
    assert res.error_class == "http_404"
    assert "not found for API version" in (res.error_detail or "")


def test_all_failed_surfaces_the_detail_to_the_caller():
    res = CallResult("d", error_class="http_404", error_detail="model not found")
    body = json.loads(proxy._all_failed(
        [proxy.Attempt("d", False, "http_404", 1.0, 0, 0, "model not found")], res).body)
    assert "model not found" in body["error"]["message"]


# --------------------------------------------------------------------------- #
# a comparison: the blank pane, reproduced
# --------------------------------------------------------------------------- #

class _FakeSession:
    def __init__(self, status_code, chunks, error_class=None):
        self.status_code = status_code
        self.error_class = error_class
        self.headers = {}
        self._chunks = chunks

    def chunks(self):
        return iter(self._chunks)

    def close(self):
        pass


def _sse_text(text: str) -> bytes:
    return _frame({"choices": [{"index": 0, "delta": {"content": text}}]})


def _run_multiplex(tmp_path, monkeypatch, fake_open,
                   opts=("bad:first", "good:one", "good:two")):
    real_resolve = proxy.resolve_endpoint

    def fake_resolve(did, **kw):
        ep = real_resolve(did, **kw)
        ep.error = None          # the invented provider names must resolve
        ep.api_key = "k"
        ep.model = did
        return ep

    monkeypatch.setattr(proxy, "resolve_endpoint", fake_resolve)
    monkeypatch.setattr(proxy, "open_stream", fake_open)
    monkeypatch.setenv("MI_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")

    opts = list(opts)
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
                m = (json.loads(payload).get("mi") or {})
                events.append((m.get("event"), m.get("arm"),
                               m.get("deploy_id"), m.get("error"), m.get("replaces")))
        return events

    return asyncio.run(run())


def test_a_200_stream_with_an_upstream_error_is_a_failed_arm(tmp_path, monkeypatch):
    """The blank comparison: an error frame must fail the arm, not end it empty."""
    def fake_open(endpoint, body, **kw):
        if kw.get("deploy_id", "").startswith("bad:"):
            return _FakeSession(200, [_frame(OPENROUTER_ERROR)])
        return _FakeSession(200, [_sse_text("ok")])

    events = _run_multiplex(tmp_path, monkeypatch, fake_open)

    # The bad arm is replaced by a spare, so the caller still gets two answers...
    assert any(e[0] == "arm" and e[1] == 0 and e[4] is True for e in events), events
    assert sum(1 for e in events if e[0] == "end") == 2, events
    # ...and the provider error is not surfaced as a blank/second error frame.
    assert not any(e[0] == "error" for e in events), events


def test_a_200_stream_error_after_text_still_fails_the_arm(tmp_path, monkeypatch):
    """A partial answer followed by an upstream error is a failure, not success."""
    def fake_open(endpoint, body, **kw):
        if kw.get("deploy_id", "").startswith("bad:"):
            return _FakeSession(200, [_sse_text("partial"), _frame(OPENROUTER_ERROR)])
        return _FakeSession(200, [_sse_text("ok")])

    events = _run_multiplex(tmp_path, monkeypatch, fake_open)
    assert any(e[0] == "arm" and e[1] == 0 and e[4] is True for e in events), events
    assert not any(e[0] == "error" for e in events), events


# --------------------------------------------------------------------------- #
# a stream that completes with no content at all
# --------------------------------------------------------------------------- #

def test_sse_has_tool_calls():
    chunk = _frame({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"name": "f", "arguments": "{}"}}]}}]})
    assert proxy._sse_has_tool_calls(chunk) is True
    assert proxy._sse_has_tool_calls(_sse_text("hi")) is False


def test_a_reasoning_only_stream_is_an_empty_answer(tmp_path, monkeypatch):
    """A reasoning model that never emits `content` must not draw a blank pane."""
    empty = [_frame({"choices": [{"delta": {"role": "assistant",
                                                "reasoning": "thinking..."}}]}),
             _frame({"choices": [{"delta": {}, "finish_reason": "length"}]})]

    def fake_open(endpoint, body, **kw):
        if kw.get("deploy_id", "").startswith("bad:"):
            return _FakeSession(200, empty)
        return _FakeSession(200, [_sse_text("ok")])

    events = _run_multiplex(tmp_path, monkeypatch, fake_open)
    assert any(e[0] == "arm" and e[1] == 0 and e[4] is True for e in events), events
    assert not any(e[0] == "error" for e in events), events


def test_a_tool_call_stream_is_not_an_empty_answer(tmp_path, monkeypatch):
    """Empty text with a tool call is a legitimate answer, not a failure."""
    tool = [_frame({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"name": "f", "arguments": "{}"}}]}}]}),
            _frame({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})]

    def fake_open(endpoint, body, **kw):
        if kw.get("deploy_id") == "good:one":
            return _FakeSession(200, tool)
        return _FakeSession(200, [_sse_text("ok")])

    events = _run_multiplex(tmp_path, monkeypatch, fake_open,
                            opts=("good:one", "good:two", "good:three"))
    # Arm 0 is not replaced, and both arms end normally.
    assert not any(e[0] == "arm" and e[1] == 0 and e[4] is True for e in events), events
    assert sum(1 for e in events if e[0] == "end") == 2, events
    assert not any(e[0] == "error" for e in events), events
