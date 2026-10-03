"""A free model that starts charging is hibernated, not silently used.

The failure this prevents is a spend one, and it is invisible: the registry still
says "free", the router keeps choosing that arm because it is cheapest, and the
provider bills for every request. So a free -> paid transition is not a price
update — it takes the arm out of the ranking and asks a human.
"""
from __future__ import annotations

from mininfer.router import Policy, build_candidates
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store


def _deploy(*, free: bool, pin: float | None, pout: float | None) -> Deployment:
    return Deployment("p:m", "w", "p", "m", price_in=pin, price_out=pout,
                      zero_price=1 if free else 0, context_window=8000)


def _seed(tmp_path) -> Store:
    s = Store(tmp_path / "h.db")
    s.upsert_weights(Weights("w", "m"))
    s.upsert_deployment(_deploy(free=True, pin=0.0, pout=0.0))
    s.commit()
    return s


def _status(s: Store) -> str:
    return s.conn.execute(
        "SELECT status FROM deployments WHERE deploy_id='p:m'").fetchone()["status"]


def test_a_free_model_that_starts_charging_is_hibernated(tmp_path):
    s = _seed(tmp_path)
    s.upsert_deployment(_deploy(free=False, pin=1.0, pout=2.0))
    s.commit()
    row = s.conn.execute(
        "SELECT status, status_reason FROM deployments WHERE deploy_id='p:m'").fetchone()
    assert row["status"] == "hibernated"
    assert "was free" in row["status_reason"]
    assert "$1.0000" in row["status_reason"]     # per-Mtok, not per-token
    s.close()


def test_a_hibernated_model_is_out_of_the_ranking(tmp_path):
    s = _seed(tmp_path)
    s.upsert_deployment(_deploy(free=False, pin=1.0, pout=2.0))
    s.commit()
    task = TaskProfile("t", tokens_in=100, tokens_out=100)
    cand = next(c for c in build_candidates(s, task, Policy(require_callable=False))
                if c.deploy_id == "p:m")
    assert cand.rejected == "status=hibernated"
    s.close()


def test_approving_returns_it_to_live(tmp_path):
    s = _seed(tmp_path)
    s.upsert_deployment(_deploy(free=False, pin=1.0, pout=2.0))
    s.commit()
    assert s.decide_review("p:m", approve=True) is True
    assert _status(s) == "live"
    task = TaskProfile("t", tokens_in=100, tokens_out=100)
    cand = next(c for c in build_candidates(s, task, Policy(require_callable=False))
                if c.deploy_id == "p:m")
    assert cand.rejected is None
    s.close()


def test_rejecting_retires_it(tmp_path):
    s = _seed(tmp_path)
    s.upsert_deployment(_deploy(free=False, pin=1.0, pout=2.0))
    s.commit()
    assert s.decide_review("p:m", approve=False, note="too expensive") is True
    assert _status(s) == "deprecated"
    assert s.reviews() == []
    s.close()


def test_a_later_ingest_does_not_un_hibernate(tmp_path):
    """The operator is asked once, not every six hours."""
    s = _seed(tmp_path)
    s.upsert_deployment(_deploy(free=False, pin=1.0, pout=2.0))
    s.commit()
    s.upsert_deployment(_deploy(free=False, pin=1.0, pout=2.0))
    s.commit()
    assert _status(s) == "hibernated"
    s.close()


def test_it_returns_when_it_is_free_again(tmp_path):
    s = _seed(tmp_path)
    s.upsert_deployment(_deploy(free=False, pin=1.0, pout=2.0))
    s.commit()
    s.upsert_deployment(_deploy(free=True, pin=0.0, pout=0.0))
    s.commit()
    assert _status(s) == "live"
    assert s.reviews() == []
    s.close()


def test_a_plain_paid_price_change_is_not_a_review(tmp_path):
    """Only a *free* arm going paid is a spend surprise."""
    s = Store(tmp_path / "p.db")
    s.upsert_weights(Weights("w", "m"))
    s.upsert_deployment(_deploy(free=False, pin=1.0, pout=2.0))
    s.upsert_deployment(_deploy(free=False, pin=3.0, pout=4.0))
    s.commit()
    assert _status(s) == "live"
    assert s.reviews() == []
    s.close()


def test_deciding_something_that_is_not_hibernated_reports_false(tmp_path):
    s = _seed(tmp_path)
    assert s.decide_review("p:m", approve=True) is False
    s.close()


# --------------------------------------------------------------------------- #
# the admin surface
# --------------------------------------------------------------------------- #

def test_the_review_endpoints_and_the_admin_surface(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import mininfer.auth as auth
    import mininfer.proxy as proxy

    assert auth.classify("/v1/reviews") == "admin"
    assert auth.classify("/v1/reviews/decide") == "admin"

    monkeypatch.setenv("MI_DB", str(tmp_path / "h.db"))
    s = Store(tmp_path / "h.db")
    s.upsert_weights(Weights("w", "m"))
    s.upsert_deployment(_deploy(free=True, pin=0.0, pout=0.0))
    s.upsert_deployment(_deploy(free=False, pin=1.0, pout=2.0))   # free -> paid
    s.commit()
    s.close()

    c = TestClient(proxy.app)
    listed = c.get("/v1/reviews").json()["reviews"]
    assert [r["deploy_id"] for r in listed] == ["p:m"]
    assert "was free" in listed[0]["status_reason"]

    decided = c.post("/v1/reviews/decide", json={"deploy_id": "p:m", "approve": True})
    assert decided.json() == {"ok": True, "deploy_id": "p:m", "status": "live"}
    assert c.get("/v1/reviews").json()["reviews"] == []

    # Deciding twice is a 404, not a silent second write.
    assert c.post("/v1/reviews/decide",
                  json={"deploy_id": "p:m", "approve": False}).status_code == 404
