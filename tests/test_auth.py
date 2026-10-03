"""Phase 0 access control: API-key auth, per-key rate limiting, surface split.

The property these tests exist to protect is the *default*: with nothing
configured, MinInfer is the local dev tool it has always been — open, unlimited,
dashboard included. Auth is opt-in, so a regression here would silently either
lock out local development or leave a deployed instance wide open.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from mininfer.execute import CallResult
from mininfer.schema import Deployment, Weights
from mininfer.store import Store

_POLICY = """\
policy:
  name: test
  top_k: 3
  require_distinct_provider: false
  on_unverified_capability: allow
  require: {}
  min_context: 0
  min_success_lb: 0.0
  allow_unknown_price: true
  prior_strength: 25.0
tasks:
  alpha:
    description: alpha task
    tokens_in: 100
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
    benchmark_keys: [coding]
    cues: [alpha]
"""

_ENV_KEYS = ("MI_API_KEYS", "MI_API_KEYS_FILE", "MI_ADMIN_TOKEN",
             "MI_RATE_LIMIT_RPM", "MI_MAX_BODY_BYTES")


def _client(tmp_path, monkeypatch, capture: list | None = None, **env) -> TestClient:
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    store = Store(tmp_path / "p.db")
    store.upsert_weights(Weights("hf:t/m", "test-model", params_b=7.0,
                                 benchmark={"coding": 50.0}))
    store.upsert_deployment(Deployment("openrouter:m", "hf:t/m", "openrouter", "m",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    store.commit()
    store.close()

    import mininfer.auth as auth
    import mininfer.proxy as proxy
    auth.reset_cache()
    proxy._LIMITER.reset()
    # A successful call, so these tests exercise the access-control layer rather
    # than the 502 the real caller would produce without a network.
    def _ok(deploys, messages, runner, **kw):
        if capture is not None:
            capture.append(kw.get("session_id"))
        did = deploys[0] if deploys else ""
        return did, CallResult(did, text="hi", ok=True, tokens_in=11, tokens_out=5), []

    monkeypatch.setattr(proxy, "try_fallbacks", _ok)
    return TestClient(proxy.app)


def _ask(client: TestClient, **kw):
    return client.post("/v1/chat/completions",
                       json={"model": "auto", "messages": [{"role": "user", "content": "just alpha"}]},
                       **kw)


# --------------------------------------------------------------- default is open


def test_auth_is_off_by_default(tmp_path, monkeypatch):
    """No keys configured -> the local dev tool, unchanged."""
    client = _client(tmp_path, monkeypatch)
    assert _ask(client).status_code == 200
    assert client.get("/v1/stats").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/v1/plan?task=alpha").status_code == 200


def test_healthz_is_always_public(tmp_path, monkeypatch):
    """A load balancer has no credential to send."""
    client = _client(tmp_path, monkeypatch, MI_API_KEYS="sk-tenant:acme")
    assert client.get("/healthz").status_code == 200


# ------------------------------------------------------------------ tenant keys


def test_tenant_key_is_required_when_configured(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, MI_API_KEYS="sk-acme:acme")
    assert _ask(client).status_code == 401
    assert _ask(client, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert _ask(client, headers={"Authorization": "Bearer sk-acme"}).status_code == 200
    # X-API-Key is accepted too, for clients that cannot set Authorization.
    assert _ask(client, headers={"X-API-Key": "sk-acme"}).status_code == 200


def test_401_body_is_in_the_openai_error_shape(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, MI_API_KEYS="sk-acme:acme")
    r = _ask(client)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"
    assert r.headers.get("www-authenticate") == "Bearer"


# ----------------------------------------------------------------- admin surface


def test_admin_surface_needs_an_admin_token_not_a_tenant_key(tmp_path, monkeypatch):
    """A tenant key must not read the registry, pricing or other callers' IPs."""
    client = _client(tmp_path, monkeypatch, MI_API_KEYS="sk-acme:acme",
                     MI_ADMIN_TOKEN="admin-secret")
    tenant = {"Authorization": "Bearer sk-acme"}
    admin = {"Authorization": "Bearer admin-secret"}

    for path in ("/v1/stats", "/v1/plan?task=alpha", "/", "/legacy"):
        assert client.get(path, headers=tenant).status_code == 401, path
        assert client.get(path, headers=admin).status_code == 200, path


def test_basic_auth_opens_the_dashboard_in_a_browser(tmp_path, monkeypatch):
    """A browser navigation cannot set a Bearer header, so Basic must work."""
    import base64

    client = _client(tmp_path, monkeypatch, MI_ADMIN_TOKEN="admin-secret")
    token = base64.b64encode(b"me:admin-secret").decode()
    assert client.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200
    # and the challenge is Basic, which is what makes a browser prompt
    r = client.get("/")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


