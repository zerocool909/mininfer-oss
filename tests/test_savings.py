"""Cost vs. savings: what a session spent, and what it did not.

The hard part of this feature is not the query, it is the definition. These tests
pin the definition rather than the arithmetic:

  * savings come only from calls that cost nothing, priced at the cheapest *paid*
    sibling of the same artifact;
  * a paid call saves nothing, however cheap it was;
  * a free arm with no paid sibling contributes nothing and is *counted* as
    unpriced, so the total never reads as complete when it is not.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mininfer.schema import Deployment, Weights
from mininfer.store import Store


def _deploy(s, deploy_id, weights_id, provider, pin, pout, *, free=False):
    s.upsert_deployment(Deployment(
        deploy_id, weights_id, provider, deploy_id.split(":", 1)[1],
        price_in=pin, price_out=pout, zero_price=free, context_window=1000))


def _seed(path) -> Store:
    s = Store(path)
    # hf:x is served free by one provider and paid by two others; hf:y is free
    # with no paid sibling; hf:z is paid only.
    s.upsert_weights(Weights("hf:x", "X", benchmark={"coding": 60.0}))
    s.upsert_weights(Weights("hf:y", "Y", benchmark={"coding": 60.0}))
    s.upsert_weights(Weights("hf:z", "Z", benchmark={"coding": 60.0}))
    _deploy(s, "openrouter:free", "hf:x", "openrouter", 0.0, 0.0, free=True)
    _deploy(s, "openrouter:paid", "hf:x", "openrouter", 2.0, 4.0)
    _deploy(s, "other:cheap", "hf:x", "other", 1.0, 2.0)
    _deploy(s, "openrouter:orphan", "hf:y", "openrouter", 0.0, 0.0, free=True)
    _deploy(s, "openrouter:paidonly", "hf:z", "openrouter", 1.0, 1.0)

    # A free call on hf:x: shadow = min(1*1000+2*1000, 2*1000+4*1000)/1e6 = 0.003
    s.observe("openrouter:free", "t", ok=True, ts="2026-01-01T00:00:00+00:00",
              tokens_in=1000, tokens_out=1000, cost_usd=0.0, session_id="s1")
    # A free call on hf:y: no paid sibling, so no estimate.
    s.observe("openrouter:orphan", "t", ok=True, ts="2026-01-01T00:00:00+00:00",
              tokens_in=1000, tokens_out=1000, cost_usd=0.0, session_id="s2")
    # A paid call: it spent, it did not save.
    s.observe("other:cheap", "t", ok=True, ts="2026-01-01T00:00:00+00:00",
              tokens_in=1000, tokens_out=1000, cost_usd=0.003, session_id="s1")
    # A failure is not a call worth pricing.
    s.observe("openrouter:free", "t", ok=False, ts="2026-01-01T00:00:00+00:00",
              error_class="429", session_id="s1")
    s.commit()
    return s


@pytest.fixture
def store(tmp_path):
    s = _seed(tmp_path / "s.db")
    yield s
    s.close()


def test_savings_price_the_cheapest_paid_sibling(store):
    out = store.spend_savings()
    assert out["calls"] == 3           # the failed call is excluded
    assert out["free_calls"] == 2
    assert out["actual_cost_usd"] == pytest.approx(0.003)
    assert out["saved_usd"] == pytest.approx(0.003)
    assert out["unpriced_free_calls"] == 1


def test_a_paid_call_saves_nothing(store):
    """The cheapest sibling is what you *could* have paid; a paid call already paid."""
    out = store.spend_savings()
    assert out["actual_cost_usd"] > 0
    # Only the one free hf:x call contributes savings; the paid call adds none.
    assert out["saved_usd"] == pytest.approx(0.003)


def test_savings_are_scoped_to_a_session(store):
    out = store.spend_savings(session_id="s1")
    assert out["calls"] == 2 and out["free_calls"] == 1
    assert out["saved_usd"] == pytest.approx(0.003)
    assert out["actual_cost_usd"] == pytest.approx(0.003)
    # s2 is the unpriced free arm: no savings, and it says so rather than 0.0.
    assert store.spend_savings(session_id="s2")["unpriced_free_calls"] == 1


def test_a_window_excludes_older_calls(store):
    store.observe("openrouter:free", "t", ok=True, ts="2020-01-01T00:00:00+00:00",
                  tokens_in=1000, tokens_out=1000, cost_usd=0.0, session_id="old")
    store.commit()
    assert store.spend_savings(days=1)["calls"] == 0
    assert store.spend_savings()["calls"] == 4


def test_calls_without_token_counts_are_not_priced(store):
    store.observe("openrouter:free", "t", ok=True, ts="2026-01-01T00:00:00+00:00",
                  cost_usd=0.0, session_id="s1")
    store.commit()
    assert store.spend_savings(session_id="s1")["calls"] == 2  # unchanged


def test_the_column_is_added_to_an_existing_registry(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` is a no-op, so the column needs a migration."""
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, deploy_id TEXT NOT NULL,
        task TEXT NOT NULL, ts TEXT NOT NULL, ok INTEGER NOT NULL,
        error_class TEXT, latency_ms REAL, tokens_in INTEGER, tokens_out INTEGER,
        cost_usd REAL, signal_kind TEXT, signal_value REAL, meta TEXT DEFAULT '{}')""")
    con.execute("INSERT INTO observations (deploy_id, task, ts, ok) VALUES ('d', 't', '2026-01-01T00:00:00+00:00', 1)")
    con.commit(); con.close()

    store = Store(path)
    # The ALTER must land before the index that depends on it.
    store.observe("d", "t", ok=True, ts="2026-01-01T00:00:00+00:00",
                  tokens_in=1, tokens_out=1, cost_usd=0.0, session_id="s")
    store.commit()
    assert store.conn.execute(
        "SELECT COUNT(*) c FROM observations WHERE session_id='s'").fetchone()["c"] == 1
    store.close()


# --------------------------------------------------------------------------- #
# surfacing
# --------------------------------------------------------------------------- #

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


def _client(tmp_path, monkeypatch) -> TestClient:
    _seed(tmp_path / "p.db").close()
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    from mininfer.proxy import app

    return TestClient(app)


def test_savings_endpoint_serves_the_totals(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/v1/savings").json()
    assert body["saved_usd"] == pytest.approx(0.003)
    assert client.get("/v1/savings?session=s2").json()["unpriced_free_calls"] == 1


def test_the_session_envelope_carries_its_own_savings(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/v1/session", headers={"X-MI-Session": "s1"}).json()
    assert body["savings"]["saved_usd"] == pytest.approx(0.003)
    assert body["savings"]["session_id"] == "s1"


def test_stats_carries_the_registry_totals(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/v1/stats").json()
    assert body["savings"]["saved_usd"] == pytest.approx(0.003)
