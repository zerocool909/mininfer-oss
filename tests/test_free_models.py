"""The user-facing free-model summary: every free arm, scouted or not.

`GET /v1/free-models` answers "what can I use for free, and what is each one good
at?" It lists the whole free tier — an arm the scout has not reached yet carries
a null summary rather than being hidden — and the summaries it does show were
researched through TinyFish.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from mininfer.schema import Deployment, Weights
from mininfer.store import Store

_POLICY = """\
policy:
  name: test
  top_k: 3
  require_distinct_provider: false
  require: {}
  min_context: 0
  min_success_lb: 0.0
  allow_unknown_price: true
tasks:
  t:
    description: test
    tokens_in: 10
    tokens_out: 10
"""


def _seed(path) -> Store:
    s = Store(path)
    s.upsert_weights(Weights("w1", "Alpha"))
    s.upsert_weights(Weights("w2", "Beta"))
    s.upsert_deployment(Deployment("p1:f1", "w1", "p1", "f1", price_in=0.0,
                                   price_out=0.0, zero_price=True, context_window=8000))
    s.upsert_deployment(Deployment("p2:f2", "w2", "p2", "f2", price_in=0.0,
                                   price_out=0.0, zero_price=True, context_window=8000))
    # A paid arm must not appear in the free list.
    s.upsert_deployment(Deployment("p3:paid", "w2", "p3", "paid", price_in=1.0,
                                   price_out=2.0, context_window=8000))
    s.upsert_dossier("p2:f2", weights_id="w2", display_name="Beta", provider="p2",
                     price_out=0.0, core_competency="good at beta things",
                     summary="A summary of Beta.",
                     warrior="The Beta Blade", story="Beta holds the line.",
                     strengths=[], weaknesses=[],
                     when_to_use=["t"], when_not_to_use=[], best_for=["t"],
                     search_sources=[{"title": "src", "url": "https://s/"}],
                     facts={}, confidence=0.8,
                     generated_at="2026-10-09T00:00:00+00:00")
    s.commit()
    return s


def test_the_store_lists_the_whole_free_tier(tmp_path):
    s = _seed(tmp_path / "f.db")
    rows = s.free_model_summaries()
    ids = {r["deploy_id"] for r in rows}
    assert ids == {"p1:f1", "p2:f2"}          # the paid arm is absent
    by_id = {r["deploy_id"]: r for r in rows}
    assert by_id["p2:f2"]["summary"] == "A summary of Beta."
    assert by_id["p2:f2"]["warrior"] == "The Beta Blade"
    assert by_id["p2:f2"]["story"] == "Beta holds the line."
    assert by_id["p1:f1"]["summary"] is None  # not scouted yet, still listed
    s.close()


def test_the_store_filters_on_the_summary(tmp_path):
    s = _seed(tmp_path / "g.db")
    assert {r["deploy_id"] for r in s.free_model_summaries(substr="beta things")} == {"p2:f2"}
    s.close()


def test_the_endpoint_is_readable_and_complete(tmp_path, monkeypatch):
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    _seed(tmp_path / "p.db").close()

    from mininfer.proxy import app
    r = TestClient(app).get("/v1/free-models")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    by_id = {m["deploy_id"]: m for m in body["models"]}
    assert by_id["p2:f2"]["core_competency"] == "good at beta things"
    assert by_id["p1:f1"]["core_competency"] is None
