"""The warm tier: periodic provider health probes and the router gate they feed.

The router otherwise learns a provider is down, or a key was revoked, only by
failing a real request. These pin the three properties that make the proactive
version useful: a probe classifies without raising, a verdict is persisted and
expires, and an unhealthy provider drops out of the candidate pool.
"""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mininfer import probe
from mininfer.router import Policy, build_candidates
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store


# --------------------------------------------------------------------- stubs


class _Resp:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _Client:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        return self._resp


def _stub(monkeypatch, resp: _Resp) -> None:
    monkeypatch.setattr(probe.httpx, "Client", lambda **kw: _Client(resp))


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    import mininfer.proxy as proxy
    return TestClient(proxy.app)


# ----------------------------------------------------------------- classifying


def test_check_provider_ok(monkeypatch):
    _stub(monkeypatch, _Resp(200, {"data": [{"id": "a"}, {"id": "b"}]}))
    out = probe.check_provider("groq", "gsk_x")
    assert out["status"] == "ok"
    assert out["n_models"] == 2


def test_check_provider_auth_error(monkeypatch):
    _stub(monkeypatch, _Resp(401, {"error": {"message": "Invalid API Key"}}))
    assert probe.check_provider("groq", "gsk_x")["status"] == "auth_error"


def test_check_provider_http_error(monkeypatch):
    _stub(monkeypatch, _Resp(404, {"error": {"message": "nope"}}))
    assert probe.check_provider("groq", "gsk_x")["status"] == "http_404"


def test_check_provider_network_error_does_not_raise(monkeypatch):
    def boom(**kw):
        class C:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, *a, **k):
                raise httpx.ConnectError("no route to host")
        return C()

    monkeypatch.setattr(probe.httpx, "Client", boom)
    assert probe.check_provider("groq", "gsk_x")["status"] == "network_error"


# ------------------------------------------------------------------ persisting


def test_probe_store_persists_health_and_last_run(tmp_path, monkeypatch):
    _stub(monkeypatch, _Resp(200, {"data": []}))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    s = Store(tmp_path / "p.db")
    summary = probe.probe_store(s, providers=["groq"])
    assert summary["checked"] == 1
    assert s.provider_health()["groq"]["status"] == "ok"
    assert s.get_setting(probe.LAST_RUN_KEY)
    s.close()


def test_probe_store_skips_providers_without_a_key(tmp_path, monkeypatch):
    _stub(monkeypatch, _Resp(200, {"data": []}))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = Store(tmp_path / "p.db")
    assert probe.probe_store(s, providers=["groq"])["checked"] == 0
    assert s.provider_health() == {}
    s.close()


# --------------------------------------------------------- expiry & scheduling


def test_unhealthy_providers_expire_with_the_ttl(tmp_path):
    s = Store(tmp_path / "p.db")
    s.set_provider_health("groq", "auth_error",
                          checked_at="2000-01-01T00:00:00+00:00")
    assert s.unhealthy_providers(ttl_seconds=900) == set()   # stale -> admitted
    s.set_provider_health("groq", "auth_error")              # just checked
    assert s.unhealthy_providers(ttl_seconds=900) == {"groq"}
    s.set_provider_health("groq", "ok")                      # recovered
    assert s.unhealthy_providers(ttl_seconds=900) == set()
    s.close()


def test_is_due_respects_the_interval():
    now = dt.datetime.now(dt.timezone.utc)
    fresh = {"interval_seconds": 300, "last_run": now.isoformat()}
    assert probe.is_due(fresh, now=now.isoformat()) is False
    stale = {"interval_seconds": 300,
             "last_run": (now - dt.timedelta(seconds=400)).isoformat()}
    assert probe.is_due(stale, now=now.isoformat()) is True
    assert probe.is_due({"interval_seconds": 300, "last_run": None}) is True


# ------------------------------------------------------------- the router gate


def _seed_provider(store: Store, provider: str = "groq") -> None:
    store.upsert_weights(Weights(f"hf:{provider}/m", "m", benchmark={"coding": 80.0}))
    store.upsert_deployment(Deployment(
        f"{provider}:m", f"hf:{provider}/m", provider, "m",
        price_in=0.0, price_out=0.0, context_window=1000))
    store.commit()


def test_an_unhealthy_provider_is_not_callable(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    s = Store(tmp_path / "r.db")
    _seed_provider(s)
    s.set_provider_health("groq", "auth_error")
    s.commit()
    task = TaskProfile("t", tokens_in=10, tokens_out=10,
                       benchmark_keys=("coding",), min_success_lb=0.0)
    c = next(c for c in build_candidates(s, task, Policy(require_callable=True))
             if c.deploy_id == "groq:m")
    assert c.callable is False
    assert "key" in (c.rejected or "")
    s.close()


def test_a_healthy_provider_stays_callable(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    s = Store(tmp_path / "r.db")
    _seed_provider(s)
    s.set_provider_health("groq", "ok")
    s.commit()
    task = TaskProfile("t", tokens_in=10, tokens_out=10,
                       benchmark_keys=("coding",), min_success_lb=0.0)
    c = next(c for c in build_candidates(s, task, Policy(require_callable=True))
             if c.deploy_id == "groq:m")
    assert c.callable is True
    assert c.rejected is None
    s.close()


def test_a_stale_failure_admits_the_provider_again(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    s = Store(tmp_path / "r.db")
    _seed_provider(s)
    s.set_provider_health("groq", "auth_error",
                          checked_at="2000-01-01T00:00:00+00:00")
    s.commit()
    task = TaskProfile("t", tokens_in=10, tokens_out=10,
                       benchmark_keys=("coding",), min_success_lb=0.0)
    c = next(c for c in build_candidates(s, task, Policy(require_callable=True))
             if c.deploy_id == "groq:m")
    assert c.callable is True
    s.close()


# ------------------------------------------------------------------- the portal


def test_probe_defaults_to_off(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/v1/probe").json()
    assert body["enabled"] is False
    assert body["interval_seconds"] == probe.DEFAULT_INTERVAL
    assert body["health"] == {}


def test_probe_config_roundtrips(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.post("/v1/probe/config",
                       json={"enabled": True, "interval_seconds": 120}).json()
    assert body["enabled"] is True
    assert body["interval_seconds"] == 120
    # Persisted, so a second read sees it without a restart.
    assert client.get("/v1/probe").json()["enabled"] is True


def test_probe_interval_has_a_floor(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/v1/probe/config",
                       json={"interval_seconds": 1}).status_code == 400


def test_run_now_probes_and_stores(tmp_path, monkeypatch):
    _stub(monkeypatch, _Resp(200, {"data": [{"id": "a"}]}))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    client = _client(tmp_path, monkeypatch)
    assert client.post("/v1/probe/run").json()["checked"] >= 1
    assert client.get("/v1/probe").json()["health"]["groq"]["status"] == "ok"
