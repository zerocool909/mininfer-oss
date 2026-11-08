"""The SQLite write lock must not be held while *waiting* on SQLite itself.

A comparison runs one thread per arm, each with its own connection. The failure
this file pins is a lock-order inversion between the process-wide Python lock and
SQLite's own write lock, and it is silent: the accounting path swallows the
exception, so the observable symptom is a *lost charge*, not an error.
"""
from __future__ import annotations

import threading
import time

from mininfer import db


def _schema(conn) -> None:
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)")
    conn.commit()


def test_two_writers_do_not_deadlock(tmp_path):
    """Writer A holds an open txn; writer B wants the lock; A writes again.

    Before the fix this deadlocked: A held SQLite's write lock (uncommitted) and
    waited for the process lock, while B held the process lock and waited for
    SQLite. Both threads stayed alive past the join timeout.
    """
    path = tmp_path / "c.db"
    a = db.connect(path)
    b = db.connect(path)
    _schema(a)

    # A opens a write transaction and deliberately does not commit: this is the
    # arm that finished streaming and is mid-accounting.
    a.execute("INSERT INTO t (n) VALUES (1)")

    done: dict[str, bool] = {}

    def write(name: str, conn, value: int) -> None:
        conn.execute("INSERT INTO t (n) VALUES (?)", (value,))
        conn.commit()
        done[name] = True

    first = threading.Thread(target=write, args=("b", b, 2), daemon=True)
    first.start()
    time.sleep(0.2)  # let B reach the lock and start waiting
    second = threading.Thread(target=write, args=("a", a, 3), daemon=True)
    second.start()

    first.join(timeout=5.0)
    second.join(timeout=5.0)
    assert not first.is_alive(), "the waiting writer never acquired the lock"
    assert not second.is_alive(), "the lock holder could not make progress"
    assert done == {"a": True, "b": True}

    a.commit()
    rows = a.execute("SELECT n FROM t ORDER BY n").fetchall()
    assert [r["n"] for r in rows] == [1, 2, 3]
    a.close()
    b.close()


def test_every_concurrent_charge_lands(tmp_path):
    """Twenty connections writing at once lose no rows.

    The comparison accounting is exactly this shape — independent connections,
    each a short write transaction — so a row that vanishes here is a charge that
    vanishes in production.
    """
    path = tmp_path / "e.db"
    setup = db.connect(path)
    _schema(setup)
    setup.close()

    n = 20

    def charge(i: int) -> None:
        conn = db.connect(path)
        try:
            conn.execute("INSERT INTO t (n) VALUES (?)", (i,))
            conn.commit()
        finally:
            conn.close()

    threads = [threading.Thread(target=charge, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert not any(t.is_alive() for t in threads)

    check = db.connect(path)
    rows = check.execute("SELECT COUNT(*) AS c FROM t").fetchone()
    check.close()
    assert rows["c"] == n


def test_a_write_transaction_does_not_survive_a_routing_read(tmp_path):
    """The routing path must not hold SQLite's writer lock between requests.

    `reset_expired` runs on the routing read path (it rolls over expired quota
    windows), and python's sqlite3 opens the write transaction at the first
    UPDATE and holds the file's writer lock until commit. Uncommitted, that
    transaction rode into the caller's model call — so a *read* on the routing
    path locked every other request's bookkeeping out for the length of an
    upstream round trip, and those requests died with `database is locked`
    instead of answering.

    So: after the rollover, another connection can write immediately.
    """
    from mininfer.store import Store

    path = tmp_path / "r.db"
    reader = Store(path)
    writer = Store(path)

    # An expired window, so the rollover actually writes.
    writer.conn.execute(
        "INSERT INTO quota_buckets (deploy_id, api_key_alias, \"window\", used_n, "
        "limit_n, reset_at) VALUES ('p:m', 'default', 'minute', 5, 10, "
        "'2000-01-01T00:00:00+00:00')")
    writer.commit()

    reader.reset_expired()

    # No retry, no sleep: the other connection must not have to wait at all.
    writer.conn.execute("INSERT INTO observations (deploy_id, task, ok, ts) "
                        "VALUES ('p:m', 'general_chat', 1, "
                        "'2026-01-01T00:00:00+00:00')")
    writer.commit()
    n = writer.conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]
    assert n == 1

    reader.close()
    writer.close()


def test_a_failed_attempt_is_committed_before_the_next_call(tmp_path):
    """A fallback chain must not carry a write transaction into the next call.

    The failed arm's evidence is written before the next (slow) upstream call.
    Left uncommitted, that write holds the writer lock across the whole
    round trip — the same starvation as the routing rollover, one layer down.
    """
    from mininfer.execute import CallResult
    from mininfer.proxy import try_fallbacks
    from mininfer.store import Store

    path = tmp_path / "f.db"
    store = Store(path)
    other = Store(path)   # a second connection, standing in for a concurrent request

    results = iter([CallResult("a:1", error_class="429"),
                    CallResult("b:2", text="ok", ok=True)])
    lock_held_during_second_call: list[bool] = []

    def runner(deploy_id, messages, **kw):
        if deploy_id == "b:2":
            # During the *second* call, a concurrent writer must not be blocked.
            try:
                other.conn.execute("INSERT INTO observations (deploy_id, task, ok, ts) "
                                   "VALUES ('x:1', 'general_chat', 1, "
                                   "'2026-01-01T00:00:00+00:00')")
                other.conn.commit()
                lock_held_during_second_call.append(False)
            except Exception:
                lock_held_during_second_call.append(True)
                raise
        return next(results)

    selected, _res, _attempts = try_fallbacks(
        ["a:1", "b:2"], [{"role": "user", "content": "hi"}], runner,
        task_name="general_chat", store=store)
    assert selected == "b:2"
    assert lock_held_during_second_call == [False], \
        "the failed attempt's write transaction was still open during the next call"

    store.close()
    other.close()