def test_an_admin_credential_also_satisfies_tenant_endpoints(tmp_path, monkeypatch):
    """The console calls tenant endpoints, so refusing admin there locks it out.

    The SPA behind `/` fetches `/v1/session` and posts to `/v1/chat/completions`
    for the playground; both are tenant surfaces.
    """
    client = _client(tmp_path, monkeypatch, MI_ADMIN_TOKEN="admin-secret",
                     MI_API_KEYS="sk-acme:acme")
    admin = {"Authorization": "Bearer admin-secret"}
    assert client.get("/v1/models", headers=admin).status_code == 200
    assert client.get("/v1/session?session=x", headers=admin).status_code in (200, 400)
    assert _ask(client, headers=admin).status_code == 200


def test_admin_traffic_is_not_tenant_rate_limited(tmp_path, monkeypatch):
    """The operator's own console should not consume a tenant's budget."""
    client = _client(tmp_path, monkeypatch, MI_ADMIN_TOKEN="admin-secret",
                     MI_API_KEYS="sk-acme:acme", MI_RATE_LIMIT_RPM="1")
    admin = {"Authorization": "Bearer admin-secret"}
    assert all(_ask(client, headers=admin).status_code == 200 for _ in range(4))


def test_assets_stay_public(tmp_path, monkeypatch):
    """Otherwise the login page cannot load its own bundle."""
    import mininfer.auth as auth

    assert auth.classify("/assets/index-abc.js") == "public"
    assert auth.classify("/healthz") == "public"
    assert auth.classify("/v1/stats") == "admin"
    assert auth.classify("/v1/chat/completions") == "tenant"
    assert auth.classify("/v1/local/probe") == "admin"


# ---------------------------------------------------------------- rate limiting


def test_rate_limit_returns_429_with_retry_after(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, MI_API_KEYS="sk-acme:acme",
                     MI_RATE_LIMIT_RPM="2")
    h = {"Authorization": "Bearer sk-acme"}
    assert _ask(client, headers=h).status_code == 200
    assert _ask(client, headers=h).status_code == 200
    r = _ask(client, headers=h)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(r.headers["retry-after"]) >= 1


def test_rate_limit_is_per_tenant(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, MI_API_KEYS="sk-a:acme,sk-b:beta",
                     MI_RATE_LIMIT_RPM="1")
    a = {"Authorization": "Bearer sk-a"}
    b = {"Authorization": "Bearer sk-b"}
    assert _ask(client, headers=a).status_code == 200
    assert _ask(client, headers=a).status_code == 429
    # beta has its own window
    assert _ask(client, headers=b).status_code == 200


def test_limiting_is_off_unless_configured(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, MI_API_KEYS="sk-acme:acme")
    h = {"Authorization": "Bearer sk-acme"}
    assert all(_ask(client, headers=h).status_code == 200 for _ in range(5))


# ------------------------------------------------------------- hashed key files


def test_hashed_key_file_never_holds_a_usable_credential(tmp_path, monkeypatch):
    digest = hashlib.sha256(b"sk-secret").hexdigest()
    keyfile = tmp_path / "keys.json"
    keyfile.write_text(json.dumps([{"tenant": "acme", "key_sha256": digest, "rpm": 10}]))
    client = _client(tmp_path, monkeypatch, MI_API_KEYS_FILE=str(keyfile))

    assert "sk-secret" not in keyfile.read_text()
    assert _ask(client).status_code == 401
    assert _ask(client, headers={"Authorization": "Bearer sk-secret"}).status_code == 200


# ------------------------------------------------------------ tenant isolation


def test_sessions_are_scoped_to_the_tenant(tmp_path, monkeypatch):
    """A client-supplied session id is a label, not a boundary.

    Asserted on the id the proxy hands the caller, because that is the value the
    session ledger is keyed on.
    """
    seen: list = []
    client = _client(tmp_path, monkeypatch, capture=seen, MI_API_KEYS="sk-a:acme,sk-b:beta")
    shared = {"X-MI-Session": "shared"}
    _ask(client, headers={**shared, "Authorization": "Bearer sk-a"})
    _ask(client, headers={**shared, "Authorization": "Bearer sk-b"})
    assert seen == ["acme/shared", "beta/shared"]


