"""The dialect seam: one connection surface, two backends.

`Store` owns the schema and `Store` owns the SQL — `tests/test_encapsulation.py`
enforces that no other module writes raw SQL. That is what keeps the distance
between "runs on SQLite" and "runs on Postgres" exactly one file wide, and this
is that file.

Both backends are fed the *same source text*. Statements stay written in
SQLite's dialect and are translated for psycopg2, so there is one readable copy
of each query instead of two that drift. The translation is a pure function and
is unit-tested without a server:

    ?        ->  %s          outside string literals and comments
    :name    ->  %(name)s    ditto
    %        ->  %%          psycopg2 treats `%` as its own format character, so
                             even a LIKE wildcard has to be escaped — inside a
                             string literal as much as outside it, because
                             psycopg2 does not parse the SQL before formatting

The statements that could not be translated mechanically were rewritten in the
portable form instead: `INSERT OR IGNORE` became `ON CONFLICT DO NOTHING`, and
`INSERT OR REPLACE` became `ON CONFLICT (pk) DO UPDATE`. Both engines have
supported that since 2018 (SQLite 3.24, Postgres 9.5), and translating
`OR REPLACE` would have meant parsing the column list at runtime to build the
SET clause — a bug waiting to happen in the one file that must not have one.

Choosing a backend is by the target string, so nothing else in the codebase has
to know: a `postgresql://` URL is Postgres, anything else is a SQLite file.
"""
from __future__ import annotations

import pathlib
import re
import sqlite3
import threading
import time

# Matched with or without the `//`: `pathlib.Path("postgresql://h/db")` collapses
# the slashes to `postgresql:/h/db`, and a mangled DSN that fell through to the
# SQLite branch would silently create a file named after the URL — the wrong
# engine, no error. Failing loudly is the only safe reading.
_POSTGRES_RE = re.compile(r"^postgres(?:ql)?:(?:/{2})?", re.IGNORECASE)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

# SQLite spells the two-argument *scalar* max/min `MAX(a, b)`; Postgres spells
# them `GREATEST`/`LEAST` and has no scalar overload of `MAX`. The single-argument
# aggregate form is identical in both, so only the comma-bearing call is rewritten.
# Args are matched without nesting, which is all this codebase uses; a nested call
# would simply not match and would fail loudly at the server rather than silently
# meaning something else.
_SCALAR_MAX = re.compile(r"\b(MAX|MIN)\(\s*([^(),]+?)\s*,\s*([^(),]+?)\s*\)")


def _scalar_max(sql: str) -> str:
    return _SCALAR_MAX.sub(
        lambda m: f"{'GREATEST' if m.group(1) == 'MAX' else 'LEAST'}("
                  f"{m.group(2)}, {m.group(3)})",
        sql)


def is_postgres(target: object) -> bool:
    """A `postgres://` URL selects Postgres; anything else is a SQLite file."""
    return bool(_POSTGRES_RE.match(str(target).strip()))


def target_from_env(value: str):
    """A `postgresql://` DSN stays a *string*; anything else is a filesystem Path.

    `pathlib.Path("postgresql://host/db")` collapses the slashes to
    `postgresql:/host/db`, and psycopg2 rejects that as a DSN — so a `MI_DB` DSN
    put through `Path` can never connect. `is_postgres` matches either spelling,
    which turns the old silent fallback-to-SQLite into a loud failure; this makes
    it simply *work*. Only file paths are Paths.
    """
    return value if is_postgres(value) else pathlib.Path(value)


def translate(sql: str) -> str:
    """SQLite-dialect SQL -> psycopg2-compatible SQL.

    One pass for the placeholder rules, because they interact: `%(name)s`
    contains a `%` that must not then be escaped, and a `?` inside a string
    literal is text, not a placeholder. So each character is classified exactly
    once. The scalar `MAX(a, b)` rewrite runs first and is purely lexical.
    """
    sql = _scalar_max(sql)
    out: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote is not None:
            if ch == quote:
                quote = None
                out.append(ch)
            elif ch == "%":
                out.append("%%")
            else:
                out.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(sql[i:end])
            i = end
            continue
        elif ch == "?":
            out.append("%s")
        elif ch == ":" and i + 1 < n and _IDENT.match(sql, i + 1):
            m = _IDENT.match(sql, i + 1)
            assert m is not None
            out.append(f"%({m.group(0)})s")
            i = m.end()
            continue
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# SQLite allows one writer at a time. The proxy runs one *thread* per comparison
# arm, each with its own connection, so two arms can be mid-write at once, and
# the process serialises them here rather than letting each collect SQLITE_BUSY.
#
# Postgres needs none of this and does not get it: `PostgresConnection` has no
# lock, so replicas scale independently.
_SQLITE_WRITE_LOCK = threading.RLock()

