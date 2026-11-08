"""An old registry must open, repair itself, and keep routing.

The failure this file pins, seen live on this tree: the proxy booted while
another process held the registry file, the schema initialisation was
interrupted part-way, and the server then answered `/healthz` with
`{"ok": true}` while *every* routed request returned 500 — `no such table:
weights_resolved`. The registry was not "a bit incomplete": it was a server
that looked healthy and could not route.

So two properties are asserted here:

* **repair** — a registry missing objects the router needs is repaired by the
  next `Store` open, without losing the rows it already had;
* **honesty** — `/readyz` says *not ready* while that is true, and `/healthz`
  keeps saying what liveness means.

The migration itself is idempotent by construction (`CREATE ... IF NOT
EXISTS`), so "repair" is not a special code path — it is the same DDL, run
again. What was missing was anything *noticing* that it needed running.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from mininfer.schema import Deployment, Weights
from mininfer.store import Store

#: The objects the router cannot work without (mirrors `store._SCHEMA_OBJECTS`).
CRITICAL = (
    "deployments", "weights", "observations", "quota_buckets", "sessions",
    "weights_resolved", "deployments_priced", "routing_stats",
)


def _seed(path):
    """A registry with one deployment, so 'the rows survived' is checkable."""
    s = Store(path)
    s.upsert_weights(Weights("w", "m"))
    s.upsert_deployment(Deployment("p:m", "w", "p", "m", price_in=0.0, price_out=0.0,
                                   zero_price=True, context_window=8000))
    s.commit()
    s.close()
    return path


def _objects(path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


def _drop(path, names) -> None:
    """Remove objects the way an interrupted migration would."""
    conn = sqlite3.connect(str(path))
    try:
        for name in names:
            conn.execute(f"DROP VIEW IF EXISTS {name}")
            conn.execute(f"DROP TABLE IF EXISTS {name}")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------- repair


def test_a_registry_missing_a_view_is_repaired_on_the_next_open(tmp_path):
    """The interrupted-initialisation case, exactly.

    The view is dropped behind the store's back — which is what an
    interrupted DDL run leaves — and the next `Store` open must put it back
    rather than letting the registry answer every query with
    `no such table`.
    """
    path = _seed(tmp_path / "m.db")
    _drop(path, ["weights_resolved"])
    assert "weights_resolved" not in _objects(path)

    s = Store(path)
    try:
        assert s.schema_complete(), "the store did not repair the missing view"
        # And it is not merely declared present — it must work.
        n = s.conn.execute("SELECT COUNT(*) AS c FROM deployments").fetchone()["c"]
        assert n == 1, "the rows did not survive the repair"
    finally:
        s.close()


def test_repairing_does_not_lose_rows(tmp_path):
    """Repair is a DDL re-run, not a rebuild."""
    path = _seed(tmp_path / "m.db")
    for i in range(3):
        s = Store(path)
        s.upsert_weights(Weights(f"w{i}", "m"))
        s.upsert_deployment(Deployment(f"p{i}:m", f"w{i}", "p", "m",
                                       context_window=8000))
        s.commit()
        s.close()

    _drop(path, ["weights_resolved", "deployments_priced"])
    s = Store(path)
    try:
        assert s.schema_complete()
        n = s.conn.execute("SELECT COUNT(*) AS c FROM deployments").fetchone()["c"]
        assert n == 4, f"expected the 4 deployments to survive, found {n}"
    finally:
        s.close()


def test_a_completely_empty_file_becomes_a_usable_registry(tmp_path):
    """The zero-byte-file case: a file that exists but has no schema in it."""
    path = tmp_path / "empty.db"
    path.write_bytes(b"")
    s = Store(path)
    try:
        assert s.schema_complete()
        assert s.conn.execute("SELECT COUNT(*) AS c FROM deployments").fetchone()["c"] == 0
    finally:
        s.close()


def test_schema_complete_is_false_when_something_is_removed_behind_its_back(tmp_path):
    """The check itself must be able to say "no" — and must notice promptly.

    The object is dropped *after* the store was opened, so the "already
    initialised" flag still claims the registry is fine. That is exactly the
    state a restored or externally-modified registry is in, and it is why the
    flag is not trusted.
    """
    path = _seed(tmp_path / "bare.db")
    s = Store(path)
    try:
        assert s.schema_complete()
        _drop(path, ["routing_stats"])
        assert not s.schema_complete(), "the check did not notice a removed object"
    finally:
        s.close()


# --------------------------------------------------------------- honesty


def test_readyz_reports_not_ready_when_the_registry_cannot_be_repaired(tmp_path, monkeypatch):
    """The incident's most expensive symptom: a green server that cannot route.

    A registry whose schema cannot be completed must make `/readyz` fail, so
    the orchestrator stops sending traffic to it — while `/healthz` keeps
    answering for liveness, because the process is fine and restarting it
    would not fix the registry.
    """
    import mininfer.proxy as proxy
    from mininfer.store import Store

    db = tmp_path / "broken.db"
    _seed(db)
    _drop(db, ["weights_resolved"])   # incomplete, so a repair is attempted

    monkeypatch.setenv("MI_DB", str(db))
    monkeypatch.setattr(proxy, "_db", lambda: str(db))

    def _cannot_repair(self):
        raise RuntimeError("schema is not writable")

    monkeypatch.setattr(Store, "_ensure_schema", _cannot_repair)
    client = TestClient(proxy.app)

    r = client.get("/readyz")
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error"] in ("registry_unreadable", "registry_schema_incomplete"), body

    # Liveness is a different question, and it still answers.
    assert client.get("/healthz").status_code == 200


def test_readyz_reports_not_ready_when_the_schema_is_still_incomplete(tmp_path, monkeypatch):
    """The other branch: the registry opens, but is missing what the router needs."""
    import mininfer.proxy as proxy
    from mininfer.store import Store

    db = tmp_path / "incomplete.db"
    _seed(db)

    monkeypatch.setenv("MI_DB", str(db))
    monkeypatch.setattr(proxy, "_db", lambda: str(db))
    monkeypatch.setattr(Store, "schema_complete", lambda self: False)
    client = TestClient(proxy.app)

    r = client.get("/readyz")
    assert r.status_code == 503, r.text
    assert r.json()["error"] == "registry_schema_incomplete"


def test_a_broken_registry_is_repaired_and_then_routes(tmp_path, monkeypatch):
    """The end-to-end property: the incident, with the fix in place.

    Before, the registry stayed broken for the lifetime of the process and
    every request 500'd while `/healthz` said everything was fine. Now the
    registry is repaired on the next open — so the request succeeds.
    """
    import mininfer.proxy as proxy

    db = tmp_path / "healed.db"
    _seed(db)
    _drop(db, ["weights_resolved"])

    monkeypatch.setenv("MI_DB", str(db))
    monkeypatch.setattr(proxy, "_db", lambda: str(db))
    client = TestClient(proxy.app)

    # The registry can now answer a query that used to fail outright.
    stats = client.get("/v1/stats")
    assert stats.status_code == 200, stats.text
    assert client.get("/readyz").status_code == 200


def test_readyz_reports_ready_for_a_sound_registry(tmp_path, monkeypatch):
    """A registry that has not been ingested is still *ready*."""
    import mininfer.proxy as proxy

    db = tmp_path / "sound.db"
    _seed(db)
    monkeypatch.setenv("MI_DB", str(db))
    monkeypatch.setattr(proxy, "_db", lambda: str(db))
    client = TestClient(proxy.app)

    r = client.get("/readyz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["registry"]["deployments"] == 1


def test_readyz_reports_ready_for_an_empty_registry(tmp_path, monkeypatch):
    """No ingest yet is not a fault: it is a registry with nothing in it."""
    import mininfer.proxy as proxy

    db = tmp_path / "new.db"
    monkeypatch.setenv("MI_DB", str(db))
    monkeypatch.setattr(proxy, "_db", lambda: str(db))
    client = TestClient(proxy.app)

    r = client.get("/readyz")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


@pytest.mark.parametrize("name", CRITICAL)
def test_every_critical_object_is_named_in_the_check(tmp_path, name):
    """A critical object the check forgot is a critical object that can go
    missing without anyone noticing."""
    import mininfer.store as store_module

    assert name in store_module._SCHEMA_OBJECTS
    assert name in CRITICAL
