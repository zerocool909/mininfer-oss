"""P3 — the anomaly log: a disagreement becomes a reviewable incident.

The reconciler (P2) decides; this is the *record* of those decisions over time. The
properties that matter:

* one row per ongoing **incident**, not one per reconcile run — otherwise a
  30-minute cron buries the log in duplicate rows for one bad source;
* an operator's acknowledgement survives every later reconcile, and does not
  change a price;
* closing something that is still genuinely wrong does not silence it — the next
  reconcile opens a new row.

`stale` is deliberately not an anomaly kind: a stale price is a freshness fact (a
`price_resolution` state, fixed by re-ingesting), not a disagreement a human has
to adjudicate. Opening one anomaly per aged-out deployment would bury the real
ones.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from mininfer import store as store_module
from mininfer.pricing.providers import read_prices
from mininfer.schema import Deployment, Weights
from mininfer.store import Store

T0 = "2026-10-01T00:00:00+00:00"
T1 = "2026-10-02T00:00:00+00:00"


def _add(s: Store, deploy_id: str, *, price_in: float, source: str,
         observed_at: str = T1, weights: str = "w") -> None:
    provider, model = deploy_id.split(":", 1)
    s.upsert_weights(Weights(weights, weights))
    s.upsert_deployment(
        Deployment(deploy_id, weights, provider, model),
        prices=read_prices(
            "novita",
            {"input_token_price_per_m": round(price_in * 10_000),
             "output_token_price_per_m": round(price_in * 10_000)},
            source=source, observed_at=observed_at,
        ),
    )


def _market(s: Store, deploy: str, *, price: float, weights: str = "w",
            prefix: str = "ling") -> None:
    """`deploy` at `price`; two independent sources agreeing on $0.075."""
    _add(s, deploy, price_in=price, source=deploy.split(":")[0], weights=weights)
    _add(s, f"openrouter:{prefix}", price_in=0.075, source="openrouter", weights=weights)
    _add(s, f"deepinfra:{prefix}", price_in=0.075, source="deepinfra", weights=weights)
    s.commit()


def _anomalies(s: Store, status: str | None = None) -> list[dict]:
    return s.anomalies(status=status, limit=99)


# ------------------------------------------------------------- opening


def test_a_refused_price_opens_one_anomaly_per_dimension(tmp_path):
    """Input and output are separate prices, so they are separate incidents."""
    s = Store(tmp_path / "t.db")
    _market(s, "novita:ling", price=0.75)
    s.reconcile_prices()
    s.commit()

    rows = _anomalies(s)
    assert sorted(r["dimension"] for r in rows) == ["input", "output"]
    assert {r["kind"] for r in rows} == {"unit_scale"}
    assert {r["severity"] for r in rows} == {"high"}
    assert all(r["status"] == "open" for r in rows)
    s.close()


def test_re_reconciling_refreshes_rather_than_duplicating(tmp_path):
    """A 30-minute cron must not append 48 rows a day for one bad source."""
    s = Store(tmp_path / "t.db")
    _market(s, "novita:ling", price=0.75)
    first = s.reconcile_prices()
    s.commit()
    second = s.reconcile_prices()
    s.commit()

    assert first["anomalies"] == {"opened": 2, "refreshed": 0, "closed": 0}
    assert second["anomalies"] == {"opened": 0, "refreshed": 2, "closed": 0}
    assert len(_anomalies(s)) == 2
    s.close()


def test_a_twenty_x_deviation_is_critical(tmp_path):
    """The band is for severity, not for the decision: both refuse, but a 20x is
    not the same event as a 10x and the log should say so."""
    s = Store(tmp_path / "t.db")
    _market(s, "novita:big", price=1.5)   # 20x the market
    s.reconcile_prices()
    s.commit()
    assert {r["severity"] for r in _anomalies(s) if r["dimension"] == "input"} == {"critical"}
    s.close()


def test_a_spread_is_a_warning_not_a_high(tmp_path):
    """5-10x is flagged, not refused, so it is a `spread` at `warning` severity."""
    s = Store(tmp_path / "t.db")
    _market(s, "novita:mild", price=0.4)   # ~5.3x
    s.reconcile_prices()
    s.commit()
    row = next(r for r in _anomalies(s) if r["dimension"] == "input")
    assert row["kind"] == "spread"
    assert row["severity"] == "warning"
    assert row["status"] == "open"
    s.close()


def test_a_history_jump_without_a_market_is_flagged(tmp_path):
    """Too few sources for a market leaves the history rule, with no factor."""
    s = Store(tmp_path / "t.db")
    _add(s, "novita:solo", price_in=0.075, source="novita", observed_at=T0)
    _add(s, "novita:solo", price_in=0.75, source="novita", observed_at=T1)
    s.commit()
    s.reconcile_prices()
    s.commit()

    row = next(r for r in _anomalies(s) if r["dimension"] == "input")
    assert row["kind"] == "history_jump"
    assert row["severity"] == "warning"
    assert row["factor"] is None
    assert row["expected"] == pytest.approx(0.075)   # the previous value
    assert row["observed"] == pytest.approx(0.75)
    s.close()


# ------------------------------------------------------------- closing


def test_an_anomaly_closes_when_the_price_returns_to_canonical(tmp_path):
    s = Store(tmp_path / "t.db")
    _market(s, "novita:ling", price=0.75)
    s.reconcile_prices()
    s.commit()
    assert len(_anomalies(s)) == 2

    _add(s, "novita:ling", price_in=0.075, source="novita", observed_at="2026-10-03T00:00:00+00:00")
    s.commit()
    rep = s.reconcile_prices()
    s.commit()

    assert rep["anomalies"] == {"opened": 0, "refreshed": 0, "closed": 2}
    # Nothing is *open*; both rows remain as history.
    assert s.anomalies(status="open") == []
    assert len(_anomalies(s)) == 2
    resolved = s.anomalies(status="resolved")
    assert all(r["resolution"] == "price returned to canonical" for r in resolved)
    s.close()


def test_acknowledging_survives_a_reconcile(tmp_path):
    """The refresh must not reset `status` — otherwise an operator's decision is
    undone by the next cron tick and the alert keeps firing."""
    s = Store(tmp_path / "t.db")
    _market(s, "novita:ling", price=0.75)
    s.reconcile_prices()
    s.commit()
    anomaly_id = next(r for r in _anomalies(s) if r["dimension"] == "input")["anomaly_id"]

    assert s.decide_anomaly(anomaly_id, status="acknowledged", note="known adapter bug") is True
    s.reconcile_prices()
    s.commit()

    row = next(r for r in _anomalies(s) if r["anomaly_id"] == anomaly_id)
    assert row["status"] == "acknowledged"
    assert row["resolution"] == "known adapter bug"
    s.close()


def test_acknowledging_does_not_change_a_price(tmp_path):
    """It is a note to self about the alert, not an override of the decision."""
    s = Store(tmp_path / "t.db")
    _market(s, "novita:ling", price=0.75)
    s.reconcile_prices()
    s.commit()
    before = s.conn.execute(
        "SELECT price_in FROM deployments WHERE deploy_id='novita:ling'").fetchone()["price_in"]
    anomaly_id = _anomalies(s)[0]["anomaly_id"]

    s.decide_anomaly(anomaly_id, status="acknowledged")
    s.reconcile_prices()
    s.commit()

    after = s.conn.execute(
        "SELECT price_in FROM deployments WHERE deploy_id='novita:ling'").fetchone()["price_in"]
    assert before is None and after is None
    s.close()


def test_closing_a_still_wrong_anomaly_reopens_a_new_row(tmp_path):
    """Resolving is not silencing. The disagreement is still there, so it comes
    back as a new incident rather than staying quietly closed."""
    s = Store(tmp_path / "t.db")
    _market(s, "novita:ling", price=0.75)
    s.reconcile_prices()
    s.commit()
    first_id = next(r for r in _anomalies(s) if r["dimension"] == "input")["anomaly_id"]

    assert s.decide_anomaly(first_id, status="resolved", note="muted") is True
    rep = s.reconcile_prices()
    s.commit()

    assert rep["anomalies"]["opened"] == 1
    rows = [r for r in s.anomalies(status=None, limit=99) if r["dimension"] == "input"]
    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"resolved", "open"}
    assert len({r["anomaly_id"] for r in rows}) == 2
    s.close()


def test_decide_anomaly_rejects_an_unknown_status(tmp_path):
    s = Store(tmp_path / "t.db")
    with pytest.raises(ValueError):
        s.decide_anomaly("nope", status="muted")
    s.close()


# ------------------------------------------------------------- the record


def test_the_evidence_record_carries_coordinates_not_rowids(tmp_path):
    """`pricing_evidence.id` is dropped by `sync`, so an anomaly cannot reference
    it. The coordinates — source, observed_at, value — do survive."""
    s = Store(tmp_path / "t.db")
    _market(s, "novita:ling", price=0.75)
    s.reconcile_prices()
    s.commit()

    row = next(r for r in _anomalies(s) if r["dimension"] == "input")
    evidence = json.loads(row["evidence"])
    assert evidence["newest"]["source"] == "novita"
    assert evidence["newest"]["observed_at"] == T1
    assert evidence["newest"]["usd_per_mtok"] == pytest.approx(0.75)
    assert evidence["kept"] is None          # nothing earlier to fall back to
    # The id is the identity, not a row number.
    assert row["anomaly_id"].startswith("novita:ling|input|")
    s.close()


def test_anomalies_are_ordered_worst_first(tmp_path):
    s = Store(tmp_path / "t.db")
    _market(s, "novita:big", price=1.5, weights="w_crit", prefix="big")     # critical
    _market(s, "novita:mild", price=0.4, weights="w_warn", prefix="mild")   # warning
    s.reconcile_prices()
    s.commit()

    severities = [r["severity"] for r in s.anomalies(limit=99)]
    assert severities[0] == "critical"
    assert severities == sorted(severities, key=["critical", "high", "warning", "info"].index)
    s.close()


def test_a_stale_price_is_not_an_anomaly(tmp_path):
    """Expiry is a freshness fact, already visible as a `price_resolution` state.
    Logging it as an anomaly would drown the disagreements."""
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(days=store_module.PRICE_FRESHNESS_DAYS + 10)).isoformat(timespec="seconds")
    s = Store(tmp_path / "t.db")
    for name in ("novita", "openrouter", "deepinfra"):
        _add(s, f"{name}:m", price_in=0.075, source=name, observed_at=old)
    s.commit()
    rep = s.reconcile_prices()
    s.commit()

    assert rep["states"]["expired"] == 6
    assert s.anomalies(status=None, limit=99) == []
    s.close()


def test_a_deployment_with_no_disagreement_produces_no_anomaly(tmp_path):
    s = Store(tmp_path / "t.db")
    _market(s, "novita:fine", price=0.075)
    rep = s.reconcile_prices()
    s.commit()

    assert rep["states"]["canonical"] == 6
    assert rep["anomalies"] == {"opened": 0, "refreshed": 0, "closed": 0}
    assert s.anomalies(status=None, limit=99) == []
    s.close()
