"""P6 — the price *belief* timeline.

"What did MinInfer believe the price was on September 14?"

That question needs a different clock from the one `pricing_evidence` keeps.
`observed_at` is the **provider's** timeline: a price can have been true upstream
for a week before we ever saw it. `effective_from` is **ours** — when the
reconciler adopted a belief — and that is what a past routing decision was
actually made against.

The table is append-only and change-only: an ingest that re-confirms the same price
writes nothing, so it stays small enough to be the answer rather than a second copy
of the evidence log.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from mininfer.pricing.providers import read_prices
from mininfer.schema import Deployment, Weights
from mininfer.store import Store


def _add(s: Store, deploy_id: str, *, price: float, source: str, observed_at: str,
         weights: str = "w") -> None:
    provider, model = deploy_id.split(":", 1)
    s.upsert_weights(Weights(weights, weights))
    s.upsert_deployment(
        Deployment(deploy_id, weights, provider, model),
        prices=read_prices(
            "novita",
            {"input_token_price_per_m": round(price * 10_000),
             "output_token_price_per_m": round(price * 10_000)},
            source=source, observed_at=observed_at),
    )


def _market(s: Store, price: float, at: str) -> None:
    """Three independent sources agreeing, so the belief is `canonical`."""
    for name in ("novita", "openrouter", "deepinfra"):
        _add(s, f"{name}:m", price=price, source=name, observed_at=at)
    s.commit()
    s.reconcile_prices()
    s.commit()


def _beliefs(s: Store, deploy_id: str = "novita:m", kind: str = "input") -> list[dict]:
    return sorted((r for r in s.price_history(deploy_id, limit=99) if r["kind"] == kind),
                  key=lambda r: r["effective_from"])


# ------------------------------------------------------------- recording


def test_the_first_belief_is_recorded(tmp_path):
    s = Store(tmp_path / "t.db")
    _market(s, 0.075, "2026-09-10T00:00:00+00:00")

    rows = _beliefs(s)
    assert len(rows) == 1
    assert rows[0]["usd_per_mtok"] == pytest.approx(0.075)
    assert rows[0]["state"] == "canonical"
    assert rows[0]["source"] == "novita"
    s.close()


def test_a_reconfirmed_price_writes_nothing(tmp_path):
    """Change-only, or the table becomes a second copy of the evidence log."""
    s = Store(tmp_path / "t.db")
    _market(s, 0.075, "2026-09-10T00:00:00+00:00")
    _market(s, 0.075, "2026-09-20T00:00:00+00:00")   # newer evidence, same belief
    _market(s, 0.075, "2026-09-25T00:00:00+00:00")

    assert len(_beliefs(s)) == 1
    s.close()


def test_a_price_change_is_a_new_belief(tmp_path):
    s = Store(tmp_path / "t.db")
    _market(s, 0.075, "2026-09-10T00:00:00+00:00")
    _market(s, 0.09, "2026-09-20T00:00:00+00:00")

    rows = _beliefs(s)
    assert [r["usd_per_mtok"] for r in rows] == [pytest.approx(0.075), pytest.approx(0.09)]
    assert rows[0]["effective_from"] < rows[1]["effective_from"]
    s.close()


def test_a_trust_change_at_the_same_price_is_a_new_belief(tmp_path):
    """The belief is the price *and* whether we trust it. The same value becoming
    `quarantined` is news even though no digit changed."""
    s = Store(tmp_path / "t.db")
    _market(s, 0.075, "2026-09-10T00:00:00+00:00")

    # The other two sources drop 10x while novita re-confirms 0.075, so novita is
    # now the outlier: quarantined, falling back to its own previous value — which
    # is 0.075 again. The price did not move; the trust did.
    _add(s, "novita:m", price=0.075, source="novita", observed_at="2026-09-20T00:00:00+00:00")
    _add(s, "openrouter:m", price=0.0075, source="openrouter", observed_at="2026-09-20T00:00:00+00:00")
    _add(s, "deepinfra:m", price=0.0075, source="deepinfra", observed_at="2026-09-20T00:00:00+00:00")
    s.commit()
    s.reconcile_prices()
    s.commit()

    rows = _beliefs(s)
    assert [r["state"] for r in rows] == ["canonical", "quarantined"]
    assert all(r["usd_per_mtok"] == pytest.approx(0.075) for r in rows)
    s.close()


def test_an_unknown_price_is_a_belief_too(tmp_path):
    """`None` means \"we do not know\", which is a state worth dating — an arm that
    became unpriced at some point is exactly what an operator is looking for."""
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "w"))
    s.upsert_deployment(Deployment("novita:m", "w", "novita", "m"),
                        prices=read_prices("novita", {"input_token_price_per_m": 7500,
                                                      "output_token_price_per_m": 7500},
                                           source="novita",
                                           observed_at="2026-09-10T00:00:00+00:00"))
    s.commit()
    s.reconcile_prices()
    s.commit()
    assert _beliefs(s)[0]["usd_per_mtok"] == pytest.approx(0.75)

    # A second deployment joins so the first becomes the 10x outlier, and the
    # market needs three sources to have an opinion at all.
    _add(s, "openrouter:m", price=0.075, source="openrouter", observed_at="2026-09-20T00:00:00+00:00")
    _add(s, "deepinfra:m", price=0.075, source="deepinfra", observed_at="2026-09-20T00:00:00+00:00")
    s.commit()
    s.reconcile_prices()
    s.commit()

    rows = _beliefs(s)
    assert rows[-1]["state"] == "quarantined"
    assert rows[-1]["usd_per_mtok"] is None      # no earlier value to fall back to
    s.close()


# ------------------------------------------------------------- the queries


def test_as_of_returns_the_belief_in_effect_then(tmp_path):
    s = Store(tmp_path / "t.db")
    _market(s, 0.075, "2026-09-10T00:00:00+00:00")
    early = _beliefs(s)[0]["effective_from"]
    _market(s, 0.09, "2026-09-20T00:00:00+00:00")
    late = _beliefs(s)[1]["effective_from"]

    assert s.price_history("novita:m", as_of=early)[0]["usd_per_mtok"] == pytest.approx(0.075)
    assert s.price_history("novita:m", as_of=late)[0]["usd_per_mtok"] == pytest.approx(0.09)
    # And now is the latest.
    assert s.price_history("novita:m")[0]["usd_per_mtok"] == pytest.approx(0.09)
    s.close()


def test_as_of_before_any_belief_is_empty(tmp_path):
    s = Store(tmp_path / "t.db")
    _market(s, 0.075, "2026-09-10T00:00:00+00:00")
    assert s.price_history("novita:m", as_of="2020-01-01T00:00:00+00:00") == []
    s.close()


def test_as_of_returns_the_belief_not_the_nearest_observation(tmp_path):
    """The distinction the whole phase exists for: we adopt a belief when we *see*
    the evidence, which is not when the price changed upstream."""
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "w"))
    s.upsert_deployment(Deployment("novita:m", "w", "novita", "m"),
                        prices=read_prices("novita", {"input_token_price_per_m": 750,
                                                      "output_token_price_per_m": 750},
                                           source="novita",
                                           observed_at="2020-01-01T00:00:00+00:00"))
    s.commit()
    s.reconcile_prices()
    s.commit()

    row = s.price_history("novita:m")[0]
    # The provider's timestamp is old; ours is when we learned it.
    assert row["observed_at"] == "2020-01-01T00:00:00+00:00"
    assert row["effective_from"] > "2026-01-01"
    assert s.price_history("novita:m", as_of="2021-01-01T00:00:00+00:00") == []
    s.close()


def test_the_timeline_is_newest_first(tmp_path):
    s = Store(tmp_path / "t.db")
    _market(s, 0.075, "2026-09-10T00:00:00+00:00")
    _market(s, 0.09, "2026-09-20T00:00:00+00:00")

    rows = s.price_history("novita:m")
    assert [r["usd_per_mtok"] for r in rows if r["kind"] == "input"] == [
        pytest.approx(0.09), pytest.approx(0.075)]
    s.close()


def test_history_is_per_kind(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "w"))
    s.upsert_deployment(Deployment("novita:m", "w", "novita", "m"),
                        prices=read_prices("novita", {"input_token_price_per_m": 750,
                                                      "output_token_price_per_m": 2200},
                                           source="novita",
                                           observed_at="2026-09-10T00:00:00+00:00"))
    s.commit()
    s.reconcile_prices()
    s.commit()
    kinds = {r["kind"] for r in s.price_history("novita:m")}
    assert kinds == {"input", "output"}
    s.close()


def test_the_view_derives_effective_to(tmp_path):
    """`effective_to` is not stored — it is the next row's `effective_from`, which is
    what keeps the table append-only instead of updated in place."""
    s = Store(tmp_path / "t.db")
    _market(s, 0.075, "2026-09-10T00:00:00+00:00")
    _market(s, 0.09, "2026-09-20T00:00:00+00:00")

    rows = [dict(r) for r in s.conn.execute(
        "SELECT kind, usd_per_mtok, effective_from, effective_to FROM price_history_dated"
        " WHERE deploy_id='novita:m' AND kind='input' ORDER BY effective_from")]
    assert rows[0]["effective_to"] == rows[1]["effective_from"]
    assert rows[1]["effective_to"] is None       # still current
    s.close()


def test_a_narrowed_reconcile_still_records_history(tmp_path):
    s = Store(tmp_path / "t.db")
    _market(s, 0.075, "2026-09-10T00:00:00+00:00")
    before = len(s.price_history("novita:m", limit=99))
    s.reconcile_prices(deploy_ids=["novita:m"])
    s.commit()
    assert len(s.price_history("novita:m", limit=99)) == before   # nothing changed
    s.close()


# ------------------------------------------------------------- the surfaces


def test_reconcile_reports_the_history_count(tmp_path):
    s = Store(tmp_path / "t.db")
    _market(s, 0.075, "2026-09-10T00:00:00+00:00")
    rep = s.reconcile_prices()
    s.commit()
    assert rep["history"] == 0          # already recorded by `_market`
    s.close()


def test_the_endpoint_serves_the_belief_timeline(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    s = Store(db)
    _market(s, 0.075, "2026-09-10T00:00:00+00:00")
    s.close()

    monkeypatch.setenv("MI_DB", str(db))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    client = TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)

    body = client.get("/v1/economics/history", params={"deploy_id": "novita:m"}).json()
    assert body["count"] == 2                      # input + output
    assert {r["kind"] for r in body["history"]} == {"input", "output"}

    # `as_of` in the far past is an empty answer, not an error.
    past = client.get("/v1/economics/history",
                      params={"deploy_id": "novita:m",
                              "as_of": "2020-01-01T00:00:00+00:00"}).json()
    assert past["count"] == 0
