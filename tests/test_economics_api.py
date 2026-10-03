"""P7 — the `/v1/economics/*` read API.

The endpoint that matters is `/v1/economics/overview`: every free/cost element on
the Overview page reads from it, in one call. That is not just convenience — a page
that fetched prices and trust states separately could render a price beside a
*different* moment's provenance, which is the kind of quiet inconsistency this
whole layer exists to prevent.

The shape is a superset of `/v1/stats`, so the page swaps one call for another
rather than merging two payloads.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mininfer import quota
from mininfer.pricing.providers import read_prices
from mininfer.schema import Deployment, Weights
from mininfer.store import Store


def _add(s: Store, deploy_id: str, price: float, source: str, *,
         weights: str = "w", observed_at: str = "2026-10-02T00:00:00+00:00") -> None:
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


def _seed(db_path) -> None:
    """A registry with one of everything the page renders."""
    s = Store(db_path)
    # Three sources agree; a fourth is 10x high and gets quarantined.
    for name in ("novita", "openrouter", "deepinfra"):
        _add(s, f"{name}:m", 0.075, name)
    _add(s, "groq:m", 0.75, "groq")

    # A free arm with a declared quota and a provider-reported limit.
    s.upsert_weights(Weights("wf", "wf"))
    s.upsert_deployment(Deployment("google:free", "wf", "google", "free",
                                   price_in=0.0, price_out=0.0, zero_price=True))
    s.set_limit("google:free", "day", 1000)
    quota.record_from_headers(s, "google:free", {
        "x-ratelimit-limit-requests": "20", "x-ratelimit-remaining-requests": "3",
        "x-ratelimit-reset-requests": "60s"}, observed_at="2026-10-02T00:00:00+00:00")

    # A free arm that started charging: hibernated, with a state transition.
    s.upsert_weights(Weights("wh", "wh"))
    s.upsert_deployment(Deployment("p:m", "wh", "p", "m", price_in=0.0, price_out=0.0,
                                   zero_price=True))
    s.commit()
    s.reconcile_prices()
    s.commit()
    s.upsert_deployment(Deployment("p:m", "wh", "p", "m", price_in=1.0, price_out=2.0))
    s.commit()
    s.reconcile_prices()
    s.commit()
    s.close()


def _client(tmp_path, monkeypatch) -> TestClient:
    db = tmp_path / "p.db"
    _seed(db)
    monkeypatch.setenv("MI_DB", str(db))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    return TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)


# ------------------------------------------------------------- the overview


def test_the_overview_carries_every_element_the_page_renders(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/v1/economics/overview").json()
    for key in ("counts", "providers", "quota", "savings", "decisions", "tasks",
                "reviews", "anomalies", "pricing_states", "generated_at"):
        assert key in body, key


def test_the_overview_is_a_superset_of_stats(tmp_path, monkeypatch):
    """So the page swaps one call for another instead of merging two payloads."""
    client = _client(tmp_path, monkeypatch)
    stats = client.get("/v1/stats").json()
    econ = client.get("/v1/economics/overview").json()
    assert set(stats) <= set(econ)
    assert stats["counts"] == econ["counts"]
    assert len(stats["providers"]) == len(econ["providers"])


def test_overview_providers_carry_the_provenance_of_the_cheapest_price(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/v1/economics/overview").json()
    novita = next(p for p in body["providers"] if p["provider"] == "novita")
    assert novita["min_in"] == pytest.approx(0.075)
    assert novita["min_in_source"] == "novita"
    assert novita["min_in_state"] == "canonical"


def test_overview_quota_reports_the_source_and_a_projection(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/v1/economics/overview").json()
    # A 60s reset is inferred into the `minute` bucket, which the policy never
    # configured — so this bucket's limit is entirely provider-reported. The
    # policy's 1000/day lives in a *different* bucket, which is the honest reading:
    # a per-minute ceiling and a daily allowance are not the same allowance.
    bucket = next(q for q in body["quota"] if q["deploy_id"] == "google:free"
                  and q["window"] == "minute")
    assert bucket["headroom"] == pytest.approx(0.15)      # 3 of the provider's 20
    assert bucket["headroom_source"] == "observed"
    assert bucket["limit_n"] is None                     # never configured
    assert bucket["observed_limit_n"] == 20
    assert bucket["observed_remaining_n"] == 3
    assert "exhaustion" in bucket

    day = next(q for q in body["quota"] if q["deploy_id"] == "google:free"
               and q["window"] == "day")
    assert day["headroom_source"] == "configured"
    assert day["limit_n"] == 1000


def test_overview_savings_carries_its_basis(tmp_path, monkeypatch):
    """Spend and Avoided are estimates, so the payload says how they were priced."""
    body = _client(tmp_path, monkeypatch).get("/v1/economics/overview").json()
    savings = body["savings"]
    assert savings["price_basis"]
    assert "priced_free_share" in savings


def test_overview_includes_the_open_anomalies(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/v1/economics/overview").json()
    assert len(body["anomalies"]) == 2                    # input and output
    assert {a["kind"] for a in body["anomalies"]} == {"unit_scale"}
    assert {a["severity"] for a in body["anomalies"]} == {"high"}
    assert all(a["deploy_id"] == "groq:m" for a in body["anomalies"])


def test_overview_includes_reviews_with_the_state_transition(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/v1/economics/overview").json()
    review = next(r for r in body["reviews"] if r["deploy_id"] == "p:m")
    assert review["status"] == "hibernated"
    # The transition, as a fact, rather than re-derived from the reason string.
    assert review["pricing_type_before"] == "free"


def test_overview_reports_the_pricing_state_distribution(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/v1/economics/overview").json()
    states = body["pricing_states"]
    assert states.get("paid", 0) >= 3
    assert states.get("free_with_quota", 0) >= 1
    assert states.get("hibernated", 0) >= 1
    # The quarantined arm has no trusted price left, so it is `unknown` — not
    # `free` and not the market's number. That distinction is the whole point.
    assert states.get("unknown", 0) >= 1
    assert states.get("free", 0) == 0


def test_a_price_and_its_trust_state_come_from_one_moment(tmp_path, monkeypatch):
    """The reason for one endpoint: the provider price and its reconciliation state
    are read together, so they cannot be *different* readings."""
    body = _client(tmp_path, monkeypatch).get("/v1/economics/overview").json()
    groq = next(p for p in body["providers"] if p["provider"] == "groq")
    # Its only deployment was quarantined, so there is no verified price to report.
    assert groq["min_in"] is None or groq["min_in_state"] == "quarantined"


# ------------------------------------------------------------ the sub-resources


def test_the_anomalies_endpoint_filters_by_status(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/v1/economics/anomalies").json()["count"] == 2
    assert client.get("/v1/economics/anomalies", params={"status": "resolved"}).json()["count"] == 0
    # An empty status is the whole history, not a filter that matches nothing.
    assert client.get("/v1/economics/anomalies", params={"status": ""}).json()["count"] == 2


def test_the_quota_endpoint_serves_buckets(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/v1/economics/quota").json()
    assert body["count"] >= 2
    assert {"deploy_id", "headroom", "headroom_source"} <= set(body["buckets"][0])


def test_the_providers_endpoint_serves_the_rollup(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/v1/economics/providers").json()
    assert body["count"] >= 4
    assert {"provider", "n", "min_in", "min_in_source"} <= set(body["providers"][0])


def test_the_deployment_detail_returns_the_whole_story(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/v1/economics/deployments/novita:m").json()
    assert body["deployment"]["deploy_id"] == "novita:m"
    assert {r["kind"] for r in body["resolution"]} == {"input", "output"}
    assert body["history"]                             # at least one belief
    assert isinstance(body["anomalies"], list)
    assert isinstance(body["quota"], list)


def test_the_deployment_detail_404s_for_something_unknown(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch).get("/v1/economics/deployments/nope:nope")
    assert r.status_code == 404


def test_a_deploy_id_with_a_slash_is_routable(tmp_path, monkeypatch):
    """Real ids look like `openrouter/novita:model:free`, so the route takes a path."""
    client = _client(tmp_path, monkeypatch)
    # The seeded novita deployment has no slash, but the route must still accept one.
    assert client.get("/v1/economics/deployments/openrouter/thinkingmachines/x:free").status_code == 404
    assert client.get("/v1/economics/deployments/novita:m").status_code == 200
