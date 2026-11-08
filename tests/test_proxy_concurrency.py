"""Concurrency at the proxy: many requests, one registry, no casualties.

The most expensive defect found on this tree was invisible to every existing
test, because every existing test is single-threaded: a write transaction was
held open across a slow upstream call, so SQLite's single writer lock was
held for the length of that call and every *concurrent* request's bookkeeping
starved into `database is locked` — a 500 for requests whose answers had
already been produced.

So the properties asserted here are about the *system under load*:

* no request ends in a bare 500;
* every attempt is recorded — a request that loses its evidence is a request
  that taught the router nothing;
* no write transaction is open while an upstream call is in flight;
* a slow arm does not stall the others.

The requests are *direct* (`model: "test:model"`), which is deliberate: the
invariant under test is about the write path around the upstream call, and the
routing path adds eligibility rules that have nothing to do with it. The
routed path is exercised concurrently by `test_proxy.py`'s routing tests and by
the nightly canary.
"""
from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from mininfer import proxy
from mininfer.execute import CallResult
from mininfer.schema import Deployment, Weights
from mininfer.store import Store

N_CONCURRENT = 12
ARM = "test:model"


def _seed(db_path):
    s = Store(db_path)
    s.upsert_weights(Weights("hf:test/m", "test-model", params_b=7.0))
    s.upsert_deployment(Deployment(ARM, "hf:test/m", "test", "model",
                                   price_in=1.0, price_out=1.0))
    s.commit()
    s.close()


def _client(monkeypatch, tmp_path):
    db = tmp_path / "c.db"
    _seed(db)
    monkeypatch.setenv("MI_DB", str(db))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    monkeypatch.setattr(proxy, "_db", lambda: str(db))
    return TestClient(proxy.app), db


def _post(client, text="hi"):
    return client.post("/v1/chat/completions",
                       json={"model": ARM,
                             "messages": [{"role": "user", "content": text}],
                             "max_tokens": 16})


# --------------------------------------------------------------------------- #
# the invariant that matters
# --------------------------------------------------------------------------- #


def test_no_write_transaction_is_open_while_an_upstream_call_is_in_flight(monkeypatch, tmp_path):
    """The invariant, asserted at the proxy level.

    python's sqlite3 opens the write transaction at the first statement and
    holds the file's writer lock until commit. A transaction carried across a
    slow model call starves every concurrent request into
    `database is locked`. So during the call, another connection must be able
    to write without waiting.
    """
    client, db = _client(monkeypatch, tmp_path)
    observed: list[str] = []
    other = Store(db)

    def runner(deploy_id, messages, **kw):
        try:
            other.conn.execute(
                "INSERT INTO observations (deploy_id, task, ok, ts) "
                "VALUES (?, 'general_chat', 1, '2026-01-01T00:00:00+00:00')",
                (deploy_id,))
            other.conn.commit()
            observed.append("free")
        except Exception as exc:  # noqa: BLE001 - the failure is the finding
            observed.append(f"blocked: {type(exc).__name__}")
        return CallResult(deploy_id, text="ok", ok=True, latency_ms=1.0)

    monkeypatch.setattr(proxy, "Runner", lambda **kw: runner)
    r = _post(client)
    assert r.status_code == 200, r.text
    assert observed == ["free"], (
        "a write transaction was held across the upstream call: "
        f"{observed}")
    other.close()


def test_concurrent_requests_all_succeed_and_all_record(monkeypatch, tmp_path):
    """Twelve concurrent requests: no 500s, and every attempt accounted for."""
    client, db = _client(monkeypatch, tmp_path)

    def runner(**kw):
        def run(deploy_id, messages, **kw2):
            return CallResult(deploy_id, text="ok", ok=True, latency_ms=1.0,
                              tokens_in=5, tokens_out=3)
        return run

    monkeypatch.setattr(proxy, "Runner", runner)

    results: list[int] = []
    lock = threading.Lock()

    def one(i: int) -> None:
        r = _post(client, f"q{i}")
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(N_CONCURRENT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a request hung"

    assert results.count(200) == N_CONCURRENT, f"statuses: {sorted(results)}"
    # And the evidence: one observation per request, none lost.
    s = Store(db)
    try:
        n = s.conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]
        assert n >= N_CONCURRENT, f"{n} observations for {N_CONCURRENT} requests"
        d = s.conn.execute("SELECT COUNT(*) AS c FROM decisions").fetchone()["c"]
        assert d >= N_CONCURRENT, f"{d} decisions for {N_CONCURRENT} requests"
    finally:
        s.close()


