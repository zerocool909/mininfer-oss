"""Best-of-N with human approval, and the bounded free-arm trial.

Two behaviours that belong together: a free arm is only trusted on trial for a
few calls, and while it is on trial the router can offer a second opinion so a
human can pick — that pick is what ends the trial.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from mininfer.execute import CallResult, Endpoint
from mininfer.router import Policy, build_candidates
from mininfer.schema import Deployment, TaskProfile, Weights
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


def _fake_runner(**kw):
    def run(deploy_id, messages, **kw2):
        return CallResult(deploy_id, text="answer from " + deploy_id, ok=True,
                          tokens_in=3, tokens_out=2)
    return run


def _setup_two(tmp_path, monkeypatch, policy_text: str = _POLICY) -> TestClient:
    (tmp_path / "policy.yaml").write_text(policy_text)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    store = Store(tmp_path / "p.db")
    store.upsert_weights(Weights("hf:a/m1", "m1", params_b=7.0))
    store.upsert_weights(Weights("hf:b/m2", "m2", params_b=7.0))
    store.upsert_deployment(Deployment("openrouter:m1", "hf:a/m1", "openrouter", "m1",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    store.upsert_deployment(Deployment("vercel:m2", "hf:b/m2", "vercel", "m2",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    store.commit()
    store.close()
    return TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)


def test_options_mode_returns_one_choice_per_candidate(tmp_path, monkeypatch):
    client = _setup_two(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")
    monkeypatch.setattr("mininfer.proxy.Runner", _fake_runner)

    r = client.post("/v1/chat/completions",
                    json={"model": "t", "mi_options": 2,
                          "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 200
    body = r.json()
    assert len(body["choices"]) == 2
    assert len(body["mi"]["options"]) == 2
    assert body["mi"]["needs_approval"] is True
    got = {c["message"]["content"] for c in body["choices"]}
    assert got == {"answer from openrouter:m1", "answer from vercel:m2"}


def test_approve_records_an_observation_per_option(tmp_path, monkeypatch):
    client = _setup_two(tmp_path, monkeypatch)

    r = client.post("/v1/approve",
                    json={"task": "t", "chosen": "openrouter:m1",
                          "rejected": ["vercel:m2"]})

    assert r.status_code == 200 and r.json()["ok"] is True
    store = Store(tmp_path / "p.db")
    rows = {row["deploy_id"]: row for row in store.conn.execute(
        "SELECT deploy_id, ok, signal_kind FROM observations")}
    assert rows["openrouter:m1"]["ok"] == 1
    assert rows["vercel:m2"]["ok"] == 0
    assert rows["openrouter:m1"]["signal_kind"] == "subjective"
    store.close()


def test_free_trial_expires_into_the_quality_floor(tmp_path, monkeypatch):
    """A free arm gets a bounded trial; after it, the floor applies again."""
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    store = Store(tmp_path / "p.db")
    store.upsert_weights(Weights("hf:or/free", "free-model", params_b=7.0))
    store.upsert_deployment(Deployment("openrouter:free", "hf:or/free", "openrouter",
                                       "free", price_in=0.0, price_out=0.0,
                                       zero_price=True, context_window=1000))
    for _ in range(4):  # more than free_trial_obs=3, and all failures
        store.observe("openrouter:free", "t", ok=False, ts="2026-01-01T00:00:00+00:00")
    store.commit()

    task = TaskProfile(name="t", tokens_in=10, tokens_out=10, min_success_lb=0.55)
    pol = Policy(min_success_lb=0.55, free_floor_exempt=True, free_trial_obs=3)
    c = build_candidates(store, task, pol)[0]

    assert c.n_obs == 4
    assert c.rejected is not None  # trial over -> held to the floor
    store.close()


def test_options_carry_their_own_latency(tmp_path, monkeypatch):
    """Options run serially, so each one's own call time is what the UI shows.

    Reporting the round's wall clock against both would make the slower arm look
    as fast as the faster one.
    """
    client = _setup_two(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")
    seen: list[float] = []

    def runner(**kw):
        def run(deploy_id, messages, **kw2):
            lat = 100.0 + 10 * len(seen)
            seen.append(lat)
            return CallResult(deploy_id, text="answer from " + deploy_id, ok=True,
                              latency_ms=lat, tokens_in=3, tokens_out=2)
        return run

    monkeypatch.setattr("mininfer.proxy.Runner", runner)
    body = client.post("/v1/chat/completions",
                       json={"model": "t", "mi_options": 2,
                             "messages": [{"role": "user", "content": "hi"}]}).json()
    latencies = [o["latency_ms"] for o in body["mi"]["options"]]
    assert latencies == [100.0, 110.0]
    assert len(set(latencies)) == 2  # not one shared number


def test_response_reports_latency_and_the_approval_flag(tmp_path, monkeypatch):
    """Both are promoted out of `reason` so a client can read them directly."""
    client = _setup_two(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")

    def runner(**kw):
        return lambda deploy_id, messages, **kw2: CallResult(
            deploy_id, text="hi", ok=True, latency_ms=42.5, tokens_in=1, tokens_out=1)

    monkeypatch.setattr("mininfer.proxy.Runner", runner)
    dm = client.post("/v1/chat/completions",
                     json={"model": "t",
                           "messages": [{"role": "user", "content": "hi"}]}).json()["mi"]
    assert dm["latency_ms"] == 42.5
    assert dm["needs_approval"] is False


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


def test_streamed_answer_carries_the_approval_flag(tmp_path, monkeypatch):
    """A streamed response has no body for the decision envelope to ride in.

    Without the header the approval prompt was only reachable on the
    non-streamed path — i.e. never, since the chat always streams.
    """
    client = _setup_two(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")
    # A free arm on trial: cheapest, under-observed, and a second arm exists.
    store = Store(tmp_path / "p.db")
    store.conn.execute("UPDATE deployments SET price_in=0.0, price_out=0.0, zero_price=1"
                       " WHERE deploy_id='openrouter:m1'")
    store.commit()
    store.close()
    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))
    monkeypatch.setattr("mininfer.proxy.open_stream",
                        lambda ep, body, *, deploy_id, timeout=120.0: _FakeSession(
                            200, [b'data: {"a":1}\n\n', b'data: [DONE]\n\n']))

    r = client.post("/v1/chat/completions",
                    json={"model": "t", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-mi-needs-approval"] == "true"
    assert r.headers["x-mi-deploy"] == "openrouter:m1"


def test_streamed_answer_flag_is_false_for_a_proven_arm(tmp_path, monkeypatch):
    """The flag must be present and false — not absent — so a client can trust it."""
    client = _setup_two(tmp_path, monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")
    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))
    monkeypatch.setattr("mininfer.proxy.open_stream",
                        lambda ep, body, *, deploy_id, timeout=120.0: _FakeSession(
                            200, [b'data: [DONE]\n\n']))

    r = client.post("/v1/chat/completions",
                    json={"model": "t", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["x-mi-needs-approval"] == "false"


class _C:
    def __init__(self, did, weights, upstream, vendor=None):
        self.deploy_id, self.weights_id, self.upstream = did, weights, upstream
        # Defaults to the upstream, so the tests that predate the vendor axis keep
        # describing a pool whose makers are as separable as its upstreams.
        self.vendor = vendor if vendor is not None else upstream


def test_distinct_options_prefers_a_different_vendor():
    """Two variants from one maker is not a choice, even across two gateways.

    Every OpenRouter row shares upstream 'openrouter', so upstream separation
    cannot see this: only the vendor axis can.
    """
    from mininfer.proxy import _distinct_options

    ranked = [_C("openrouter:z1", "hf:glm-prime", "openrouter", vendor="z-ai"),
              _C("openrouter:z2", "hf:glm-air", "openrouter", vendor="z-ai"),
              _C("vercel:q1", "hf:qwen", "vercel", vendor="qwen")]
    ids = [c.deploy_id for c in ranked]
    assert _distinct_options(ranked, ids, 2) == ["openrouter:z1", "vercel:q1"]


def test_distinct_options_allows_the_same_vendor_when_no_other_exists():
    """A thin pool beats a single option — the relaxation is deliberate."""
    from mininfer.proxy import _distinct_options

    ranked = [_C("openrouter:z1", "hf:glm-prime", "openrouter", vendor="z-ai"),
              _C("openrouter:z2", "hf:glm-air", "openrouter", vendor="z-ai")]
    ids = [c.deploy_id for c in ranked]
    assert _distinct_options(ranked, ids, 2) == ["openrouter:z1", "openrouter:z2"]


def test_distinct_options_skips_the_same_model_twice():
    """Same weights behind two gateways is one answer, not two."""
    from mininfer.proxy import _distinct_options

    ranked = [_C("openrouter:x", "hf:x", "openrouter"),
              _C("openrouter/y:x", "hf:x", "y"),
              _C("vercel:z", "hf:z", "vercel")]
    assert _distinct_options(ranked, ["openrouter:x", "openrouter/y:x", "vercel:z"], 2) \
        == ["openrouter:x", "vercel:z"]


def test_distinct_options_relaxes_upstream_when_pool_is_thin():
    from mininfer.proxy import _distinct_options

    ranked = [_C("openrouter:a", "hf:a", "openrouter"),
              _C("openrouter:b", "hf:b", "openrouter")]
    assert _distinct_options(ranked, ["openrouter:a", "openrouter:b"], 2) \
        == ["openrouter:a", "openrouter:b"]


def test_options_come_from_the_full_ranking_not_just_top_k(tmp_path, monkeypatch):
    """Regression: the options pool must be the whole ranking.

    With top_k=1 the diversified `chosen` holds one candidate, so a pool built
    from it can never yield a second option — the comparison silently degrades to
    a single answer.
    """
    client = _setup_two(tmp_path, monkeypatch, _POLICY.replace("top_k: 3", "top_k: 1"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")
    monkeypatch.setattr("mininfer.proxy.Runner", _fake_runner)

    r = client.post("/v1/chat/completions",
                    json={"model": "t", "mi_options": 2,
                          "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 200
    assert len(r.json()["choices"]) == 2


# --------------------------------------------------------------------------- #
# multiplexed compare
# --------------------------------------------------------------------------- #
# Two independent arms over one SSE connection. Serial execution made a
# comparison cost the sum of both calls; the point of the multiplex is that the
# caller waits on the slower arm, not on both.


class _SlowSession:
    """A stream whose frames arrive with a real gap, tagged for one arm."""

    def __init__(self, text: str, delay: float = 0.0):
        self.status_code = 200
        self.error_class = None
        self.headers: dict = {}
        self._text = text
        self._delay = delay
        self.closed = False

    def chunks(self):
        import time as _t
        for piece in self._text.split("|"):
            if self._delay:
                _t.sleep(self._delay)
            yield (b'data: {"choices":[{"delta":{"content":"' + piece.encode() +
                   b'"}}]}\n\n')
        yield (b'data: {"choices":[],"usage":{"prompt_tokens":4,'
               b'"completion_tokens":2,"cost":0.001}}\n\n')
        yield b"data: [DONE]\n\n"

    def close(self):
        self.closed = True


def _mul_setup(tmp_path, monkeypatch, *, delay=0.0):
    client = _setup_two(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")
    started: list[float] = []

    def fake_open(ep, body, *, deploy_id, timeout=120.0):
        import time as _t
        started.append(_t.perf_counter())
        return _SlowSession(f"answer from {deploy_id}".replace(":", "-"),
                            delay=delay)

    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))
    monkeypatch.setattr("mininfer.proxy.open_stream", fake_open)
    return client, started


def _frames(body: str) -> list[dict]:
    import json
    out = []
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[6:]))
    return out


def test_multiplex_streams_both_arms_over_one_connection(tmp_path, monkeypatch):
    client, _ = _mul_setup(tmp_path, monkeypatch)
    r = client.post("/v1/chat/completions",
                    json={"model": "t", "stream": True, "mi_options": 2,
                          "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-mi-multiplex"] == "true"
    assert r.headers["x-mi-arms"] == "2"
    assert r.text.rstrip().endswith("data: [DONE]")

    frames = _frames(r.text)
    arm_events = [f for f in frames if f["mi"]["event"] == "arm"]
    assert [a["mi"]["arm"] for a in arm_events] == [0, 1]
    # the arm announcement is what lets the UI name both models up front
    assert {a["mi"]["deploy_id"] for a in arm_events} == {"openrouter:m1", "vercel:m2"}
    assert len({a["mi"]["vendor"] for a in arm_events}) == 2

    deltas = [f for f in frames if f["mi"]["event"] == "delta"]
    assert {d["mi"]["arm"] for d in deltas} == {0, 1}
    # the OpenAI-shaped half of the frame still addresses the right choice
    for d in deltas:
        assert d["choices"][0]["index"] == d["mi"]["arm"]

    ends = [f for f in frames if f["mi"]["event"] == "end"]
    assert {e["mi"]["arm"] for e in ends} == {0, 1}
    for e in ends:
        assert e["mi"]["latency_ms"] > 0
        assert e["mi"]["usage"]["prompt_tokens"] == 4


def test_multiplex_runs_the_arms_concurrently(tmp_path, monkeypatch):
    """Serial execution was the bug, so this asserts overlap rather than speed.

    A timing threshold would pass even if the arms ran one after the other. A
    two-party barrier cannot: if the arms are not in flight at the same time, the
    first one blocks until the barrier times out and the test fails.
    """
    import threading

    client = _setup_two(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")
    gate = threading.Barrier(2, timeout=5.0)

    class _Gated:
        def __init__(self):
            self.status_code, self.error_class, self.headers = 200, None, {}

        def chunks(self):
            gate.wait()  # both arms must be in flight before either answers
            yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        def close(self):
            pass

    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))
    monkeypatch.setattr("mininfer.proxy.open_stream",
                        lambda ep, body, *, deploy_id, timeout=120.0: _Gated())
    r = client.post("/v1/chat/completions",
                    json={"model": "t", "stream": True, "mi_options": 2,
                          "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 200
    frames = _frames(r.text)
    # Both arms completed. Had they been serial the barrier would have raised
    # BrokenBarrierError, which surfaces as an `error` frame for each arm.
    assert {f["mi"]["arm"] for f in frames if f["mi"]["event"] == "error"} == set()
    assert {f["mi"]["arm"] for f in frames if f["mi"]["event"] == "end"} == {0, 1}
    assert sum(1 for f in frames if f["mi"]["event"] == "delta") == 2


def test_multiplex_records_one_observation_per_arm(tmp_path, monkeypatch):
    client, _ = _mul_setup(tmp_path, monkeypatch)
    client.post("/v1/chat/completions",
                json={"model": "t", "stream": True, "mi_options": 2,
                      "messages": [{"role": "user", "content": "hi"}]})
    store = Store(tmp_path / "p.db")
    rows = [(r["deploy_id"], r["ok"], r["tokens_in"], r["cost_usd"], r["signal_kind"])
            for r in store.conn.execute(
                "SELECT deploy_id, ok, tokens_in, cost_usd, signal_kind FROM observations"
                " ORDER BY deploy_id")]
    assert [(d, ok, tin, cost) for d, ok, tin, cost, _ in rows] == [
        ("openrouter:m1", 1, 4, 0.001), ("vercel:m2", 1, 4, 0.001)]
    assert {k for *_x, k in rows} == {"provider_reported"}
    store.close()


def test_multiplex_reports_one_failed_arm_without_losing_the_other(tmp_path, monkeypatch):
    """An arm that cannot be called must not take the healthy answer with it."""
    import json

    client = _setup_two(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")

    def fake_open(ep, body, *, deploy_id, timeout=120.0):
        if deploy_id == "openrouter:m1":
            raise RuntimeError("boom")
        return _SlowSession("only vercel answered")

    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))
    monkeypatch.setattr("mininfer.proxy.open_stream", fake_open)
    r = client.post("/v1/chat/completions",
                    json={"model": "t", "stream": True, "mi_options": 2,
                          "messages": [{"role": "user", "content": "hi"}]})

    frames = _frames(r.text)
    kinds = {(f["mi"]["event"], f["mi"].get("arm")) for f in frames}
    assert ("error", 0) in kinds
    assert ("end", 1) in kinds
    text = "".join(f["choices"][0]["delta"]["content"]
                   for f in frames if f["mi"]["event"] == "delta")
    assert text == "only vercel answered"
    # and the failure is recorded, so the arm is demoted next time
    store = Store(tmp_path / "p.db")
    bad = store.conn.execute("SELECT ok FROM observations WHERE deploy_id=?",
                             ("openrouter:m1",)).fetchone()
    assert bad["ok"] == 0
    store.close()


def test_no_multiplex_without_mi_options(tmp_path, monkeypatch):
    """A plain streaming request keeps the single-arm shape."""
    client, _ = _mul_setup(tmp_path, monkeypatch)
    r = client.post("/v1/chat/completions",
                    json={"model": "t", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "x-mi-multiplex" not in r.headers
    assert "x-mi-deploy" in r.headers
