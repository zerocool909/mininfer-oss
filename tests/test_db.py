"""The dialect translator, which is the whole risk surface of the Postgres port.

Every statement in the registry goes through `translate()` on its way to
psycopg2, and psycopg2 does not parse the SQL before formatting it. That makes
two mistakes possible and both silent: an unescaped `%` raises at the server
*if you are lucky*, and a `?` inside a string literal becomes `%s` and quietly
changes what the query means. These are pure-function tests, so they run
everywhere — the end-to-end proof is `MI_TEST_PG_DSN=... pytest`.
"""
from __future__ import annotations

import pytest

from mininfer.db import is_postgres, target_from_env, translate


@pytest.mark.parametrize("target,expected", [
    ("postgresql://u@h/db", True),
    ("postgres://u@h/db", True),
    # `pathlib.Path("postgresql://h/db")` collapses the slashes. Treating that as
    # SQLite would create a file named after the URL instead of failing.
    ("postgresql:/u@h/db", True),
    ("mininfer.db", False),
    ("/data/mininfer.db", False),
    ("mininfer.db", False),
])
def test_backend_is_chosen_by_the_target(target, expected):
    assert is_postgres(target) is expected


def test_positional_placeholder():
    assert translate("SELECT * FROM t WHERE a=? AND b=?") == \
        "SELECT * FROM t WHERE a=%s AND b=%s"


def test_named_placeholder():
    assert translate("VALUES (:weights_id, :display_name)") == \
        "VALUES (%(weights_id)s, %(display_name)s)"


def test_percent_is_escaped():
    """A LIKE wildcard is not a format character, but psycopg2 cannot tell."""
    assert translate("WHERE reason LIKE '%\"intent\"%'") == \
        "WHERE reason LIKE '%%\"intent\"%%'"


def test_placeholder_inside_a_string_literal_is_text():
    assert translate("SELECT '?' AS q, ? AS p") == "SELECT '?' AS q, %s AS p"


def test_colon_inside_a_string_literal_is_text():
    """A time literal must not become a parameter."""
    assert translate("SELECT '12:30' AS t, :name AS n") == \
        "SELECT '12:30' AS t, %(name)s AS n"


def test_comment_is_left_alone():
    out = translate("SELECT 1 -- why is a? and b:?\n, ? AS x")
    assert "why is a? and b:?" in out
    assert out.rstrip().endswith("%s AS x")


def test_scalar_max_becomes_greatest():
    """SQLite's two-argument MAX(a, b) has no Postgres equivalent under that name."""
    assert translate("SET x = MAX(a, b)") == "SET x = GREATEST(a, b)"
    assert translate("SET x = MIN(a, b)") == "SET x = LEAST(a, b)"


def test_aggregate_max_is_untouched():
    """The one-argument aggregate is the same in both engines."""
    assert translate("SELECT MAX(price_in), MIN(price_in) FROM t") == \
        "SELECT MAX(price_in), MIN(price_in) FROM t"


def test_scalar_max_with_placeholders_keeps_them():
    assert translate("SET x = MAX(?, col)") == "SET x = GREATEST(%s, col)"


def test_reserved_word_window_is_quoted_in_the_source_not_here():
    """`window` is reserved in Postgres, so store.py quotes it. Documented by test."""
    assert translate('SELECT "window" FROM quota_buckets') == \
        'SELECT "window" FROM quota_buckets'


def test_a_real_statement_from_the_registry():
    """The session ledger's upsert, which needed both fixes."""
    sql = (
        """INSERT INTO sessions (session_id, calls) VALUES (?, ?)\n"""
        """ON CONFLICT(session_id) DO UPDATE SET calls = sessions.calls + excluded.calls"""
    )
    out = translate(sql)
    assert "VALUES (%s, %s)" in out
    assert "sessions.calls + excluded.calls" in out
    assert "?" not in out


# --------------------------------------------------------------------------- #
# a DSN must never go through `pathlib.Path`
# --------------------------------------------------------------------------- #

def test_target_from_env_keeps_a_dsn_intact():
    dsn = "postgresql://u:p@postgres:5432/mininfer"
    assert target_from_env(dsn) == dsn
    assert target_from_env("postgres://u@h/db") == "postgres://u@h/db"


def test_target_from_env_makes_a_path_for_a_file():
    import pathlib

    assert isinstance(target_from_env("mininfer.db"), pathlib.Path)


def test_the_default_db_keeps_a_dsn_intact(monkeypatch):
    """Both the proxy and the CLI build the default target the same way.

    `pathlib.Path("postgresql://host/db")` collapses the slashes to
    `postgresql:/host/db`, and psycopg2 rejects that — so the proxy could never
    reach a Postgres registry from `MI_DB`, however correct the DSN was. The
    tests never caught it because they build `Store(dsn)` directly; only the
    default path went through `Path`.
    """
    dsn = "postgresql://u:p@postgres:5432/mininfer"
    monkeypatch.setenv("MI_DB", dsn)
    from mininfer import cli, proxy

    assert proxy._default_db() == dsn
    assert cli._default_db() == dsn


def test_record_decision_returns_the_row_it_just_wrote(tmp_path):
    """Portable, and it has to be: `cursor.lastrowid` is always 0 on psycopg2.

    The SQLite idiom works locally and returns a *wrong* id on Postgres, so every
    caller of `record_decision` — the decision-id header, the compare preference
    write, the escalation write-back — silently addressed row 0. This asserts the
    contract on whichever engine the suite is running against, so the Postgres leg
    of CI covers it too.
    """
    from mininfer.store import Store

    s = Store(tmp_path / "ids.db")
    first = s.record_decision(task="t", policy="p", mode="auto", chosen="a",
                              candidates=["a"], reason={})
    second = s.record_decision(task="t", policy="p", mode="auto", chosen="b",
                               candidates=["b"], reason={})
    s.commit()
    newest = s.conn.execute("SELECT id FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    assert second == newest["id"], (second, newest["id"])
    assert second != first, "each insert must get its own id"
    s.close()
