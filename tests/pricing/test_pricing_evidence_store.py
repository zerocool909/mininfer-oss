"""P1 — evidence becomes the authoritative input for a deployment's price.

The property this file pins: an adapter emits *observations*, and the store
resolves the deployment's stored economics from them. Two rules matter and are
both deliberately trivial in P1, because P1's job is to move the decision out of
the adapter and into the store — not to arbitrate between observations:

* observations are **append-only**; a price is never edited, only superseded;
* resolution is **deployment-level** and picks the newest observation per kind.

The second rule is the one that must survive P2. A peer's price must never be able
to appear here, however clever the reconciler becomes.
"""
from __future__ import annotations

import pytest

from mininfer.pricing.providers import read_prices
from mininfer.schema import Deployment, Weights
from mininfer.store import Store

T0 = "2026-10-01T00:00:00+00:00"
T1 = "2026-10-02T00:00:00+00:00"
NEWER = "2026-10-05T00:00:00+00:00"


def _novita(*, raw_in: int = 750, raw_out: int = 2200, observed_at: str = T1,
            confidence: float = 0.95):
    """Read a Novita-shaped payload into observations, with provenance."""
    return read_prices(
        "novita",
        {"input_token_price_per_m": raw_in, "output_token_price_per_m": raw_out},
        source="novita",
        url="https://api.novita.ai/v3/openai/models",
        observed_at=observed_at,
        confidence=confidence,
    )


def _ingest(s: Store, *, deploy_id: str = "novita:m", **kw) -> None:
    s.upsert_weights(Weights("w", "m"))
    s.upsert_deployment(
        Deployment(deploy_id, "w", "novita", deploy_id.split(":", 1)[1]),
        prices=_novita(**kw),
    )
    s.commit()


def _one(s: Store, sql: str, *args):
    return s.conn.execute(sql, args).fetchone()


# ------------------------------------------------------------- append-only


def test_each_ingest_appends_rather_than_replaces(tmp_path):
    """A price is a fact about a moment, so a second read adds a row.

    Replacing would destroy the history a reconciliation decision is made from —
    the same reason `observations` is append-only.
    """
    s = Store(tmp_path / "t.db")
    _ingest(s)
    _ingest(s)  # the identical snapshot, read again
    n = _one(s, "SELECT COUNT(*) c FROM pricing_evidence")["c"]
    assert n == 4  # 2 kinds x 2 reads
    s.close()


def test_a_replayed_older_snapshot_does_not_win_by_insertion_order(tmp_path):
    """Newest is `observed_at`, not highest rowid.

    A snapshot replayed later carries its *original* timestamp. If resolution
    ordered by insertion it would silently become the current price.
    """
    s = Store(tmp_path / "t.db")
    _ingest(s, raw_in=600, raw_out=1800, observed_at=NEWER)   # read 2026-10-05
    _ingest(s, raw_in=750, raw_out=2200, observed_at=T0)      # replay of 2026-10-01
    row = _one(s, "SELECT price_in FROM deployments WHERE deploy_id='novita:m'")
    assert row["price_in"] == pytest.approx(0.06)             # the newer reading
    s.close()


# ------------------------------------------------- deployment-level resolution


def test_the_stored_price_is_resolved_from_the_observation(tmp_path):
    s = Store(tmp_path / "t.db")
    _ingest(s)
    row = _one(s, "SELECT price_in, price_out FROM deployments WHERE deploy_id='novita:m'")
    assert row["price_in"] == pytest.approx(0.075)
    assert row["price_out"] == pytest.approx(0.22)
    s.close()


def test_resolution_reads_only_this_deployment(tmp_path):
    """The rule P2 must not break. A peer's price is never this deployment's."""
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "m"))
    _ingest(s, deploy_id="novita:cheap")
    # A second deployment of the same weights at a wildly different price.
    s.upsert_deployment(
        Deployment("other:expensive", "w", "other", "expensive"),
        prices=read_prices(
            "deepinfra",
            {"metadata": {"pricing": {"input_tokens": 9.0, "output_tokens": 9.0}}},
            observed_at=T1,
        ),
    )
    s.commit()
    cheap = _one(s, "SELECT price_in FROM deployments WHERE deploy_id='novita:cheap'")
    expensive = _one(s, "SELECT price_in FROM deployments WHERE deploy_id='other:expensive'")
    assert cheap["price_in"] == pytest.approx(0.075)   # untouched by the peer
    assert expensive["price_in"] == pytest.approx(9.0)
    s.close()


def test_a_deployment_with_no_observations_keeps_its_own_price(tmp_path):
    """The legacy path: seeds and `agent_ingest` still construct prices directly."""
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "m"))
    s.upsert_deployment(Deployment("seed:m", "w", "seed", "m", price_in=1.0, price_out=2.0))
    s.commit()
    row = _one(s, "SELECT price_in, price_out FROM deployments WHERE deploy_id='seed:m'")
    assert row["price_in"] == pytest.approx(1.0)
    assert row["price_out"] == pytest.approx(2.0)
    s.close()


