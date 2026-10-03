"""The proxy's use of intent classification, end to end through the API.

Three behaviours: an ambiguous prompt routes against the conservative join of the
tied profiles; the join is abandoned when it excludes everything; and a caller's
explicit task is recorded against the classifier's guess — that disagreement is
the only free label the system gets.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mininfer.schema import Deployment, Weights
from mininfer.store import Store

_POLICY = """\
policy:
  name: test
  top_k: 3
  require_distinct_provider: false
  on_unverified_capability: allow
  require: {}
  min_context: 0
  min_success_lb: 0.0
  allow_unknown_price: true
  # Same as config/policy.yaml: with 6 pseudo-observations a single-row registry
  # normalises to q=0.5 and the Wilson bound lands at 0.27, under beta's floor.
  prior_strength: 25.0
tasks:
  alpha:
    description: alpha task
    tokens_in: 100
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.2
    benchmark_keys: [coding]
    cues: [alpha]
  beta:
    description: beta task
    tokens_in: 900
    tokens_out: 90
    require: {}
    min_context: 0
    min_success_lb: 0.3
    benchmark_keys: [aa_intelligence]
    cues: [beta]
"""


def _client(tmp_path, monkeypatch, *, seed: bool) -> TestClient:
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("MI_INTENT_MODEL", raising=False)
    if seed:
        store = Store(tmp_path / "p.db")
        # Scored on both keys so it satisfies the merged profile too.
        store.upsert_weights(Weights("hf:t/m", "test-model", params_b=7.0,
                                     benchmark={"coding": 50.0, "aa_intelligence": 50.0}))
        store.upsert_deployment(Deployment("openrouter:m", "hf:t/m", "openrouter", "m",
                                           price_in=1.0, price_out=1.0, context_window=1000))
        store.commit()
        store.close()
    return TestClient(__import__("mininfer.proxy", fromlist=["app"]).app)


def _ask(client: TestClient, model: str, prompt: str):
    return client.post("/v1/chat/completions",
                       json={"model": model, "messages": [{"role": "user", "content": prompt}]})


def _last_decision(tmp_path) -> dict:
    store = Store(tmp_path / "p.db")
    row = store.conn.execute(
        "SELECT task, reason FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    store.close()
    return {"task": row["task"], "reason": json.loads(row["reason"])}


def test_ambiguous_prompt_routes_against_the_merged_profile(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, seed=True)
    _ask(client, "auto", "alpha beta")

    reason = _last_decision(tmp_path)["reason"]
    assert reason["intent"]["confidence"] == 0.5
    assert reason["profile"] == "alpha+beta"
    assert reason["merged_from"] == ["alpha", "beta"]


def test_confident_prompt_uses_a_single_profile(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, seed=True)
    _ask(client, "auto", "just alpha")

    reason = _last_decision(tmp_path)["reason"]
    assert reason["profile"] == "alpha"
    assert "merged_from" not in reason
    assert reason["intent"]["confidence"] == 1.0


def test_merge_is_abandoned_when_it_excludes_everything(tmp_path, monkeypatch):
    """The join is strictly stricter, so it can empty the pool.

    With no registry at all nothing passes either profile, but the recorded
    profile must be the single best guess rather than a merge that produced
    nothing.
    """
    client = _client(tmp_path, monkeypatch, seed=False)
    _ask(client, "auto", "alpha beta")

    reason = _last_decision(tmp_path)["reason"]
    assert reason["intent"]["confidence"] == 0.5      # still ambiguous
    assert reason["profile"] == "alpha"               # but not merged
    assert "merged_from" not in reason


def test_explicit_task_is_recorded_against_the_classifier_guess(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, seed=True)
    _ask(client, "beta", "alpha beta")

    reason = _last_decision(tmp_path)["reason"]
    assert reason["intent"]["explicit"] == "beta"
    assert reason["intent"]["agreed"] is False
    # an explicit choice is never overridden by a merge
    assert reason["profile"] == "beta"
    assert "merged_from" not in reason
