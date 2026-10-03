"""Phase 1b — per-tenant attribution and reporting.

The decision this encodes: **usage is attributed per tenant, learning is shared.**
The router still ranks on every tenant's observations, because model quality and
price are properties of the deployment, not the caller, and isolating them would
give each new tenant a cold start. What *is* per-tenant is spend, headroom and
transcripts — and those are what these tests keep scoped. The one rule the
endpoint must not get wrong is that a tenant can never read another tenant's
numbers, so that is asserted directly, including the attempt to ask for it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mininfer.execute import CallResult
from mininfer.fetch import utcnow
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

_ENV_KEYS = ("MI_API_KEYS", "MI_API_KEYS_FILE", "MI_ADMIN_TOKEN",
             "MI_RATE_LIMIT_RPM", "MI_MAX_BODY_BYTES")


def _seed(path) -> Store:
    s = Store(path)
    s.upsert_weights(Weights("hf:m", "m", benchmark={"coding": 60.0}))
    s.upsert_deployment(Deployment("openrouter:m", "hf:m", "openrouter", "m",
                                   price_in=1.0, price_out=1.0, context_window=1000))
    s.commit()
    return s


def _client(tmp_path, monkeypatch, **env) -> TestClient:
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _seed(tmp_path / "p.db").close()

    import mininfer.auth as auth
    import mininfer.proxy as proxy
    auth.reset_cache()
    proxy._LIMITER.reset()

    # The *real* `try_fallbacks` runs, so the observation, the ledger and the
    # decision are written by production code with the tenant the request carried.
    def _runner(**kw):
        return lambda deploy_id, messages, **kw2: CallResult(
            deploy_id, text="ok", ok=True, tokens_in=10, tokens_out=5, latency_ms=1.0)

    monkeypatch.setattr(proxy, "Runner", _runner)
    return TestClient(proxy.app)


def _ask(client, headers, text="hi"):
    return client.post("/v1/chat/completions", headers=headers,
                       json={"model": "t", "messages": [{"role": "user", "content": text}]})


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def test_usage_report_scopes_to_one_tenant(tmp_path):
    s = _seed(tmp_path / "s.db")
    s.observe("openrouter:m", "t", True, ts="2026-01-01T00:00:00+00:00",
              tokens_in=10, tokens_out=5, cost_usd=0.001, tenant_id="acme")
    s.observe("openrouter:m", "t", True, ts="2026-01-01T00:00:00+00:00",
              tokens_in=20, tokens_out=5, cost_usd=0.002, tenant_id="beta")
    s.observe("openrouter:m", "t", True, ts="2026-01-01T00:00:00+00:00",
              tokens_in=1, tokens_out=1)  # unattributed
    s.record_decision(task="t", policy="p", mode="auto", chosen="openrouter:m",
                      candidates=[], reason={}, tenant_id="acme")
    s.add_session_usage("acme/s1", tokens_in=10, tokens_out=5, cost_usd=0.001,
                        tenant_id="acme")
    s.add_session_usage("beta/s1", tokens_in=20, tokens_out=5, cost_usd=0.002,
                        tenant_id="beta")
    s.commit()

    acme = s.usage_report(tenant_id="acme")
    assert (acme["calls"], acme["tokens"], acme["cost_usd"]) == (1, 15, 0.001)
    assert (acme["decisions"], acme["sessions"]) == (1, 1)
    assert acme["by_task"][0]["task"] == "t"
    assert acme["by_deploy"][0]["deploy_id"] == "openrouter:m"

    beta = s.usage_report(tenant_id="beta")
    assert (beta["calls"], beta["tokens"]) == (1, 25)
    assert beta["decisions"] == 0  # no decision was recorded for beta

    whole = s.usage_report()
    assert whole["tenant_id"] is None
    assert (whole["calls"], whole["sessions"]) == (3, 2)
    s.close()


def test_savings_are_also_scoped_by_tenant(tmp_path):
    s = _seed(tmp_path / "s.db")
    # A free arm sharing the weights, so there is a paid sibling to price against.
    s.upsert_deployment(Deployment("openrouter:free", "hf:m", "openrouter", "free",
                                   price_in=0.0, price_out=0.0, zero_price=True,
                                   context_window=1000))
    s.commit()
    s.observe("openrouter:free", "t", True, ts="2026-01-01T00:00:00+00:00",
              tokens_in=1000, tokens_out=1000, cost_usd=0.0, tenant_id="acme")
    s.observe("openrouter:free", "t", True, ts="2026-01-01T00:00:00+00:00",
              tokens_in=1000, tokens_out=1000, cost_usd=0.0, tenant_id="beta")
    s.commit()
    assert s.usage_report(tenant_id="acme")["saved_usd"] == pytest.approx(0.002)
    assert s.usage_report(tenant_id="acme")["saved_usd"] == \
        s.usage_report(tenant_id="beta")["saved_usd"]
    s.close()


def test_the_window_uses_each_tables_own_timestamp_shape(tmp_path):
    """Observations are `T…+00:00`; sessions are `YYYY-MM-DD HH:MM:SS`.

    Comparing the two forms lexically is silently wrong, so the window is built
    per table. This would pass a same-instant comparison only if the formats were
    compatible — they are not.
    """
    s = _seed(tmp_path / "s.db")
    s.observe("openrouter:m", "t", True, ts="2020-01-01T00:00:00+00:00",
              tokens_in=99, tenant_id="acme")
    s.observe("openrouter:m", "t", True, ts=utcnow(), tokens_in=1, tenant_id="acme")
    s.add_session_usage("acme/s1", tenant_id="acme")
    s.commit()
    recent = s.usage_report(tenant_id="acme", days=1)
    assert recent["calls"] == 1          # the 2020 observation is excluded
    assert recent["tokens_in"] == 1
    assert recent["sessions"] == 1       # the session row is current
    assert s.usage_report(tenant_id="acme")["calls"] == 2
    s.close()


def test_tenant_columns_are_added_to_an_existing_registry(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` is a no-op, so each column needs a migration."""
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, deploy_id TEXT NOT NULL,
        task TEXT NOT NULL, ts TEXT NOT NULL, ok INTEGER NOT NULL,
        error_class TEXT, latency_ms REAL, tokens_in INTEGER, tokens_out INTEGER,
        cost_usd REAL, signal_kind TEXT, signal_value REAL, meta TEXT DEFAULT '{}')""")
    con.execute("""CREATE TABLE decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, task TEXT NOT NULL,
        policy TEXT, mode TEXT, chosen TEXT, candidates TEXT, reason TEXT)""")
    con.execute("""CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0,
        tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        first_seen TEXT DEFAULT CURRENT_TIMESTAMP, last_seen TEXT DEFAULT CURRENT_TIMESTAMP)""")
    con.commit(); con.close()

    store = Store(path)
    store.observe("d", "t", True, ts="2026-01-01T00:00:00+00:00", tokens_in=1,
                  tokens_out=1, tenant_id="acme")
    store.record_decision(task="t", policy="p", mode="auto", chosen="d",
                          candidates=[], reason={}, tenant_id="acme")
    store.add_session_usage("acme/s", tenant_id="acme")
    store.commit()
    got = store.usage_report(tenant_id="acme")
    assert (got["calls"], got["decisions"], got["sessions"]) == (1, 1, 1)
    store.close()


# --------------------------------------------------------------------------- #
# endpoint
# --------------------------------------------------------------------------- #


def test_a_tenant_reads_only_its_own_usage(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch,
                     MI_API_KEYS="sk-a:acme,sk-b:beta", MI_ADMIN_TOKEN="adm")
    assert _ask(client, {"Authorization": "Bearer sk-a", "X-MI-Session": "s"}).status_code == 200
    assert _ask(client, {"Authorization": "Bearer sk-b", "X-MI-Session": "s"}).status_code == 200

    a = client.get("/v1/usage", headers={"Authorization": "Bearer sk-a"}).json()
    b = client.get("/v1/usage", headers={"Authorization": "Bearer sk-b"}).json()
    assert (a["tenant_id"], a["calls"], a["tokens"]) == ("acme", 1, 15)
    assert (b["tenant_id"], b["calls"], b["tokens"]) == ("beta", 1, 15)

    # Asking for someone else's tenant is ignored, not honoured.
    sneaky = client.get("/v1/usage?tenant=beta",
                        headers={"Authorization": "Bearer sk-a"}).json()
    assert sneaky["tenant_id"] == "acme"


def test_an_admin_can_read_one_tenant_or_all(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch,
                     MI_API_KEYS="sk-a:acme,sk-b:beta", MI_ADMIN_TOKEN="adm")
    _ask(client, {"Authorization": "Bearer sk-a", "X-MI-Session": "s"})
    _ask(client, {"Authorization": "Bearer sk-b", "X-MI-Session": "s"})

    one = client.get("/v1/usage?tenant=beta", headers={"Authorization": "Bearer adm"}).json()
    assert one["tenant_id"] == "beta" and one["calls"] == 1
    whole = client.get("/v1/usage", headers={"Authorization": "Bearer adm"}).json()
    assert whole["tenant_id"] is None and whole["calls"] == 2
    assert len(whole["by_deploy"]) == 1
    assert len(whole["by_task"]) == 1


def test_usage_requires_a_credential_when_auth_is_on(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, MI_API_KEYS="sk-a:acme")
    assert client.get("/v1/usage").status_code == 401


def test_with_auth_off_usage_is_the_whole_registry(tmp_path, monkeypatch):
    """The local tool has no tenants, so nothing is attributed — and that is fine."""
    client = _client(tmp_path, monkeypatch)
    _ask(client, {"X-MI-Session": "s"})
    body = client.get("/v1/usage").json()
    assert body["tenant_id"] is None and body["calls"] == 1
    assert body["sessions"] == 1


def test_a_session_carries_its_tenant(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch,
                     MI_API_KEYS="sk-a:acme", MI_ADMIN_TOKEN="adm")
    _ask(client, {"Authorization": "Bearer sk-a", "X-MI-Session": "chat1"})
    store = Store(tmp_path / "p.db")
    row = store.conn.execute(
        "SELECT tenant_id FROM sessions WHERE session_id='acme/chat1'").fetchone()
    assert row is not None and row["tenant_id"] == "acme"
    # The decision row carries it too, so the log can be read per tenant.
    dec = store.conn.execute(
        "SELECT tenant_id FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    assert dec["tenant_id"] == "acme"
    store.close()
