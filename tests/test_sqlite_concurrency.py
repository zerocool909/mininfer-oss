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
