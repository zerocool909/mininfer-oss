"""The shared cache, and the one thing the router keeps in it.

Two properties matter:

1. **Shared, not per-process.** The benchmark-norms scan is the whole table. With
   `MI_REDIS_URL` set, N replicas must read one cached band, not scan N times.
   That is proven here with a fake Redis client — no server, no `redis` package.
2. **Degrade, never fail.** A missing package or an unreachable server falls back
   to the per-process cache. A cache that can fail a request is worse than no
   cache; `make_cache` is pinned to the same fallback `auth.make_limiter` uses.
"""
from __future__ import annotations

import sys
import time

import pytest

import mininfer.cache as cache_mod
import mininfer.router as router


class FakeRedis:
    """The three calls RedisCache makes, backed by a dict."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value

    def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in list(self.store) if k.startswith(prefix)]

    def delete(self, key):
        self.store.pop(key, None)


class CountingStore:
    """Just enough `Store` for `benchmark_norms`: a target and the scan."""

    target = "counting.db"

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def all_benchmarks(self):
        self.calls += 1
        return self.rows


@pytest.fixture(autouse=True)
def _no_redis_env(monkeypatch):
    monkeypatch.delenv("MI_REDIS_URL", raising=False)
    monkeypatch.setattr(router, "_NORMS_CACHE", cache_mod.InProcessCache())
    monkeypatch.setattr(router, "_NORMS_CACHE_URL", None)


# --------------------------------------------------------------------------- #
# the cache itself
# --------------------------------------------------------------------------- #

def test_in_process_cache_round_trips():
    c = cache_mod.InProcessCache()
    assert c.get("missing") is None
    c.set("k", {"a": 1}, ttl=60)
    assert c.get("k") == {"a": 1}


def test_in_process_cache_expires():
    c = cache_mod.InProcessCache()
    c.set("k", 1, ttl=0.02)
    time.sleep(0.03)
    assert c.get("k") is None


def test_redis_cache_round_trips_json():
    c = cache_mod.RedisCache(FakeRedis())
    c.set("k", {"aa": [0.1, 0.9]}, ttl=60)
    assert c.get("k") == {"aa": [0.1, 0.9]}


def test_redis_cache_treats_garbage_as_a_miss():
    fake = FakeRedis()
    fake.store["mininfer:cache:k"] = "not json"
    assert cache_mod.RedisCache(fake).get("k") is None


def test_redis_cache_prefixes_its_keys():
    fake = FakeRedis()
    cache_mod.RedisCache(fake).set("k", 1, ttl=60)
    assert "mininfer:cache:k" in fake.store


def test_make_cache_without_a_url_is_in_process():
    assert isinstance(cache_mod.make_cache(None), cache_mod.InProcessCache)
    assert isinstance(cache_mod.make_cache(""), cache_mod.InProcessCache)


def test_make_cache_falls_back_when_redis_is_unavailable(monkeypatch):
    """An import failure must degrade, never raise — same contract as the limiter."""
    import builtins

    real_import = builtins.__import__

    def no_redis(name, *args, **kwargs):
        if name == "redis":
            raise ImportError("redis deliberately absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_redis)
    monkeypatch.delitem(sys.modules, "redis", raising=False)
    assert isinstance(cache_mod.make_cache("redis://127.0.0.1:6379/0"),
                      cache_mod.InProcessCache)


# --------------------------------------------------------------------------- #
# the router's use of it
# --------------------------------------------------------------------------- #

def test_norms_scan_the_registry_once_per_ttl():
    store = CountingStore([{"aa": 10.0}, {"aa": 20.0}])
    first = router.benchmark_norms(store)
    second = router.benchmark_norms(store)
    assert first == {"aa": (10.0, 20.0)}
    assert second == first
    assert store.calls == 1, "a warm window must not rescan the table"


def test_norms_are_shared_across_replicas_through_redis(monkeypatch):
    fake = FakeRedis()
    shared = cache_mod.RedisCache(fake)
    monkeypatch.setenv("MI_REDIS_URL", "redis://cache.internal:6379/0")
    monkeypatch.setattr(cache_mod, "make_cache", lambda url=None: shared)
    monkeypatch.setattr(router, "_NORMS_CACHE_URL", None)

    first = CountingStore([{"aa": 1.0}])
    assert router.benchmark_norms(first)["aa"] == (1.0, 1.0)

    # A second replica resolves the cache, finds the shared copy, and does not
    # touch its own store.
    monkeypatch.setattr(router, "_NORMS_CACHE_URL", None)
    second = CountingStore([{"aa": 99.0}])
    assert router.benchmark_norms(second)["aa"] == (1.0, 1.0)
    assert second.calls == 0


def test_norms_key_is_per_registry(monkeypatch):
    """Two stores (different DB targets) must not read each other's norms."""
    fake = FakeRedis()
    monkeypatch.setattr(router, "_NORMS_CACHE", cache_mod.RedisCache(fake))
    monkeypatch.setattr(router, "_NORMS_CACHE_URL", "")
    a = CountingStore([{"aa": 1.0}])
    a.target = "a.db"
    b = CountingStore([{"bb": 2.0}])
    b.target = "b.db"
    assert router.benchmark_norms(a) == {"aa": (1.0, 1.0)}
    assert router.benchmark_norms(b) == {"bb": (2.0, 2.0)}