def test_a_slow_arm_does_not_stall_the_others(monkeypatch, tmp_path):
    """One hung provider must not become everyone's problem.

    The slow arm must not hold the registry's writer lock while it waits — that
    is the same starvation as above, and it is what turns one slow provider
    into a fleet-wide outage.
    """
    client, db = _client(monkeypatch, tmp_path)
    started = threading.Event()
    released = threading.Event()

    def runner(**kw):
        def run(deploy_id, messages, **kw2):
            started.set()
            released.wait(timeout=10)
            return CallResult(deploy_id, text="ok", ok=True, latency_ms=1.0)
        return run

    monkeypatch.setattr(proxy, "Runner", runner)

    slow = threading.Thread(target=lambda: _post(client, "slow"), daemon=True)
    slow.start()
    assert started.wait(timeout=5), "the slow arm never started"

    # While the first request waits on the slow arm, a *writer* must still get
    # through: the slow call must not be holding the write lock.
    writer = Store(db)
    try:
        writer.conn.execute(
            "INSERT INTO observations (deploy_id, task, ok, ts) "
            "VALUES ('other:1', 'general_chat', 1, '2026-01-01T00:00:00+00:00')")
        writer.conn.commit()
    finally:
        writer.close()
        released.set()


def test_concurrent_requests_scale_rather_than_collapse(monkeypatch, tmp_path):
    """The property the fix bought, pinned so it cannot regress.

    Before, SQLite's writer lock was held across an upstream call and every
    write met `SQLITE_BUSY` and slept in half-second steps. Measured on this
    tree: 16 concurrent requests took **23 seconds** (worst case), because
    each one queued behind the previous one's uncommitted transaction. The
    same test after the fix: under half a second.

    The bound is deliberately generous (5 s for 12 requests that finish in
    ~0.25 s) — it is not a benchmark, it is a *collapse* detector. A
    regression to the old behaviour is 25x over the line.
    """
    client, db = _client(monkeypatch, tmp_path)

    def runner(**kw):
        def run(deploy_id, messages, **kw2):
            return CallResult(deploy_id, text="ok", ok=True, latency_ms=1.0,
                              tokens_in=5, tokens_out=3)
        return run

    monkeypatch.setattr(proxy, "Runner", runner)

    n = 12
    t0 = time.time()
    threads = [threading.Thread(target=lambda i=i: _post(client, f"q{i}"))
               for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    elapsed = time.time() - t0

    assert not any(t.is_alive() for t in threads), "a request hung"
    assert elapsed < 5.0, (
        f"{n} concurrent requests took {elapsed:.1f}s — the writer lock is "
        f"being held across an upstream call again (the old behaviour was 23s)")


def test_a_failing_arm_under_load_still_records_its_failure(monkeypatch, tmp_path):
    """Under concurrency, a failure is still *evidence*.

    The failure path is the one that writes; if the write is lost under load,
    the router keeps choosing an arm that is failing.
    """
    client, db = _client(monkeypatch, tmp_path)

    def runner(**kw):
        def run(deploy_id, messages, **kw2):
            return CallResult(deploy_id, error_class="429", latency_ms=1.0)
        return run

    monkeypatch.setattr(proxy, "Runner", runner)

    statuses: list[int] = []
    lock = threading.Lock()

    def one() -> None:
        r = _post(client)
        with lock:
            statuses.append(r.status_code)

    threads = [threading.Thread(target=one) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    # Every request failed at the provider, so the caller gets a coherent
    # error — never a bare 500.
    assert all(code in (429, 502, 503) for code in statuses), statuses

    s = Store(db)
    try:
        n = s.conn.execute(
            "SELECT COUNT(*) AS c FROM observations WHERE ok=0").fetchone()["c"]
        assert n >= 6, f"{n} failure observations for 6 requests — evidence lost"
    finally:
        s.close()
