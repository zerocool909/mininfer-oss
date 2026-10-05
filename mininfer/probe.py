"""Warm tier — periodic provider health probes.

The router otherwise learns that a provider is unreachable, or that a key was
revoked, only by failing a real request. This module asks each configured
provider directly on a timer, so the registry's view of "which providers are
callable right now" is warm *before* traffic arrives.

Two shapes:

* `check_provider` — one `GET /models`, classified. That proves reachability
  (DNS/TLS) and that the key is accepted, without inventing a chat model.
* `run_probe` / `probe_store` — walk every configured provider, persist each
  result in `provider_health`, and stamp `probe.last_run`. The background loop
  and the portal's "Run now" both call this.

Probing is opt-in: `probe.enabled` lives in the `settings` table and is toggled
from the portal, so a plain `docker compose up` makes no scheduled upstream calls
it did not ask for. `HEALTH_TTL` is what stops an old failure from excluding a
provider forever once the probe is switched off.
"""
from __future__ import annotations

import datetime as dt
import os
import time

import httpx

from .execute import ENDPOINTS, _network_class, _TLS_HINT
from .fetch import utcnow, _verify as _tls_verify
from .store import Store

#: Keys in the `settings` table. Strings because `settings` is a text bag.
ENABLED_KEY = "probe.enabled"
INTERVAL_KEY = "probe.interval"
LAST_RUN_KEY = "probe.last_run"

DEFAULT_INTERVAL = 300.0        # 5 minutes
MIN_INTERVAL = 30.0
#: A result older than this stops counting against a provider.
HEALTH_TTL = 900.0              # 15 minutes
PROBE_TIMEOUT = 10.0

#: Local runtimes. Probing them on a timer only logs a failure whenever the
#: runtime is not up, which is the operator's choice to make by naming them.
LOCAL_PROVIDERS = frozenset({"ollama", "llamacpp"})


def _env_key(provider: str) -> str | None:
    entry = ENDPOINTS.get(provider)
    if not entry or not entry[1]:
        return None
    return os.environ.get(entry[1]) or None


def configured_providers(user_keys: dict[str, str] | None = None) -> list[str]:
    """Providers with a usable credential right now, local runtimes excluded.

    The background loop passes no `user_keys` (browser keys are per-request and
    invisible to it), so it probes what the *server* is configured with. The
    portal's "Test" button remains the way to check a browser-only key.
    """
    out: list[str] = []
    for name in ENDPOINTS:
        if name in LOCAL_PROVIDERS:
            continue
        if (user_keys or {}).get(name) or _env_key(name):
            out.append(name)
    return sorted(out)


def check_provider(provider_id: str, api_key: str, *,
                   timeout: float = PROBE_TIMEOUT) -> dict:
    """`GET /models` and classify the answer.

    Returns a normalized dict (`provider`, `status`, `detail`, `latency_ms`,
    `n_models`). It never raises for a provider-side or network failure: a probe
    that throws is a probe that cannot report, and the router would keep trusting
    a stale verdict.
    """
    base, _key_env, headers = ENDPOINTS[provider_id]
    url = base.rstrip("/") + "/models"
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, verify=_tls_verify()) as client:
            resp = client.get(url, headers={**headers, "Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        kind = _network_class(exc)
        return {"provider": provider_id, "status": kind,
                "detail": _TLS_HINT if kind == "tls_error" else str(exc),
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "n_models": None}

    latency = round((time.perf_counter() - t0) * 1000, 1)
    if resp.status_code in (401, 403):
        return {"provider": provider_id, "status": "auth_error",
                "detail": f"provider rejected the key (HTTP {resp.status_code})",
                "latency_ms": latency, "n_models": None}
    if resp.status_code >= 400:
        return {"provider": provider_id, "status": f"http_{resp.status_code}",
                "detail": resp.text[:200], "latency_ms": latency, "n_models": None}
    try:
        n = len((resp.json() or {}).get("data") or [])
    except Exception:
        n = 0
    return {"provider": provider_id, "status": "ok", "detail": None,
            "latency_ms": latency, "n_models": n}


def probe_store(store: Store, *, user_keys: dict[str, str] | None = None,
                providers: list[str] | None = None,
                timeout: float = PROBE_TIMEOUT) -> dict:
    """Probe the named (or every configured) provider and persist the verdicts."""
    names = providers if providers is not None else configured_providers(user_keys=user_keys)
    results: list[dict] = []
    for name in names:
        if name not in ENDPOINTS:
            continue
        key = (user_keys or {}).get(name) or _env_key(name)
        if not key:
            continue
        res = check_provider(name, key, timeout=timeout)
        store.set_provider_health(name, res["status"], detail=res.get("detail"),
                                  latency_ms=res.get("latency_ms"),
                                  n_models=res.get("n_models"))
        results.append(res)
    store.set_setting(LAST_RUN_KEY, utcnow())
    store.commit()
    return {"checked": len(results), "results": results,
            "last_run": store.get_setting(LAST_RUN_KEY)}


def run_probe(db_path, *, user_keys: dict[str, str] | None = None,
              providers: list[str] | None = None,
              timeout: float = PROBE_TIMEOUT) -> dict:
    """`probe_store` with its own `Store`, for the background loop / CLI."""
    store = Store(db_path)
    try:
        return probe_store(store, user_keys=user_keys, providers=providers,
                           timeout=timeout)
    finally:
        store.close()


def _as_float(raw: str | None, default: float) -> float:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def config(store: Store) -> dict:
    """The warm tier's current settings, as the portal reads them."""
    raw = (store.get_setting(ENABLED_KEY) or "0").strip().lower()
    return {
        "enabled": raw in ("1", "true", "yes", "on"),
        "interval_seconds": _as_float(store.get_setting(INTERVAL_KEY), DEFAULT_INTERVAL),
        "last_run": store.get_setting(LAST_RUN_KEY),
    }


def is_due(cfg: dict, *, now: str | None = None) -> bool:
    """Whether the loop should probe, given the config it just read.

    `now` is injectable for the test; production passes nothing and the clock is
    UTC. A missing or unparseable `last_run` counts as due, so enabling the probe
    runs it on the next tick instead of waiting a full interval.
    """
    last = cfg.get("last_run")
    if not last:
        return True
    try:
        prev = dt.datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    now_dt = dt.datetime.fromisoformat(now) if now else dt.datetime.now(dt.timezone.utc)
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=dt.timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=dt.timezone.utc)
    return (now_dt - prev).total_seconds() >= float(
        cfg.get("interval_seconds") or DEFAULT_INTERVAL)
