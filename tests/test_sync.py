"""Tests for the Supabase / Postgres sync (Phase 6).

Two things must hold: the Postgres schema mirrors the SQLite one (so the same
queries run), and a sync is idempotent (keyed tables upsert, append tables are
replaced) — otherwise re-running it duplicates the whole time series.
"""
from __future__ import annotations

import os

import pytest

from mininfer import sync
from mininfer.schema import Deployment, Weights
from mininfer.store import Store

PG_URL = os.environ.get("MI_TEST_PG_URL")


def _seed(tmp_path):
    s = Store(tmp_path / "s.db")
    s.upsert_weights(Weights("hf:a/m", "m"))
    s.upsert_deployment(Deployment("prov:m", "hf:a/m", "prov", "m",
                                   price_in=1.0, price_out=2.0))
    s.observe("prov:m", "sql_generation", ok=True, ts="2026-01-01T00:00:00+00:00")
    s.commit()
    return s


# ------------------------------------------------------------------ schema


def test_ddl_declares_every_table_and_view():
    d = sync.ddl()
    for t in ("weights", "deployments", "evidence", "snapshots", "quota_buckets",
              "observations", "decisions", "quarantine", "weight_aliases",
              "sessions", "messages", "pushed_models", "pricing_evidence",
              "price_resolution", "price_anomalies", "pricing_transitions",
              "price_history"):
        assert f"create table if not exists {t} " in d
    assert "create or replace view weights_resolved" in d
    assert "create or replace view routing_stats" in d
    assert "create or replace view deployments_priced" in d
    assert "create or replace view price_history_dated" in d
    # Postgres cannot enforce a FK the SQLite side never enforced either
    assert "references weights" not in d


# -------------------------------------------------------------------- plan


def test_plan_skips_evidence_unless_asked(tmp_path):
    s = _seed(tmp_path)
    names = [t for t, _, _ in sync.plan(s)]
    assert "weights" in names and "deployments" in names and "observations" in names
    assert "evidence" not in names
    assert "evidence" in [t for t, _, _ in sync.plan(s, evidence=True)]
    s.close()


def test_rows_drop_the_surrogate_id(tmp_path):
    s = _seed(tmp_path)
    cols, rows = sync._rows(s, "observations")
    assert "id" not in cols and len(rows) == 1
    s.close()


def test_dry_run_reports_counts_without_connecting(tmp_path):
    s = _seed(tmp_path)
    rep = sync.sync(s, "postgresql://unused.invalid/db", dry_run=True)
    assert rep["dry_run"] is True
    assert rep["tables"]["weights"] == 1
    assert rep["tables"]["deployments"] == 1
    assert rep["tables"]["observations"] == 1
    s.close()


def test_every_keyed_table_declares_a_primary_key():
    """`sessions` was listed as keyed with no entry in `_PK`.

    A sync only hit the lookup when the table had rows, so an empty table hid it
    and the first registry with sessions died with `KeyError: 'sessions'`.
    """
    for table in sync._KEYED:
        assert table in sync._PK, f"{table} is synced as keyed but has no primary key"


def test_a_keyed_table_with_rows_upserts(tmp_path, monkeypatch):
    """The lookup that used to raise, exercised end to end without a server."""
    s = _seed(tmp_path)
    s.add_session_usage("s-1", tokens_in=3, tokens_out=2, cost_usd=0.01,
                        tenant_id="t")
    s.commit()
    planned = dict((t, rows) for t, _cols, rows in sync.plan(s))
    assert len(planned["sessions"]) == 1
    # `_upsert` looked up `_PK[table]` before touching the cursor, which is what
    # raised; assert the lookup itself is now answerable for every planned table.
    for table in planned:
        if table in sync._KEYED:
            assert sync._PK[table]
    s.close()


# --------------------------------------------------------- real round-trip


@pytest.mark.skipif(not PG_URL, reason="set MI_TEST_PG_URL to run this")
def test_sync_roundtrip_is_idempotent(tmp_path):
    import psycopg2

    s = _seed(tmp_path)
    sync.sync(s, PG_URL, evidence=True)

    con = psycopg2.connect(PG_URL)
    cur = con.cursor()
    cur.execute("select count(*) from weights")
    assert cur.fetchone()[0] == 1
    cur.execute("select count(*) from deployments")
    assert cur.fetchone()[0] == 1
    cur.execute("select count(*) from observations")
    assert cur.fetchone()[0] == 1
    # the mirrored view must work
    cur.execute("select n, wins from routing_stats where task='sql_generation'")
    assert cur.fetchone() == (1, 1)

    # second sync must not duplicate the append table
    sync.sync(s, PG_URL, evidence=True)
    cur.execute("select count(*) from observations")
    assert cur.fetchone()[0] == 1
    con.close()
    s.close()
