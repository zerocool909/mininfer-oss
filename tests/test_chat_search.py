"""Chat web search: decide when a turn needs the live web, then ground the answer.

Search is grounding, not a tool loop — the results are injected as a system
message before routing, so one request is still one model call.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mininfer import chatsearch as cs
from mininfer.execute import CallResult
from mininfer.search import SearchResult
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


# --- the decision ------------------------------------------------------------


def test_time_sensitive_prompts_need_the_web():
    assert cs.needs_search("what is the latest version of Python?")[0] is True
    assert cs.needs_search("who won the match today?")[0] is True
    assert cs.needs_search("what happened in 2026?")[0] is True


def test_explicit_requests_need_the_web():
    need, why = cs.needs_search("search the web for vector databases")
    assert need is True and why.startswith("explicit:")


def test_ordinary_prompts_do_not():
    assert cs.needs_search("write me a haiku about the sea")[0] is False
    assert cs.needs_search("explain recursion")[0] is False


def test_the_context_message_carries_urls_to_cite():
    msg = cs.context_message("q", [{"title": "T", "url": "https://t/", "snippet": "s"}])
    assert msg["role"] == "system"
    assert "https://t/" in msg["content"] and "cite" in msg["content"].lower()


# --- the endpoint ------------------------------------------------------------


def _env(tmp_path, monkeypatch, *, mode: str = "", daily: int = 0):
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    # Import first: `mininfer.proxy` loads the developer's `.env` at import, which
    # would otherwise re-set the very knobs this test is pinning.
    from mininfer.proxy import app
    for var in ("MI_CHAT_SEARCH", "MI_SEARCH_DAILY_LIMIT", "MI_SESSION_TOKEN_LIMIT"):
        monkeypatch.delenv(var, raising=False)
    if mode:
        monkeypatch.setenv("MI_CHAT_SEARCH", mode)
    if daily:
        monkeypatch.setenv("MI_SEARCH_DAILY_LIMIT", str(daily))
    store = Store(tmp_path / "p.db")
    store.conn.execute("INSERT INTO weights (weights_id, display_name) VALUES ('hf:m','m')")
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " price_in, price_out, context_window) VALUES ('openrouter:m','hf:m',"
        "'openrouter','m',1.0,1.0,1000)")
    store.commit()
    store.close()
    return TestClient(app)


def _runner(captured: dict):
    def factory(**kw):
        def run(deploy_id, messages, **kw2):
            captured["messages"] = messages
            return CallResult(deploy_id, text="ok", ok=True, tokens_in=10,
                              tokens_out=5, latency_ms=1.0)
        return run
    return factory


def _fake_search(**kw):
    return SearchResult(query=kw.get("query", "q"), provider="wikipedia",
                        results=[{"title": "T", "url": "https://t/", "snippet": "s"}],
                        cost_usd=0.0)


def _last_reason(db) -> dict:
    s = Store(db)
    row = s.conn.execute("SELECT reason FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    s.close()
    return json.loads(row["reason"]) if row else {}


def test_off_by_default_never_searches(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr("mininfer.search.search",
                        lambda *a, **k: called.append(1) or _fake_search(**k))
    captured: dict = {}
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(captured))
    r = client.post("/v1/chat/completions",
                    json={"model": "t", "messages": [{"role": "user", "content": "latest news"}]})
    assert r.status_code == 200
    assert called == []
    assert all(m["role"] != "system" for m in captured["messages"])


def test_auto_searches_on_a_cue_and_grounds_the_answer(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, mode="auto")
    monkeypatch.setattr("mininfer.search.search", lambda *a, **k: _fake_search(**k))
    captured: dict = {}
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(captured))

    r = client.post("/v1/chat/completions",
                    json={"model": "t",
                          "messages": [{"role": "user", "content": "what is the latest news?"}]})
    assert r.status_code == 200
    # The routed model received the injected context...
    assert any(m["role"] == "system" and "https://t/" in m["content"]
               for m in captured["messages"])
    # ...and the decision records that it did.
    reason = _last_reason(tmp_path / "p.db")
    assert reason["search"]["used"] is True
    assert reason["search"]["sources"][0]["url"] == "https://t/"
    # ...and the streamed facts reach the client as headers, which is how the
    # Details panel shows that the answer was grounded.
    assert "used=1" in r.headers["x-mi-search"]
    assert "provider=wikipedia" in r.headers["x-mi-search"]
    assert "https://t/" in r.headers["x-mi-search-sources"]


def test_search_headers_summarise_the_decision():
    from mininfer.proxy import _search_headers

    assert _search_headers({}) == {}
    used = _search_headers({"search": {
        "used": True, "mode": "auto", "provider": "tinyfish", "why": "time:latest",
        "remaining_today": 7, "sources": [{"title": "T", "url": "https://t/"}]}})
    assert used["X-MI-Search"] == ("used=1; mode=auto; provider=tinyfish; why=time:latest;"
                                   " remaining=7")
    assert "https://t/" in used["X-MI-Search-Sources"]
    skipped = _search_headers({"search": {"used": False, "reason": "daily_quota_exceeded"}})
    assert "used=0" in skipped["X-MI-Search"]
    assert "reason=daily_quota_exceeded" in skipped["X-MI-Search"]


def test_auto_skips_when_no_cue_fires(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, mode="auto")
    called = []
    monkeypatch.setattr("mininfer.search.search",
                        lambda *a, **k: called.append(1) or _fake_search(**k))
    captured: dict = {}
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(captured))
    client.post("/v1/chat/completions",
                json={"model": "t", "messages": [{"role": "user", "content": "write a haiku"}]})
    assert called == []


def test_a_request_can_force_search_on(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch)  # server default off
    monkeypatch.setattr("mininfer.search.search", lambda *a, **k: _fake_search(**k))
    captured: dict = {}
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(captured))
    client.post("/v1/chat/completions",
                json={"model": "t", "search": True,
                      "messages": [{"role": "user", "content": "write a haiku"}]})
    assert any(m["role"] == "system" for m in captured["messages"])


def test_an_exhausted_daily_quota_leaves_the_answer_ungrounded(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, mode="on", daily=1)
    monkeypatch.setattr("mininfer.search.search", lambda *a, **k: _fake_search(**k))
    captured: dict = {}
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(captured))
    body = {"model": "t", "messages": [{"role": "user", "content": "latest news"}]}
    assert client.post("/v1/chat/completions", json=body).status_code == 200   # uses the 1
    captured.clear()
    assert client.post("/v1/chat/completions", json=body).status_code == 200   # over quota
    assert all(m["role"] != "system" for m in captured["messages"])
    reason = _last_reason(tmp_path / "p.db")
    assert reason["search"]["used"] is False
    assert reason["search"]["reason"] == "daily_quota_exceeded"


# --- which provider grounds the chat -----------------------------------------
# `auto` stops at the first non-empty answer, and Wikipedia answers a news query
# with tangential encyclopedia pages. Chat grounding must reach TinyFish first.


def test_chat_search_prefers_tinyfish(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, mode="on")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf")
    seen: dict = {}

    def fake_search(*a, **k):
        seen.update(k)
        return _fake_search(**k)

    monkeypatch.setattr("mininfer.search.search", fake_search)
    captured: dict = {}
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(captured))
    client.post("/v1/chat/completions",
                json={"model": "t", "messages": [{"role": "user", "content": "latest news"}]})
    assert seen.get("provider") == "tinyfish"


def test_chat_search_falls_back_to_auto_without_a_tinyfish_key(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, mode="on")
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    seen: dict = {}

    def fake_search(*a, **k):
        seen.update(k)
        return _fake_search(**k)

    monkeypatch.setattr("mininfer.search.search", fake_search)
    captured: dict = {}
    monkeypatch.setattr("mininfer.proxy.Runner", _runner(captured))
    client.post("/v1/chat/completions",
                json={"model": "t", "messages": [{"role": "user", "content": "latest news"}]})
    assert seen.get("provider") == "auto"     # keyless tiers, rather than no search
