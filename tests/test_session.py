"""Server-side session budget.

The cap has to live on the server to mean anything: a client-side counter cannot
refuse a call, and it drifts on refresh, retry and regenerate. These tests pin the
two halves — the ledger accumulates every *billed* attempt, and the ceiling
refuses before spending anything.
"""
from __future__ import annotations

import os

import pytest

from fastapi.testclient import TestClient

from mininfer.execute import CallResult, Endpoint
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
  session_token_limit: LIMIT_HERE
tasks:
  t:
    description: test task
    tokens_in: 10
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
"""


def _env(tmp_path, monkeypatch, *, limit: int = 1_000_000) -> TestClient:
    (tmp_path / "policy.yaml").write_text(_POLICY.replace("LIMIT_HERE", str(limit)))
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("MI_SESSION_TOKEN_LIMIT", raising=False)
    store = Store(tmp_path / "p.db")
    store.conn.execute("INSERT INTO weights (weights_id, display_name) VALUES ('hf:m','m')")
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " price_in, price_out, context_window) VALUES ('openrouter:m','hf:m',"
        "'openrouter','m',1.0,1.0,1000)")
    store.commit()
    store.close()
    return TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)


def _runner(tin=100, tout=50):
    def factory(**kw):
        return lambda deploy_id, messages, **kw2: CallResult(
            deploy_id, text="ok", ok=True, tokens_in=tin, tokens_out=tout,
            latency_ms=5.0)
    return factory


def test_no_session_means_no_accounting(tmp_path, monkeypatch):
    """Callers that do not opt in keep the old stateless behaviour."""
    client = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.proxy.Runner", _runner())
    r = client.post("/v1/chat/completions",
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["mi"]["session"] is None
    store = Store(tmp_path / "p.db")
    assert store.conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0
    store.close()


def test_session_accumulates_tokens_across_turns(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(tin=100, tout=50))
    for _ in range(3):
        r = client.post("/v1/chat/completions",
                        headers={"X-MI-Session": "s-1"},
                        json={"model": "t",
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
    sess = r.json()["mi"]["session"]
    assert (sess["calls"], sess["tokens_in"], sess["tokens_out"], sess["tokens"]) == (3, 300, 150, 450)
    assert sess["limit"] == 1_000_000
    assert sess["remaining"] == 1_000_000 - 450
    # and the endpoint agrees with the envelope the call already returned
    got = client.get("/v1/session", headers={"X-MI-Session": "s-1"}).json()
    assert got["tokens"] == 450 and got["calls"] == 3


def test_a_fallback_chain_charges_every_attempt(tmp_path, monkeypatch):
    """Every attempt was billed, not just the one that answered."""
    client = _env(tmp_path, monkeypatch)
    store = Store(tmp_path / "p.db")
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " price_in, price_out, context_window) VALUES ('openrouter:m2','hf:m',"
        "'openrouter','m2',1.0,1.0,1000)")
    store.commit(); store.close()

    seen: list[str] = []

    def factory(**kw):
        def run(deploy_id, messages, **kw2):
            seen.append(deploy_id)
            if len(seen) == 1:
                return CallResult(deploy_id, error_class="429", latency_ms=3.0,
                                  tokens_in=20, tokens_out=0)
            return CallResult(deploy_id, text="ok", ok=True, tokens_in=100,
                              tokens_out=50, latency_ms=5.0)
        return run

    monkeypatch.setattr("mininfer.proxy.Runner", factory)
    r = client.post("/v1/chat/completions",
                    headers={"X-MI-Session": "s-2"},
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    sess = r.json()["mi"]["session"]
    # the 429 still cost 20 prompt tokens and is charged
    assert sess["calls"] == 2
    assert sess["tokens_in"] == 120


def test_the_cap_reserves_headroom_before_the_call(tmp_path, monkeypatch):
    """A ceiling, not a tripwire: the check reserves the call's worst case.

    Checking only `used >= limit` would let a session overshoot by a whole call
    every time. Here 1000 of a 1500 budget is spent, and the next call reserves
    ~1025 (prompt estimate + max_tokens), so it is refused before it runs.
    """
    client = _env(tmp_path, monkeypatch, limit=1500)
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(tin=1000, tout=0))

    r = client.post("/v1/chat/completions", headers={"X-MI-Session": "s-3"},
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["mi"]["session"]["remaining"] == 500

    r = client.post("/v1/chat/completions", headers={"X-MI-Session": "s-3"},
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "session_budget_exceeded"
    assert "1,000 of its 1,500 token budget" in r.json()["error"]["message"]

    # the refusal itself is free: no extra observation, no extra call
    store = Store(tmp_path / "p.db")
    assert store.conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 1
    assert store.session_usage("s-3")["calls"] == 1
    store.close()


def test_a_sub_max_tokens_limit_still_admits_the_first_call(tmp_path, monkeypatch):
    """A limit smaller than one call's ceiling must not refuse everything.

    Otherwise a mis-set limit looks like a broken proxy rather than a budget.
    """
    client = _env(tmp_path, monkeypatch, limit=200)
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(tin=150, tout=0))
    r = client.post("/v1/chat/completions", headers={"X-MI-Session": "s-6"},
                    json={"model": "t", "max_tokens": 16,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["mi"]["session"]["remaining"] == 50


def test_session_id_comes_from_the_openai_user_field(tmp_path, monkeypatch):
    """`user` is the closest thing the OpenAI schema has to a session."""
    client = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.proxy.Runner", _runner())
    r = client.post("/v1/chat/completions",
                    json={"model": "t", "user": "alice",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["mi"]["session"]["session_id"] == "alice"


def test_env_limit_overrides_the_policy(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, limit=1_000_000)
    monkeypatch.setenv("MI_SESSION_TOKEN_LIMIT", "10")
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(tin=100, tout=0))
    r = client.post("/v1/chat/completions", headers={"X-MI-Session": "s-4"},
                    json={"model": "t", "max_tokens": 1,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["mi"]["session"]["limit"] == 10
    r2 = client.post("/v1/chat/completions", headers={"X-MI-Session": "s-4"},
                     json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    assert r2.status_code == 429


def test_sessions_do_not_leak_into_each_other(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, limit=5_000)
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(tin=100, tout=50))
    a = client.post("/v1/chat/completions", headers={"X-MI-Session": "a"},
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    b = client.post("/v1/chat/completions", headers={"X-MI-Session": "b"},
                    json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    assert a.status_code == 200 and b.status_code == 200
    assert client.get("/v1/session", headers={"X-MI-Session": "b"}).json()["calls"] == 1


def test_session_endpoint_needs_a_session(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)
    r = client.get("/v1/session")
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "no_session"


def test_unreported_usage_still_counts_the_call(tmp_path, monkeypatch):
    """A provider that reports no tokens is still a call against the session."""
    client = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(tin=None, tout=None))
    client.post("/v1/chat/completions", headers={"X-MI-Session": "s-5"},
                json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    sess = client.get("/v1/session", headers={"X-MI-Session": "s-5"}).json()
    assert sess["calls"] == 1 and sess["tokens"] == 0


def test_streamed_call_charges_the_session(tmp_path, monkeypatch):
    """The streamed path is the one the chat uses, so it must be the one that counts."""
    from mininfer.execute import Endpoint

    client = _env(tmp_path, monkeypatch, limit=5_000)
    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))

    class _Sess:
        def __init__(self):
            self.status_code, self.error_class, self.headers = 200, None, {}

        def chunks(self):
            yield (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
            yield (b'data: {"choices":[],"usage":{"prompt_tokens":30,'
                   b'"completion_tokens":20,"cost":0.0005}}\n\n')
            yield b"data: [DONE]\n\n"

        def close(self):
            pass

    monkeypatch.setattr("mininfer.proxy.open_stream",
                        lambda ep, body, *, deploy_id, timeout=120.0: _Sess())
    r = client.post("/v1/chat/completions", headers={"X-MI-Session": "s-7"},
                    json={"model": "t", "stream": True, "max_tokens": 8,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    sess = client.get("/v1/session", headers={"X-MI-Session": "s-7"}).json()
    assert (sess["calls"], sess["tokens_in"], sess["tokens_out"]) == (1, 30, 20)
    assert sess["cost_usd"] == 0.0005


def test_both_arms_of_a_comparison_are_charged(tmp_path, monkeypatch):
    """A comparison is two billed calls, not one."""
    from mininfer.execute import Endpoint

    client = _env(tmp_path, monkeypatch, limit=500_000)
    store = Store(tmp_path / "p.db")
    # Its own weights row: `_distinct_options` collapses two deployments of the
    # same weights into one option, which would make this a single-arm stream.
    store.conn.execute("INSERT INTO weights (weights_id, display_name) VALUES ('hf:m2','m2')")
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " price_in, price_out, context_window) VALUES ('vercel:m2','hf:m2',"
        "'vercel','m2',1.0,1.0,1000)")
    store.conn.execute("UPDATE deployments SET price_in=0.0, price_out=0.0,"
                       " zero_price=1 WHERE deploy_id='openrouter:m'")
    store.commit(); store.close()
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")

    class _Sess:
        def __init__(self):
            self.status_code, self.error_class, self.headers = 200, None, {}

        def chunks(self):
            yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            yield (b'data: {"choices":[],"usage":{"prompt_tokens":7,'
                   b'"completion_tokens":3}}\n\n')
            yield b"data: [DONE]\n\n"

        def close(self):
            pass

    monkeypatch.setattr("mininfer.proxy.resolve_endpoint",
                        lambda did, **kw: Endpoint("http://x/v1", "m", "k", {}))
    monkeypatch.setattr("mininfer.proxy.open_stream",
                        lambda ep, body, *, deploy_id, timeout=120.0: _Sess())
    r = client.post("/v1/chat/completions", headers={"X-MI-Session": "s-8"},
                    json={"model": "t", "stream": True, "mi_options": 2,
                          "max_tokens": 8,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and "x-mi-multiplex" in r.headers
    sess = client.get("/v1/session", headers={"X-MI-Session": "s-8"}).json()
    assert sess["calls"] == 2 and sess["tokens"] == 20


# --------------------------------------------------------------------------- #
# tool spend — the budget has to see the expensive half
# --------------------------------------------------------------------------- #
# A token cap cannot bound a search: Tavily is ~$0.008 while a routed call on a
# free arm is $0. So the session carries two budgets, and search is billed to the
# one that can actually stop it.


def test_search_requires_a_session(tmp_path, monkeypatch):
    """Billed and uncapped is the one combination this endpoint must not allow."""
    client = _env(tmp_path, monkeypatch)
    r = client.post("/v1/search", json={"query": "anything"})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "no_session"


def test_search_is_charged_to_the_session(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.search.search", lambda *a, **k: _FakeResult())
    r = client.post("/v1/search", headers={"X-MI-Session": "s-9"},
                    json={"query": "vector databases", "provider": "tavily"})
    assert r.status_code == 200
    sess = r.json()["session"]
    assert sess["searches"] == 1
    assert sess["search_cost_usd"] == 0.008
    assert sess["cost_usd"] == 0.008
    assert sess["model_cost_usd"] == 0.0
    # and the ledger the endpoint writes is the one /v1/session reads
    got = client.get("/v1/session", headers={"X-MI-Session": "s-9"}).json()
    assert got["searches"] == 1 and got["cost_usd"] == 0.008


def test_a_free_search_costs_nothing_but_still_counts(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("mininfer.search.search",
                        lambda *a, **k: _FakeResult(cost=0.0, provider="wikipedia"))
    client.post("/v1/search", headers={"X-MI-Session": "s-10"},
                json={"query": "q", "provider": "wikipedia"})
    sess = client.get("/v1/session", headers={"X-MI-Session": "s-10"}).json()
    assert sess["searches"] == 1 and sess["cost_usd"] == 0.0


def test_the_cost_budget_bounds_search_spend(tmp_path, monkeypatch):
    """The whole point: a token-only cap cannot see this."""
    client = _env(tmp_path, monkeypatch, limit=1_000_000)   # tokens: ample
    monkeypatch.setenv("MI_SESSION_COST_LIMIT", "0.01")
    monkeypatch.setattr("mininfer.search.search", lambda *a, **k: _FakeResult())

    first = client.post("/v1/search", headers={"X-MI-Session": "s-11"},
                        json={"query": "one", "provider": "tavily"})
    assert first.status_code == 200
    assert first.json()["session"]["cost_remaining"] == 0.002

    second = client.post("/v1/search", headers={"X-MI-Session": "s-11"},
                         json={"query": "two", "provider": "tavily"})
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "session_budget_exceeded"
    assert "$0.0080 of its $0.0100 budget" in second.json()["error"]["message"]

    # the refusal cost nothing
    sess = client.get("/v1/session", headers={"X-MI-Session": "s-11"}).json()
    assert sess["searches"] == 1


def test_the_reservation_is_the_price_that_may_be_paid_not_the_one_that_was(tmp_path, monkeypatch):
    """`auto` can reach the paid tier, so it reserves the paid rate.

    A budget that only counts money already spent cannot stop the spend it exists
    to bound — the free tier answering today says nothing about tomorrow.
    """
    from mininfer.search import search_cost

    assert search_cost("auto") == 0.008
    assert search_cost("tavily") == 0.008
    assert search_cost("wikipedia") == 0.0
    assert search_cost("duckduckgo") == 0.0

    client = _env(tmp_path, monkeypatch)
    monkeypatch.setenv("MI_SESSION_COST_LIMIT", "0.005")   # less than one search
    monkeypatch.setattr("mininfer.search.search",
                        lambda *a, **k: _FakeResult(cost=0.0, provider="wikipedia"))
    r = client.post("/v1/search", headers={"X-MI-Session": "s-12"},
                    json={"query": "q"})   # provider defaults to auto
    assert r.status_code == 429


def test_model_and_search_spend_share_one_budget(tmp_path, monkeypatch):
    """Two counters, one ceiling — otherwise you can spend the budget twice."""
    client = _env(tmp_path, monkeypatch, limit=1_000_000)
    monkeypatch.setenv("MI_SESSION_COST_LIMIT", "0.02")
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(tin=100, tout=0))
    monkeypatch.setattr("mininfer.search.search", lambda *a, **k: _FakeResult())

    store = Store(tmp_path / "p.db")
    store.conn.execute("INSERT INTO deployments (deploy_id, weights_id, provider,"
                       " provider_model_id, price_in, price_out, context_window)"
                       " VALUES ('openrouter:paid','hf:m','openrouter','paid',"
                       "10.0,10.0,1000)")
    store.commit(); store.close()

    # one model call, then one search — both drawn from the same ceiling
    client.post("/v1/chat/completions", headers={"X-MI-Session": "s-13"},
                json={"model": "t", "messages": [{"role": "user", "content": "hi"}]})
    r = client.post("/v1/search", headers={"X-MI-Session": "s-13"},
                    json={"query": "q", "provider": "tavily"})
    assert r.status_code == 200
    sess = r.json()["session"]
    assert sess["calls"] == 1 and sess["searches"] == 1
    # The ledger keeps the two kinds of spend separable: 100 prompt tokens on a
    # $1/Mtok arm is $0.0001, and the search is $0.008.
    assert sess["model_cost_usd"] == 0.0001
    assert sess["search_cost_usd"] == 0.008
    assert round(sess["cost_usd"], 4) == 0.0081


@pytest.mark.skipif(bool(os.environ.get("MI_TEST_PG_DSN")),
                    reason="exercises the SQLite-only column migration")
def test_adding_columns_to_an_existing_registry(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` is a no-op, so a new column needs a migration."""
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    # The shape that shipped before search was billed: same base columns,
    # without `searches` / `search_cost_usd`.
    con.execute("""CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0,
        tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen TEXT DEFAULT CURRENT_TIMESTAMP)""")
    con.execute("INSERT INTO sessions (session_id, calls, cost_usd) VALUES ('old', 3, 0.5)")
    con.commit(); con.close()

    store = Store(path)   # opening it must widen the table, not fail on it
    got = store.session_usage("old")
    assert got["calls"] == 3 and got["cost_usd"] == 0.5
    assert got["searches"] == 0 and got["search_cost_usd"] == 0.0
    store.add_session_usage("old", searches=1, search_cost_usd=0.008, cost_usd=0.008)
    store.commit()
    after = store.session_usage("old")
    assert after["searches"] == 1 and after["cost_usd"] == 0.508
    store.close()


class _FakeResult:
    """A SearchResult-shaped stand-in, so these tests never touch the network."""

    def __init__(self, cost: float = 0.008, provider: str = "tavily"):
        from mininfer.search import SearchResult
        self._r = SearchResult(query="q", provider=provider,
                               results=[{"title": "t", "url": "u", "snippet": "s"}],
                               cost_usd=cost)

    def __getattr__(self, name):
        return getattr(self._r, name)

    def as_dict(self):
        return self._r.as_dict()
