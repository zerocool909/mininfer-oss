"""A tiny TTL cache that can live in one process or in Redis.

The router derives benchmark norms with a full-table scan and then reuses the
result for every request in a 120 s window. In-process that cache is *per
replica*: with N replicas the whole-table scan runs N times per window, and each
replica's view of the registry ages on its own schedule. This module puts the
value in Redis when `MI_REDIS_URL` is set, so every replica reads one copy.

The shape is deliberately the same as `auth.make_limiter`: a missing `redis`
package or an unreachable server falls back to the in-process cache rather than
refusing to serve. A cold cache costs a scan; a dead proxy costs the product.

Values are JSON, so the cache is inspectable with `redis-cli` and holds nothing
that a restart cannot rebuild. Callers that need rich types (tuples, sets)
convert at their own boundary — see `router.benchmark_norms`.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Protocol


class TTLCache(Protocol):
    """The seam the router depends on. Both implementations satisfy it."""

    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any, ttl: float) -> None: ...
    def clear(self) -> None: ...


class InProcessCache:
    """Per-process TTL cache. The default; correct for a single replica.

    Keys live in a dict guarded by a lock, so the cache is safe to read from the
    request threads. `time.monotonic` is the clock: a wall-clock jump (NTP, a
    laptop lid) must not silently expire a value early or keep it forever.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            hit = self._items.get(key)
            if hit is None:
                return None
            expires, value = hit
            if expires <= now:
                self._items.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class RedisCache:
    """Shared TTL cache.

    Takes a *client*, not a URL, so the policy is testable without a server and
    the `redis` package stays optional. A value that is not valid JSON (an
    inspect-by-hand key, or a truncated write) is treated as a miss rather than
    an error: a cache must never be able to fail a request.
    """

    def __init__(self, client, prefix: str = "mininfer:cache:") -> None:
        self._client = client
        self._prefix = prefix

    def get(self, key: str) -> Any | None:
        try:
            raw = self._client.get(f"{self._prefix}{key}")
        except Exception:
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        try:
            self._client.set(f"{self._prefix}{key}", json.dumps(value),
                             ex=max(1, int(ttl)))
        except Exception:
            # A cache that cannot be written is a slower request, not a failure.
            return

    def clear(self) -> None:
        """Delete this cache's keys. Only used by tests."""
        try:
            for k in self._client.scan_iter(f"{self._prefix}*"):
                self._client.delete(k)
        except Exception:
            pass


def make_cache(url: str | None = None) -> TTLCache:
    """The shared cache if `MI_REDIS_URL` is set, else the in-process one.

    Mirrors `auth.make_limiter`: the `redis` import is lazy, and any failure
    (package absent, malformed URL) degrades to the per-process cache.
    """
    if not url:
        return InProcessCache()
    try:
        import redis  # type: ignore

        return RedisCache(redis.Redis.from_url(url, socket_timeout=2))
    except Exception:
        return InProcessCache()