def test_an_unnamed_session_becomes_the_tenant(tmp_path, monkeypatch):
    """So a per-tenant cap applies even when the caller names no session."""
    seen: list = []
    client = _client(tmp_path, monkeypatch, capture=seen, MI_API_KEYS="sk-a:acme")
    _ask(client, headers={"Authorization": "Bearer sk-a"})
    assert seen == ["acme"]


def test_auth_off_leaves_the_session_id_untouched(tmp_path, monkeypatch):
    """The prefix is an auth-era invention; local dev must not grow one."""
    seen: list = []
    client = _client(tmp_path, monkeypatch, capture=seen)
    _ask(client, headers={"X-MI-Session": "local"})
    assert seen == ["local"]


# ------------------------------------------------------------------ body limits


def test_body_cap_rejects_oversized_payloads(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, MI_API_KEYS="sk-acme:acme",
                     MI_MAX_BODY_BYTES="50")
    r = _ask(client, headers={"Authorization": "Bearer sk-acme"})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "request_too_large"


@pytest.mark.parametrize("path,kind", [
    ("/healthz", "public"),
    ("/assets/index-abc.js", "public"),
    ("/", "admin"),
    ("/v1/stats", "admin"),
    ("/v1/chat/completions", "tenant"),
    ("/v1/search", "tenant"),
    ("/v1/approve", "tenant"),
    # Operator surfaces, not public ones.
    ("/docs", "admin"),
    ("/redoc", "admin"),
    ("/openapi.json", "admin"),
    # Fail closed: an unclassified path is guarded until someone says otherwise.
    ("/some/future/route", "admin"),
    ("/v2/brand-new", "admin"),
])
def test_surface_classification(path, kind):
    import mininfer.auth as auth

    assert auth.classify(path) == kind


def test_no_route_is_public_by_accident():
    """The public set is pinned, so a new endpoint cannot be exposed silently.

    `classify` fails closed, which is what makes this assertion meaningful: a
    route added without touching `auth.py` lands in `admin`, and only an
    explicit listing puts it in public. If this set grows, it must be on
    purpose.

    `web/dist` is a gitignored build artifact, and `proxy.py` mounts `/assets`
    only when it exists — the dependency-free server-rendered page is the
    no-build fallback, so a fresh clone (`git clone && pytest`, no npm) has one
    fewer route. The invariant that survives that is: `/healthz` and the favicon
    are always public, and **nothing else** may be. An exact list would fail on a
    clean checkout for a reason unrelated to accidental exposure.
    """
    import mininfer.auth as auth
    from mininfer.proxy import app

    public = sorted({r.path for r in app.routes
                     if getattr(r, "path", None) and auth.classify(r.path) == "public"})
    # A load balancer and a login page have no credential to send.
    assert "/healthz" in public, public
    assert "/favicon.svg" in public, public
    # Everything else that is public must be one of the sanctioned few — adding
    # a public prefix without updating `auth.py` is what this catches.
    assert set(public) <= {"/assets", "/favicon.svg", "/healthz"}, public


# ------------------------------------------------- shared (Redis) rate limiting


class _FakeRedis:
    """Only the three commands the limiter uses, counted like the real thing."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, 60)

    def flushdb(self) -> None:
        self.counts.clear()


def test_the_redis_limiter_implements_the_same_policy():
    """Same window, same answer — the difference is where the counter lives."""
    import mininfer.auth as auth

    fake = _FakeRedis()
    limiter = auth.RedisRateLimiter(fake)
    assert [limiter.allow("acme", 2)[0] for _ in range(2)] == [True, True]
    allowed, retry_after = limiter.allow("acme", 2)
    assert allowed is False and retry_after >= 1


def test_the_redis_limiter_is_per_key():
    import mininfer.auth as auth

    limiter = auth.RedisRateLimiter(_FakeRedis())
    assert limiter.allow("acme", 1)[0] is True
    assert limiter.allow("acme", 1)[0] is False
    assert limiter.allow("beta", 1)[0] is True          # its own bucket


def test_the_redis_limiter_sets_a_ttl_so_buckets_do_not_leak():
    import mininfer.auth as auth

    fake = _FakeRedis()
    limiter = auth.RedisRateLimiter(fake)
    limiter.allow("acme", 5)
    assert list(fake.ttls.values()) == [90]     # first hit arms the expiry


def test_a_redis_url_without_the_package_falls_back_instead_of_failing():
    """A degraded limit beats a dead proxy — but it is a per-process limit."""
    import mininfer.auth as auth

    assert isinstance(auth.make_limiter("redis://127.0.0.1:6379/0"), auth.RateLimiter)
    assert isinstance(auth.make_limiter(None), auth.RateLimiter)