#: Database files already switched to WAL. The mode is a property of the file,
#: so the switch happens once — and asking SQLite about it on every connection
#: (every request opens one) was the lock convoy on the hot path.
_WAL_FILES: set[str] = set()

#: How long a connection waits for the process write lock before giving up and
#: falling back to statement-level retries. Bounded on purpose: the lock is
#: held for a transaction, so a caller that never commits must not be able to
#: block every writer in the process indefinitely.
_TXN_LOCK_TIMEOUT = 30.0


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _with_retry(fn, attempts: int = 6):
    """Run a *read* statement, backing off while SQLite reports the file locked.

    Retrying is safe because a single statement is atomic: the failure means
    nothing was applied. Reads take no process lock (WAL lets them run beside a
    writer), so they back off in place.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.03 * (attempt + 1))


def _retry_locked(fn, attempts: int = 60):
    """Run a *write* statement under the process lock, waiting **outside** it.

    Releasing the lock before sleeping is the whole point, and it fixes a real
    deadlock rather than a slow path. Two comparison arms each write on their own
    connection, so without it this interleaving is reachable:

        arm A   holds SQLite's write lock (an uncommitted INSERT)
                and waits for the process lock
        arm B   holds the process lock
                and waits for SQLite's write lock

    Neither can move. After ~30 s of retries arm A raised, and the accounting
    path's `except Exception: pass` swallowed it — a *lost charge*, which is
    silent billing under-count, not a visible error. Waiting outside the lock lets
    whichever arm owns SQLite commit and free it.

    `busy_timeout` is small and this loop does the waiting, because a long
    busy-timeout would block *inside* the SQLite call while still holding the
    process lock — the same inversion, just less obviously. Retrying is safe: a
    single statement is atomic, so a busy failure applied nothing.
    """
    for attempt in range(attempts):
        with _SQLITE_WRITE_LOCK:
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or attempt == attempts - 1:
                    raise
        # Outside the lock: the thread that owns SQLite must be able to commit.
        time.sleep(min(0.02 * (attempt + 1), 0.5))
_WRITE_STMT = re.compile(
    r"^\s*(insert|update|delete|replace|create|alter|drop|vacuum|begin|commit|rollback)",
    re.IGNORECASE)


def _retry_write(fn, attempts: int = 20):
    """Run a *write* statement, retrying while SQLite reports the file locked.

    No process lock, and the waiting happens inside SQLite (the busy timeout)
    rather than in a Python sleep loop. Two reasons, both measured:

    * an app-level lock around the statement holds that lock *while SQLite
      waits* — the inversion that let a connection holding SQLite's write lock
      wait for the process lock while another thread held the process lock and
      waited for SQLite's;
    * sleeping in Python between attempts adds the sleep to every retry, so a
      contended write took hundreds of milliseconds of pure sleeping.

    SQLite arbitrates between connections correctly; the busy timeout is the
    efficient way to wait for it. A single statement is atomic, so a busy
    failure applied nothing and the retry is safe.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or attempt == attempts - 1:
                raise
            time.sleep(min(0.005 * (attempt + 1), 0.05))