# ---------------------------------------------------------------- provenance


def test_the_view_reports_where_the_price_came_from(tmp_path):
    """The price is stored; *which source said it* is the reconciler's output."""
    s = Store(tmp_path / "t.db")
    _ingest(s)
    s.reconcile_prices()
    s.commit()
    row = _one(
        s,
        "SELECT price_in_canonical, price_in_source, price_in_observed_at,"
        " price_in_confidence, price_in_state FROM deployments_priced"
        " WHERE deploy_id='novita:m'",
    )
    assert row["price_in_canonical"] == pytest.approx(0.075)
    assert row["price_in_source"] == "novita"
    assert row["price_in_observed_at"] == T1
    assert row["price_in_confidence"] == pytest.approx(0.95)
    assert row["price_in_state"] == "canonical"
    s.close()


def test_the_view_prefers_the_newest_observation(tmp_path):
    s = Store(tmp_path / "t.db")
    _ingest(s, raw_in=750, raw_out=2200, observed_at=T0)
    _ingest(s, raw_in=600, raw_out=1800, observed_at=NEWER)
    s.reconcile_prices()
    s.commit()
    row = _one(s, "SELECT price_in_canonical, price_in_observed_at"
                  " FROM deployments_priced WHERE deploy_id='novita:m'")
    assert row["price_in_canonical"] == pytest.approx(0.06)
    assert row["price_in_observed_at"] == NEWER
    s.close()


# ------------------------------------------------------- unknowns and sentinels


def test_a_field_the_provider_did_not_publish_is_not_stored(tmp_path):
    """\"DeepInfra did not list a cache price\" is not a claim about the cache
    price, so it is not evidence and gets no row."""
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "m"))
    s.upsert_deployment(
        Deployment("novita:m", "w", "novita", "m"),
        prices=read_prices("novita", {"input_token_price_per_m": 750}, observed_at=T1),
    )
    s.commit()
    assert _one(s, "SELECT COUNT(*) c FROM pricing_evidence")["c"] == 1
    assert _one(s, "SELECT price_in FROM deployments WHERE deploy_id='novita:m'")["price_in"] \
        == pytest.approx(0.075)
    s.close()


def test_a_sentinel_is_recorded_but_never_becomes_a_price(tmp_path):
    """`-1` means \"depends on routing\". Recording it is diagnosis; pricing it is
    the bug that would make a competitor's meta-router look free."""
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "m"))
    s.upsert_deployment(
        Deployment("openrouter:m", "w", "openrouter", "m"),
        prices=read_prices(
            "openrouter",
            {"pricing": {"prompt": "-1", "completion": "-1000000"}},
            observed_at=T1,
        ),
    )
    s.commit()
    assert _one(s, "SELECT COUNT(*) c FROM pricing_evidence"
                   " WHERE deploy_id='openrouter:m'")["c"] == 2
    row = _one(s, "SELECT price_in, price_out FROM deployments WHERE deploy_id='openrouter:m'")
    assert row["price_in"] is None and row["price_out"] is None
    s.close()


def test_a_sentinel_does_not_erase_a_previous_good_price(tmp_path):
    """A meta-router flipping to a sentinel must not blank a real price."""
    s = Store(tmp_path / "t.db")
    s.upsert_weights(Weights("w", "m"))
    d = Deployment("openrouter:m", "w", "openrouter", "m")
    s.upsert_deployment(d, prices=read_prices(
        "openrouter", {"pricing": {"prompt": "0.000000075", "completion": "0.00000022"}},
        observed_at=T0))
    s.commit()
    s.upsert_deployment(d, prices=read_prices(
        "openrouter", {"pricing": {"prompt": "-1", "completion": "-1000000"}},
        observed_at=T1))
    s.commit()
    # The sentinel is newer, so resolution yields no *new* price; COALESCE keeps
    # the last known good one rather than blanking the column.
    assert _one(s, "SELECT price_in FROM deployments WHERE deploy_id='openrouter:m'")["price_in"] \
        == pytest.approx(0.075)
    s.close()


# ------------------------------------------------------- the free -> paid flip


def test_a_resolved_price_flip_hibernates_the_arm(tmp_path):
    """The whole point, end to end: free becomes paid through *evidence*, and the
    arm leaves the ranking instead of quietly spending."""
    s = Store(tmp_path / "t.db")
    _ingest(s, raw_in=0, raw_out=0, observed_at=T0)
    row = _one(s, "SELECT zero_price, status FROM deployments WHERE deploy_id='novita:m'")
    assert row["zero_price"] == 1 and row["status"] == "live"

    _ingest(s, raw_in=750, raw_out=2200, observed_at=T1)
    row = _one(s, "SELECT zero_price, status, status_reason"
                  " FROM deployments WHERE deploy_id='novita:m'")
    assert row["zero_price"] == 0
    assert row["status"] == "hibernated"
    assert "$0.0750" in row["status_reason"]   # the resolved price, per-Mtok
    s.close()
