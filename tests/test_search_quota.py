"""The per-tenant daily search quota — search as a birthright, bounded by a day.

A session cap is a cap you reset by opening a tab. The daily bucket is keyed to
the tenant, so it survives new sessions, and it resets on the UTC day the free
provider's own allowance does.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

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
tasks:
  t:
    description: test
    tokens_in: 10
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
"""


class _Fake:
    """A SearchResult-shaped stand-in, so these tests never touch the network."""

    def __init__(self, cost: float = 0.0, provider: str = "wikipedia"):
        from mininfer.search import SearchResult
        self._r = SearchResult(query="q", provider=provider,
                               results=[{"title": "t", "url": "u", "snippet": "s"}],
                               cost_usd=cost)

    def __getattr__(self, name):
        return getattr(self._r, name)

    def as_dict(self):
        return self._r.as_dict()


def _env(tmp_path, monkeypatch, *, daily: int = 0) -> TestClient:
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.delenv("MI_SESSION_TOKEN_LIMIT", raising=False)
    monkeypatch.delenv("MI_SESSION_COST_LIMIT", raising=False)
    if daily:
        monkeypatch.setenv("MI_SEARCH_DAILY_LIMIT", str(daily))
    else:
        monkeypatch.delenv("MI_SEARCH_DAILY_LIMIT", raising=False)
    from mininfer.proxy import app
    return TestClient(app)


# --- the store bucket --------------------------------------------------------


def test_daily_search_usage_accumulates_per_tenant_and_day(tmp_path):
    s = Store(tmp_path / "q.db")
    assert s.daily_search_usage("acme", "2026-10-09")["searches"] == 0
    s.add_daily_search_usage("acme", "2026-10-09", searches=1, cost_usd=0.0)
    s.add_daily_search_usage("acme", "2026-10-09", searches=1, cost_usd=0.008)
    u = s.daily_search_usage("acme", "2026-10-09")
    assert u["searches"] == 2 and u["cost_usd"] == 0.008
    # A different tenant, and a different day, are their own buckets.
    assert s.daily_search_usage("acme", "2026-10-10")["searches"] == 0
    assert s.daily_search_usage("other", "2026-10-09")["searches"] == 0
    s.close()


# --- the endpoint ------------------------------------------------------------


def test_the_daily_quota_refuses_the_next_search(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, daily=2)
    monkeypatch.setattr("mininfer.search.search", lambda *a, **k: _Fake())

    for i in range(2):
        r = client.post("/v1/search", headers={"X-MI-Session": f"s{i}"},
                        json={"query": "q"})
        assert r.status_code == 200

    refused = client.post("/v1/search", headers={"X-MI-Session": "s3"},
                          json={"query": "q"})
    assert refused.status_code == 429
    assert refused.json()["error"]["type"] == "search_daily_quota_exceeded"
    assert "resets at 00:00 UTC" in refused.json()["error"]["message"]


def test_a_new_session_does_not_reset_the_daily_quota(tmp_path, monkeypatch):
    """The whole point of a tenant bucket: a new tab is not a new allowance."""
    client = _env(tmp_path, monkeypatch, daily=1)
    monkeypatch.setattr("mininfer.search.search", lambda *a, **k: _Fake())
    assert client.post("/v1/search", headers={"X-MI-Session": "first"},
                       json={"query": "q"}).status_code == 200
    assert client.post("/v1/search", headers={"X-MI-Session": "second"},
                       json={"query": "q"}).status_code == 429


def test_the_response_carries_the_remaining_quota(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, daily=3)
    monkeypatch.setattr("mininfer.search.search", lambda *a, **k: _Fake())
    r = client.post("/v1/search", headers={"X-MI-Session": "s"}, json={"query": "q"})
    q = r.json()["search_quota"]
    assert q == {"daily_limit": 3, "used_today": 1, "remaining_today": 2}


def test_no_limit_means_no_refusal(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, daily=0)
    monkeypatch.setattr("mininfer.search.search", lambda *a, **k: _Fake())
    r = client.post("/v1/search", headers={"X-MI-Session": "s"}, json={"query": "q"})
    assert r.status_code == 200
    q = r.json()["search_quota"]
    assert q["daily_limit"] == 0 and q["used_today"] == 1
    assert "remaining_today" not in q
