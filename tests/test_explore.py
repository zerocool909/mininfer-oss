"""The model explorer: browse the registry, not just the routable surface.

`/v1/models` answers "what virtual models can an OpenAI client ask for"; this
answers "what is actually in the registry, at what price, with what evidence".
The two are different enough that the second needs its own query — the router
filters to *eligible for this task now*, the explorer must show the arms the
router would reject, or the registry stops being auditable.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mininfer.schema import Deployment, Weights
from mininfer.store import Store


def _seed(path) -> Store:
    s = Store(path)
    s.upsert_weights(Weights(weights_id="hf:qwen7b", display_name="Qwen 7B",
                             family="qwen", params_b=7.0, modalities=("text",),
                             benchmark={"coding": 60.0}, benchmark_source="test"))
    s.upsert_weights(Weights(weights_id="hf:vision", display_name="Vision Model",
                             family="llava", params_b=13.0, modalities=("text", "image")))
    s.upsert_deployment(Deployment(
        deploy_id="openrouter:qwen7b-free", weights_id="hf:qwen7b",
        provider="openrouter", provider_model_id="qwen7b:free",
        context_window=32000, price_in=0.0, price_out=0.0, zero_price=True,
        free_variant=True, caps={"tools": True, "structured": True}))
    s.upsert_deployment(Deployment(
        deploy_id="openrouter:qwen7b", weights_id="hf:qwen7b",
        provider="openrouter", provider_model_id="qwen7b",
        context_window=128000, price_in=1.0, price_out=3.0,
        caps={"tools": True}))
    s.upsert_deployment(Deployment(
        deploy_id="groq:vision", weights_id="hf:vision",
        provider="groq", provider_model_id="llava",
        context_window=8000, price_in=0.2, price_out=0.6,
        caps={"vision": True, "structured": True}))
    s.upsert_deployment(Deployment(
        deploy_id="nvidia:unknown", weights_id="hf:qwen7b",
        provider="nvidia", provider_model_id="qwen7b",
        context_window=4000, price_in=None, price_out=None, caps={}))
    s.upsert_deployment(Deployment(
        deploy_id="sub:qwen7b", weights_id="hf:qwen7b",
        provider="sub", provider_model_id="qwen7b",
        context_window=32000, price_in=0.0, price_out=0.0, subscription=True,
        caps={"tools": True}))
    s.commit()
    # 3 attempts on the paid arm: 2 wins, and a 4th on groq.
    for ok in (True, True, False):
        s.observe("openrouter:qwen7b", "code_edit", ok, ts="2026-01-01T00:00:00",
                  latency_ms=100.0, signal_kind="provider_reported",
                  signal_value=1.0 if ok else 0.0)
    s.observe("groq:vision", "shelf_image_audit", True, ts="2026-01-01T00:00:00",
              latency_ms=50.0, signal_kind="provider_reported", signal_value=1.0)
    s.commit()
    return s


@pytest.fixture
def store(tmp_path):
    s = _seed(tmp_path / "explore.db")
    yield s
    s.close()


def _ids(result) -> list[str]:
    return [m["deploy_id"] for m in result["models"]]


def test_it_shows_every_deployment_not_just_the_routable_ones(store):
    out = store.explore_models(limit=200)
    assert out["total"] == 5
    assert out["count"] == 5
    assert set(_ids(out)) == {
        "openrouter:qwen7b-free", "openrouter:qwen7b", "groq:vision",
        "nvidia:unknown", "sub:qwen7b"}


def test_unknown_price_is_not_free(store):
    """The distinction the router is built on must survive into the explorer."""
    out = {m["deploy_id"]: m for m in store.explore_models(limit=200)["models"]}
    assert out["nvidia:unknown"]["price_in"] is None
    assert out["nvidia:unknown"]["free_kind"] is None
    assert out["nvidia:unknown"]["free"] is False
    assert out["openrouter:qwen7b-free"]["free_kind"] == "zero_price"
    assert out["openrouter:qwen7b-free"]["free"] is True


def test_a_subscription_is_not_free_it_is_prepaid(store):
    out = {m["deploy_id"]: m for m in store.explore_models(limit=200)["models"]}
    assert out["sub:qwen7b"]["free_kind"] == "subscription"
    assert out["sub:qwen7b"]["free"] is False
    assert "sub:qwen7b" not in _ids(store.explore_models(free_only=True, limit=200))


def test_free_only_excludes_paid_and_unknown(store):
    out = store.explore_models(free_only=True, limit=200)
    assert _ids(out) == ["openrouter:qwen7b-free"]


def test_provider_filter(store):
    assert _ids(store.explore_models(provider="groq", limit=200)) == ["groq:vision"]


def test_capability_matches_confirmed_true_only(store):
    tools = _ids(store.explore_models(capability="tools", limit=200))
    # The free arm, the paid arm and the subscription carry tools:true; the
    # unknown-caps deployment does not, and a null is not a yes.
    assert set(tools) == {"openrouter:qwen7b-free", "openrouter:qwen7b", "sub:qwen7b"}
    assert _ids(store.explore_models(capability="vision", limit=200)) == ["groq:vision"]


def test_capability_filter_ignores_an_unknown_key(store):
    """An unknown capability is not an error and must not silently match all."""
    assert store.explore_models(capability="telepathy", limit=200)["total"] == 5


def test_min_context_and_max_price(store):
    assert set(_ids(store.explore_models(min_context=16000, limit=200))) == {
        "openrouter:qwen7b-free", "openrouter:qwen7b", "sub:qwen7b"}
    # `max_price_out` excludes null prices: an unknown price is not a cheap one.
    assert set(_ids(store.explore_models(max_price_out=1.0, limit=200))) == {
        "openrouter:qwen7b-free", "groq:vision", "sub:qwen7b"}


def test_text_search_spans_deploy_id_model_id_and_weights(store):
    assert _ids(store.explore_models(q="llava", limit=200)) == ["groq:vision"]
    assert _ids(store.explore_models(q="Qwen 7B", limit=200)) != []
    assert _ids(store.explore_models(q="hf:vision", limit=200)) == ["groq:vision"]


def test_success_rate_comes_from_the_observation_log(store):
    out = {m["deploy_id"]: m for m in store.explore_models(limit=200)["models"]}
    assert out["openrouter:qwen7b"]["n"] == 3
    assert out["openrouter:qwen7b"]["wins"] == 2
    assert out["openrouter:qwen7b"]["success_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert out["groq:vision"]["success_rate"] == 1.0
    assert out["nvidia:unknown"]["n"] == 0
    assert out["nvidia:unknown"]["success_rate"] is None


def test_sort_by_success_puts_evidence_first(store):
    assert _ids(store.explore_models(sort="success", limit=200))[0] == "groq:vision"
    # An unobserved arm must not sort above a measured one.
    assert _ids(store.explore_models(sort="success", limit=200))[-1] != "groq:vision"


def test_an_unknown_sort_key_falls_back_to_name(store):
    out = store.explore_models(sort="; DROP TABLE deployments", limit=200)
    assert out["sort"] == "name"
    assert out["total"] == 5


def test_pagination_is_stable_and_bounded(store):
    first = store.explore_models(sort="name", limit=2, offset=0)
    second = store.explore_models(sort="name", limit=2, offset=2)
    assert first["count"] == 2 and second["count"] == 2
    assert set(_ids(first)).isdisjoint(_ids(second))
    assert first["total"] == second["total"] == 5
    # A caller cannot ask for the whole registry in one page.
    assert store.explore_models(limit=100000)["limit"] == 200


def test_benchmarks_travel_with_the_model(store):
    """The explorer must show the quality evidence, not just the price."""
    out = {m["deploy_id"]: m for m in store.explore_models(limit=200)["models"]}
    assert out["openrouter:qwen7b"]["benchmark"] == {"coding": 60.0}
    assert out["openrouter:qwen7b"]["benchmark_source"] == "test"
    # A weights row with no benchmark reads as empty, never as a score of zero.
    assert out["groq:vision"]["benchmark"] == {}


def test_facets_report_confirmed_capabilities(store):
    caps = store._capability_facets()
    assert caps["tools"] == 3
    assert caps["vision"] == 1
    assert caps["structured"] == 2
    assert caps["reasoning"] == 0


# --------------------------------------------------------------------------- #
# the endpoint
# --------------------------------------------------------------------------- #


def test_endpoint_serves_the_explorer(tmp_path, monkeypatch):
    _seed(tmp_path / "p.db").close()
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    from mininfer.proxy import app

    client = TestClient(app)
    r = client.get("/v1/models/explore", params={"free_only": True, "limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["models"][0]["deploy_id"] == "openrouter:qwen7b-free"
    assert body["models"][0]["caps"]["tools"] is True
    assert isinstance(body["providers"], list) and "groq" in body["providers"]
    assert body["capabilities"]["vision"] == 1
