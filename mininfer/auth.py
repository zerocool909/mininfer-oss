"""Authentication and rate limiting for the proxy.

**Off by default.** MinInfer ships as a local development tool and that has to
keep working: with no `MI_API_KEYS`, `MI_API_KEYS_FILE` or `MI_ADMIN_TOKEN`
configured, every request is allowed exactly as before, nothing is rate limited,
and the dashboard is open. Auth engages only when a deployment states who its
callers are.

Two credentials, two surfaces:

* a **tenant key** may call the model surface — `/v1/models`,
  `/v1/chat/completions`, `/v1/route`, `/v1/search`, `/v1/approve`, `/v1/session`
  — and is rate limited;
* an **admin token** may additionally read the operational surface — `/`,
  `/legacy`, `/v1/stats`, `/v1/plan`, `/v1/providers`, `/v1/local/*` — which
  exposes the registry, provider pricing, routing decisions and caller IPs.

`/healthz` and the compiled SPA assets stay public so a load balancer and a
browser can reach them.

Configuration
-------------
`MI_API_KEYS`
    `key:tenant,key2:tenant2` — the quick form. The key is in the environment,
    which is fine for a container secret and wrong for a checked-in file.
`MI_API_KEYS_FILE`
    JSON list of `{"tenant", "key_sha256" | "key", "rpm"?}`. Prefer this: keys
    are compared as digests, so the file never holds a usable credential.
`MI_ADMIN_TOKEN`
    One token for the operational surface.
`MI_RATE_LIMIT_RPM`
    Default requests-per-minute per tenant (`0`, the default, disables limiting).
`MI_MAX_BODY_BYTES`
    Reject request bodies larger than this (`0` disables the check).

The limiter is **in-process**. That is correct for one replica and wrong for
several — run a single replica behind it, or move the counters to Redis. See
`PRODUCTIZATION.md`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pathlib
import threading
import time
from dataclasses import dataclass, field

from fastapi import Request
from fastapi.responses import JSONResponse

# --------------------------------------------------------------------------- #
# surfaces
# --------------------------------------------------------------------------- #

#: Reachable without any credential: a load balancer must be able to probe, and a
#: browser must be able to fetch the login page's own assets.
PUBLIC_PATHS = frozenset({"/healthz"})
PUBLIC_PREFIXES = ("/assets/", "/favicon", "/vite.svg", "/robots.txt",
                   "/apple-touch-icon")

#: Registry internals, telemetry, the interactive API docs and the dashboards.
#: Admin only.
ADMIN_PATHS = frozenset({
    "/", "/legacy", "/v1/stats", "/v1/plan", "/v1/providers", "/v1/savings",
    "/v1/local/probe", "/v1/local/register",
    # Hibernation review is an operator decision, not a tenant one: approving a
    # paid model changes what every tenant is charged for.
    "/v1/reviews", "/v1/reviews/decide",
    # FastAPI's built-ins. They publish the whole schema and an interactive
    # client, which is an operator surface, not a public one.
    "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect",
})

TENANT_PREFIX = "/v1/"

REALM = "mininfer"


def _is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path == "/assets":
        return True
    return path.startswith(PUBLIC_PREFIXES)


def classify(path: str) -> str:
    """`public` | `admin` | `tenant` for a request path.

    Fails **closed**: an unrecognised path is `admin`. A new endpoint added
    without touching this function is then guarded by default, and the only way
    to expose something is to add it to `PUBLIC_PATHS`/`PUBLIC_PREFIXES` on
    purpose. `tests/test_auth.py` pins the resulting public set, so a route that
    lands there by accident fails the suite rather than the deployment.
    """
    if _is_public(path):
        return "public"
    if path in ADMIN_PATHS:
        return "admin"
    if path.startswith(TENANT_PREFIX):
        return "tenant"
    return "admin"


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Tenant:
    name: str
    digest: str
    rpm: int = 0  # 0 -> fall back to the default


@dataclass(slots=True)
class AuthConfig:
    tenants: dict[str, Tenant] = field(default_factory=dict)  # digest -> Tenant
    admin_digest: str | None = None
    default_rpm: int = 0
    max_body_bytes: int = 0

    @property
    def enabled(self) -> bool:
        return bool(self.tenants or self.admin_digest)

    def authenticate(self, token: str | None) -> Tenant | None:
        """The tenant a token belongs to, or None. Constant-time on the digest."""
        if not token:
            return None
        digest = _digest(token)
        for known, tenant in self.tenants.items():
            if hmac.compare_digest(known, digest):
                return tenant
        return None

    def is_admin(self, token: str | None) -> bool:
        if not token or not self.admin_digest:
            return False
        return hmac.compare_digest(self.admin_digest, _digest(token))


def _get_env_val(env, key: str, fallback_prefix: str = "FB_") -> str:
    mi_key = "MI_" + key[len(fallback_prefix):] if key.startswith(fallback_prefix) else key
    return (env.get(mi_key) or env.get(key) or "").strip()


def _int_env(env, name: str, default: int = 0) -> int:
    raw = _get_env_val(env, name)
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _tenants_from_env(env) -> dict[str, Tenant]:
    tenants: dict[str, Tenant] = {}

    inline = _get_env_val(env, "MI_API_KEYS")
    for item in filter(None, (p.strip() for p in inline.split(","))):
        key, _, name = item.partition(":")
        key, name = key.strip(), (name.strip() or "default")
        if key:
            tenants[_digest(key)] = Tenant(name=name, digest=_digest(key))

    path = _get_env_val(env, "MI_API_KEYS_FILE")
    if path:
        p = pathlib.Path(path)
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raw = []
            rows = raw if isinstance(raw, list) else [
                {"tenant": k, **(v if isinstance(v, dict) else {})}
                for k, v in (raw or {}).items()
            ]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("tenant") or row.get("name") or "default")
                digest = row.get("key_sha256")
                if not digest and row.get("key"):
                    digest = _digest(str(row["key"]))
                if not digest:
                    continue
                try:
                    rpm = max(0, int(row.get("rpm") or 0))
                except (TypeError, ValueError):
                    rpm = 0
                tenants[str(digest).lower()] = Tenant(name=name, digest=str(digest).lower(), rpm=rpm)

    return tenants


_CONFIG_LOCK = threading.Lock()
_CONFIG_CACHE: tuple[tuple, AuthConfig] | None = None


def _signature(env) -> tuple:
    path = _get_env_val(env, "MI_API_KEYS_FILE")
    mtime = 0.0
    if path:
        try:
            mtime = pathlib.Path(path).stat().st_mtime
        except OSError:
            mtime = -1.0
    return (
        _get_env_val(env, "MI_API_KEYS"),
        path,
        mtime,
        _get_env_val(env, "MI_ADMIN_TOKEN"),
        _get_env_val(env, "MI_RATE_LIMIT_RPM"),
        _get_env_val(env, "MI_MAX_BODY_BYTES"),
    )


def load_config(env=None) -> AuthConfig:
    """Parse the environment, cached until something relevant changes."""
    global _CONFIG_CACHE
    env = os.environ if env is None else env
    sig = _signature(env)
    with _CONFIG_LOCK:
        if _CONFIG_CACHE and _CONFIG_CACHE[0] == sig:
            return _CONFIG_CACHE[1]
    admin = _get_env_val(env, "MI_ADMIN_TOKEN")
    cfg = AuthConfig(
        tenants=_tenants_from_env(env),
        admin_digest=_digest(admin) if admin else None,
        default_rpm=_int_env(env, "MI_RATE_LIMIT_RPM", 0),
        max_body_bytes=_int_env(env, "MI_MAX_BODY_BYTES", 0),
    )
    with _CONFIG_LOCK:
        _CONFIG_CACHE = (sig, cfg)
    return cfg


def reset_cache() -> None:
    """Drop the parsed config. Tests call this after changing the environment."""
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        _CONFIG_CACHE = None


# --------------------------------------------------------------------------- #
# request helpers
# --------------------------------------------------------------------------- #


def presented_token(request: Request) -> str | None:
    """The credential on a request.

    Accepts `Authorization: Bearer <key>` (API clients) and
    `Authorization: Basic <base64>` (a browser opening the dashboard, where the
    token is the password and the username is ignored), plus `X-API-Key`.
    """
    header = request.headers.get("authorization") or ""
    scheme, _, rest = header.partition(" ")
    scheme, rest = scheme.lower(), rest.strip()
    if scheme == "bearer" and rest:
        return rest
    if scheme == "basic" and rest:
        try:
            decoded = base64.b64decode(rest, validate=True).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return None
        _, _, password = decoded.partition(":")
        return password.strip() or None
    key = (request.headers.get("x-api-key") or "").strip()
    return key or None


def unauthorized(detail: str, *, admin: bool = False) -> JSONResponse:
    """401 in the OpenAI error shape, with a challenge a browser understands."""
    challenge = f'Basic realm="{REALM}"' if admin else "Bearer"
    return JSONResponse(
        status_code=401,
        content={"error": {"message": detail, "type": "invalid_request_error",
                           "param": None, "code": "invalid_api_key"}},
        headers={"WWW-Authenticate": challenge},
    )


def too_many(retry_after: int, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": {"message": detail, "type": "rate_limit_error",
                           "param": None, "code": "rate_limit_exceeded"}},
        headers={"Retry-After": str(max(1, retry_after))},
    )


# --------------------------------------------------------------------------- #
# rate limiting
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Fixed-window counter per key, in this process.

    Fixed-window rather than sliding so the memory cost is one tuple per live
    tenant instead of a timestamp deque that grows with traffic — the burst at a
    window edge is a trade worth making for a counter that never leaks.

    Correct for exactly one replica. For more, use `RedisRateLimiter`, which
    implements the same policy against shared state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}  # key -> (window_start, count)

    def allow(self, key: str, rpm: int, *, now: float | None = None) -> tuple[bool, int]:
        """`(allowed, retry_after_seconds)`. `rpm <= 0` disables the limit."""
        if rpm <= 0:
            return True, 0
        now = time.monotonic() if now is None else now
        window = int(now // 60)
        with self._lock:
            start, count = self._windows.get(key, (window, 0))
            if start != window:
                start, count = window, 0
            if count >= rpm:
                self._windows[key] = (start, count)
                return False, int(60 - (now - window * 60)) + 1
            self._windows[key] = (start, count + 1)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


class RedisRateLimiter:
    """The same fixed-window policy, counted in Redis so replicas share it.

    Takes a *client*, not a URL: the object needs only `incr`, `expire` and
    `ttl`, which keeps the `redis` package an optional dependency and lets the
    policy be tested without a server.

    Two round trips in the common case (`INCR`, and `EXPIRE` on the first hit of
    a window). That is the price of a limit that survives a restart or a second
    replica; an in-process counter is a limit on nothing once you scale out.
    """

    def __init__(self, client, prefix: str = "mininfer:rl:") -> None:
        self._client = client
        self._prefix = prefix

    def allow(self, key: str, rpm: int, *, now: float | None = None) -> tuple[bool, int]:
        if rpm <= 0:
            return True, 0
        window = int((time.time() if now is None else now) // 60)
        bucket = f"{self._prefix}{key}:{window}"
        count = int(self._client.incr(bucket))
        if count == 1:
            # First hit in this window: give the counter a life so a tenant that
            # stops calling does not leave a key behind forever.
            self._client.expire(bucket, 90)
        if count > rpm:
            try:
                ttl = int(self._client.ttl(bucket))
            except Exception:
                ttl = 60
            return False, max(1, ttl if ttl > 0 else 60)
        return True, 0

    def reset(self) -> None:
        """Flush this limiter's keys. Only used by tests."""
        try:
            self._client.flushdb()
        except Exception:
            pass


def make_limiter(url: str | None = None):
    """The shared limiter if `MI_REDIS_URL` is set, else the in-process one.

    A missing `redis` package or an unreachable server falls back to in-process
    rather than refusing to serve: a degraded limit beats a dead proxy. That
    fallback is per-process, so it silently stops being a global limit — which
    is why a single-replica deployment is still the honest default.
    """
    if not url:
        return RateLimiter()
    try:
        import redis  # type: ignore

        return RedisRateLimiter(redis.Redis.from_url(url, socket_timeout=2))
    except Exception:
        return RateLimiter()
