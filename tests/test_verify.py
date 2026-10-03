"""`mi verify` — the daily check that a "free" label is still true.

The failure it exists for: a provider starts charging for a model that was free
when it was ingested. Nothing errors, the router keeps choosing it on price, and
every request quietly costs money. `verify` re-reads the catalogues and reports
the difference; these tests pin the difference logic without any network.
"""
from __future__ import annotations

import argparse

import pytest

from mininfer import cli
from mininfer.schema import Deployment, Weights
from mininfer.store import Store


def _seed(tmp_path):
    db = tmp_path / "v.db"
    s = Store(db)
    s.upsert_weights(Weights("w:free", "free-one"))
    s.upsert_weights(Weights("w:paid", "paid-one"))
    s.upsert_deployment(Deployment(
        "p:free", "w:free", "p", "free", price_in=0.0, price_out=0.0,
        zero_price=1, context_window=8000))
    s.upsert_deployment(Deployment(
        "p:paid", "w:paid", "p", "paid", price_in=1e-6, price_out=2e-6,
        context_window=8000))
    s.commit()
    s.close()
    return db


def _args(db, **kw):
    base = dict(db=str(db), sources=[], force=True, no_endpoints=True,
                endpoints_top=25, limit=0, report="")
    base.update(kw)
    return argparse.Namespace(**base)


def test_a_free_arm_that_started_charging_is_reported(tmp_path, monkeypatch, capsys):
    db = _seed(tmp_path)

    def fake_ingest(store, _args):
        # The provider now charges for it.
        store.conn.execute("UPDATE deployments SET zero_price=0, free_variant=0,"
                           " price_in=1e-6, price_out=3e-6 WHERE deploy_id='p:free'")
        store.commit()

    monkeypatch.setattr(cli, "_ingest_all", fake_ingest)
    assert cli.cmd_verify(_args(db)) == 0
    out = capsys.readouterr().out
    assert "FREE -> PAID" in out
    assert "p:free" in out
    assert "1 free-status flip" in out


def test_an_unchanged_registry_reports_no_drift(tmp_path, monkeypatch, capsys):
    db = _seed(tmp_path)
    monkeypatch.setattr(cli, "_ingest_all", lambda store, _args: None)
    assert cli.cmd_verify(_args(db)) == 0
    out = capsys.readouterr().out
    assert "0 changed" in out and "0 free-status flips" in out


def test_a_price_change_is_reported_without_a_flip(tmp_path, monkeypatch, capsys):
    db = _seed(tmp_path)

    def fake_ingest(store, _args):
        store.conn.execute("UPDATE deployments SET price_in=9e-6 WHERE deploy_id='p:paid'")
        store.commit()

    monkeypatch.setattr(cli, "_ingest_all", fake_ingest)
    cli.cmd_verify(_args(db))
    out = capsys.readouterr().out
    assert "price" in out and "p:paid" in out
    assert "0 free-status flips" in out


def test_the_report_is_written_as_json(tmp_path, monkeypatch):
    import json

    db = _seed(tmp_path)

    def fake_ingest(store, _args):
        store.conn.execute("UPDATE deployments SET zero_price=0 WHERE deploy_id='p:free'")
        store.commit()

    monkeypatch.setattr(cli, "_ingest_all", fake_ingest)
    report = tmp_path / "verify.json"
    cli.cmd_verify(_args(db, report=str(report)))
    data = json.loads(report.read_text())
    assert data["free_flips"] == ["p:free"]
    assert data["checked"] == 2


# --------------------------------------------------------------------------- #
# `mi refresh` — the single command a scheduler runs
# --------------------------------------------------------------------------- #


def _refresh_args(**over):
    base = dict(sources=[], force=False, no_endpoints=False, endpoints_top=25,
                limit=0, report="", source="vercel")
    base.update(over)
    return argparse.Namespace(**base)


def test_refresh_runs_verify_then_reconcile_then_metrics(monkeypatch):
    calls: list = []

    def rec(name, **kw):
        def fn(_a):
            calls.append((name, kw))
            return 0
        return fn

    monkeypatch.setattr(cli, "cmd_verify", rec("verify"))
    monkeypatch.setattr(cli, "cmd_reconcile", rec("reconcile"))
    monkeypatch.setattr(cli, "cmd_metrics", rec("metrics"))

    assert cli.cmd_refresh(_refresh_args()) == 0
    # The order is the contract: reconcile re-derives from the evidence verify
    # just wrote, and metrics enriches what reconcile settled.
    assert [c[0] for c in calls] == ["verify", "reconcile", "metrics"]


def test_refresh_forces_the_fetch_for_verify_and_writes_for_metrics(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(cli, "cmd_verify", lambda a: seen.update(v_force=a.force) or 0)
    monkeypatch.setattr(cli, "cmd_reconcile", lambda a: 0)
    monkeypatch.setattr(cli, "cmd_metrics", lambda a: seen.update(m_dry=a.dry_run) or 0)

    assert cli.cmd_refresh(_refresh_args(force=False)) == 0
    assert seen["v_force"] is True     # a cached payload cannot verify anything
    assert seen["m_dry"] is False      # a refresh writes


def test_refresh_stops_when_verify_fails(monkeypatch):
    calls: list = []
    monkeypatch.setattr(cli, "cmd_verify", lambda a: calls.append("verify") or 3)
    monkeypatch.setattr(cli, "cmd_reconcile", lambda a: calls.append("reconcile") or 0)
    monkeypatch.setattr(cli, "cmd_metrics", lambda a: calls.append("metrics") or 0)

    assert cli.cmd_refresh(_refresh_args()) == 3
    assert calls == ["verify"]   # a failed fetch must not reconcile stale data