class SQLiteConnection:
    """The original backend: WAL, a long busy timeout, dict rows.

    Writes are serialised process-wide (see `_SQLITE_WRITE_LOCK`); reads are not.
    """

    dialect = "sqlite"

    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Whether this connection currently holds the process write lock for an
        # open transaction. See `_hold_write_lock`.
        self._txn_locked = False
        # The busy timeout is the *waiting* mechanism for writes now (see
        # `_retry_write`), so it is generous rather than tiny: it is spent
        # inside SQLite, where it costs nothing but a park. The connection-level
        # default is 10 s; 3 s per attempt with the retry loop on top is
        # enough for any realistic contention without turning a stuck writer
        # into a hung request.
        self._conn.execute("PRAGMA busy_timeout = 3000;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")
        # WAL is a property of the *file*, not of the connection: once it is
        # set, every later connection sees WAL. So the switch is needed once
        # per file. Asking SQLite on every connection — and every request opens
        # one — put a lock convoy on the hot path: measured 13.6 req/s at one
        # concurrent request and 0.7 req/s at sixteen, because each opener
        # queued behind the previous one's wait for the file lock.
        key = str(self.path.resolve())
        if key not in _WAL_FILES:
            with _SQLITE_WRITE_LOCK:
                try:
                    mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
                    if str(mode or "").lower() != "wal":
                        self._conn.execute("PRAGMA journal_mode = WAL;")
                    _WAL_FILES.add(key)
                except sqlite3.OperationalError:
                    # Another connection is switching it right now; WAL is
                    # persistent, so losing that race is harmless. Do not cache
                    # the path — the next connection must try again.
                    pass

    def _run(self, sql, params=()):
        if params:
            return _with_retry(lambda: self._conn.execute(sql, params))
        return _with_retry(lambda: self._conn.execute(sql))

    def execute(self, sql, params=()):
        if _WRITE_STMT.match(sql):
            # No process lock around the write, and the wait happens *inside*
            # SQLite (the busy timeout) rather than in a Python sleep loop.
            #
            # The app-level lock was an attempt to stop writers colliding, but
            # it inverted the lock order: a connection holding SQLite's write
            # lock (an uncommitted transaction) waited for the process lock
            # while another thread held the process lock and waited for
            # SQLite's — and holding it across a transaction meant one
            # uncommitted connection blocked every writer in the process.
            # SQLite's own locking arbitrates correctly between connections;
            # the busy timeout does the waiting efficiently. The process lock
            # stays for *schema* work, where it is what serialises DDL.
            if params:
                return _retry_write(lambda: self._conn.execute(sql, params))
            return _retry_write(lambda: self._conn.execute(sql))
        return self._run(sql, params)

    def executemany(self, sql, seq):
        return _retry_write(lambda: self._conn.executemany(sql, seq))

    def executescript(self, script: str) -> None:
        # DDL: the process lock is what keeps two initialisations from racing
        # each other into "table already exists" or a half-created view.
        with _SQLITE_WRITE_LOCK:
            _retry_write(lambda: self._conn.executescript(script))

    def columns(self, table: str) -> set[str]:
        return {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def last_insert_id(self) -> int | None:
        row = self._conn.execute("SELECT last_insert_rowid()").fetchone()
        return int(row[0]) if row else None

    def commit(self) -> None:
        _retry_write(self._conn.commit)

    def rollback(self) -> None:
        _retry_write(self._conn.rollback)

    def close(self) -> None:
        self._conn.close()

    @property
    def raw(self):
        return self._conn


class PostgresConnection:
    """psycopg2 with `DictCursor`.

    `DictCursor` returns `DictRow`, which answers to both `row["col"]` (what
    `Store` uses) and `row[0]` (what the tests use). A plain `RealDictCursor`
    would have broken every positional read.
    """

    dialect = "postgres"

    def __init__(self, dsn: str) -> None:
        import psycopg2
        from psycopg2.extras import DictCursor

        # gssencmode=disable: without it libpq attempts a GSSAPI handshake and a
        # machine with a stale Kerberos realm fails before it ever reaches the
        # server. It is a client default, not a server requirement.
        self._conn = psycopg2.connect(dsn, cursor_factory=DictCursor,
                                      gssencmode="disable")

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(translate(sql), params if params else None)
        return cur

    def executemany(self, sql, seq):
        cur = self._conn.cursor()
        cur.executemany(translate(sql), seq)
        return cur

    def executescript(self, script: str) -> None:
        # psycopg2 sends one command string; a multi-statement schema script is
        # fine in a single execute().
        cur = self._conn.cursor()
        cur.execute(script)
        cur.close()

    def columns(self, table: str) -> set[str]:
        # Scoped to the search path, not just the table name. The test suite runs each
        # case in its own schema, so an unscoped lookup reports columns from *every*
        # schema that happens to hold a table by this name: `_migrate` then concludes
        # the column already exists (it does — elsewhere) and skips the ALTER, and the
        # index that needs it fails on the schema actually in use. That is exactly how
        # a newly added column broke only the Postgres leg, and only after the first
        # test had initialised its own schema.
        cur = self._conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = %s AND table_schema = ANY(current_schemas(false))",
            (table,))
        return {r["column_name"] for r in cur.fetchall()}

    def last_insert_id(self) -> int | None:
        """The id of the row just inserted.

        Not `cursor.lastrowid`: psycopg2 always reports 0 there, so the SQLite
        idiom silently returns a *wrong* id on this engine — which is how decision
        ids were 0 on Postgres while the rows were numbered normally. `lastval()`
        is the sequence value this session just produced, i.e. the insert that
        prompted the call.
        """
        cur = self._conn.cursor()
        cur.execute("SELECT lastval()")
        row = cur.fetchone()
        cur.close()
        return int(row["lastval"]) if row else None

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    @property
    def raw(self):
        return self._conn


def connect(target):
    """Open a connection. Applying the schema is the caller's job.

    Deliberately not done here: `Store` is constructed per request, and SQLite's
    `executescript` issues an implicit COMMIT. Re-running the DDL on every
    request would be both wasteful and a transaction-boundary change, so the
    once-per-process guard stays in `Store` where it always was.
    """
    if is_postgres(target):
        return PostgresConnection(str(target))
    return SQLiteConnection(target)


def schema_for(target, *, sqlite_ddl: str, postgres_ddl: str) -> str:
    return postgres_ddl if is_postgres(target) else sqlite_ddl
