"""Run the suite against Postgres when asked.

SQLite is the local backend and stays the default: `pytest` with no environment
set does exactly what it always did. Set `MI_TEST_PG_DSN` and every `Store(...)`
in the suite is redirected to Postgres, one schema per test so the isolation
`tmp_path` used to give us still holds:

    MI_TEST_PG_DSN="postgresql://user@127.0.0.1:55433/mininfer" pytest -q

This is the point of the dialect seam in `mininfer/db.py`: the same 47
statements run on both engines, so the same tests can prove it. A store that
happens to work on SQLite and silently misbehave on Postgres is the failure this
guards against.

Two tests open `sqlite3` directly rather than going through `Store`. They cannot
be redirected, so they skip in this mode instead of quietly asserting against
the wrong engine.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading

import pytest

DSN = os.environ.get("MI_TEST_PG_DSN")

# One schema per distinct store target, created once. The proxy's multiplex test
# builds two Stores from two threads, and two concurrent `create schema if not
# exists` for the *same* schema race in Postgres' catalog (the loser fails with a
# duplicate-key/`tuple concurrently updated` error, which surfaces as a spurious
# test failure). Creation is cheap and idempotent; doing it under a lock and once
# per schema is what makes the Postgres run deterministic.
_SCHEMAS: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


def pytest_report_header(config):
    if DSN:
        return f"mininfer: Store -> Postgres ({re.sub(r'//.*@', '//***@', DSN)})"
    return "mininfer: Store -> SQLite"


def pytest_configure(config):
    if not DSN:
        return
    import psycopg2

    import mininfer.db as db

    import mininfer.store as store_mod

    original = store_mod.Store.__init__

    def redirected(self, path="mininfer.db"):
        # Idempotent: some code paths build a second Store from the first one's
        # `target`, which is already the redirected DSN. Re-deriving a schema from
        # that would open a *different*, empty database — the failure would look
        # like the feature was broken rather than the harness.
        if db.is_postgres(path):
            original(self, path)
            return
        # One Postgres schema per distinct store target. The suite keys isolation
        # on `tmp_path`, so this preserves that: two Stores built from the same
        # path share a schema, and no two tests do.
        tag = hashlib.sha1(str(path).encode()).hexdigest()[:12]
        schema = f"t_{tag}"
        with _SCHEMA_LOCK:
            if schema not in _SCHEMAS:
                admin = psycopg2.connect(DSN, gssencmode="disable")
                admin.autocommit = True
                with admin.cursor() as cur:
                    cur.execute(f'create schema if not exists "{schema}"')
                admin.close()
                _SCHEMAS.add(schema)

        sep = "&" if "?" in DSN else "?"
        original(self, f"{DSN}{sep}options=-csearch_path%3D{schema}")

    store_mod.Store.__init__ = redirected


#: Tuning knobs a developer's `.env` may set, loaded into `os.environ` when
#: `mininfer.proxy` is imported. Left ambient, `MI_CHAT_SEARCH=auto` would make
#: every test that posts a cue-bearing prompt hit the real search network — the
#: suite must not depend on the machine's configuration.
_AMBIENT_ENV = (
    "MI_CHAT_SEARCH", "MI_CHAT_SEARCH_PROVIDER", "MI_SEARCH_DAILY_LIMIT",
    "MI_SEARCH_ALLOW_PAID",
    "MI_SCOUT_TONE", "MI_SCOUT_SEARCH_PROVIDER",
    # These change routing/agent behaviour, so a developer's `.env` must not leak
    # into a test that asserts an exact chain.
    "MI_AGENT_MODEL", "MI_AGENT_MODELS", "MI_INTENT_MODEL",
    "MI_SESSION_TOKEN_LIMIT", "MI_SESSION_COST_LIMIT",
)

#: A whitespace value disables the reserve: `route()` strips and falls back to
#: `None` when the result is empty, which beats `config/policy.yaml`'s operator
#: value without inventing an arm. Tests that assert an exact call chain must not
#: inherit it; tests that exercise the reserve set `MI_LAST_RESORT` themselves.
_LAST_RESORT_OFF = " "


@pytest.fixture(autouse=True)
def _no_ambient_tuning(monkeypatch):
    """Import the app (which loads `.env`) once, then clear the knobs it set."""
    import mininfer.proxy  # noqa: F401  - ensures `load_env()` has run
    for var in _AMBIENT_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MI_LAST_RESORT", _LAST_RESORT_OFF)
    yield


@pytest.fixture
def pg_or_sqlite():
    return "postgres" if DSN else "sqlite"
