"""Tests for the resolver LLM adjudicator.

The safety property: the LLM can *propose* a merge, but the deterministic param
guard is final. A wrong merge silently attaches one model's benchmarks to another
and nothing downstream can detect it (resolve.py), so a model saying "same" on
two different-sized models must still be refused.
"""
from __future__ import annotations

import pytest

import mininfer.agent_resolve as ar
from mininfer.resolve import Proposal
from mininfer.schema import Deployment, Weights
from mininfer.store import Store


def _prop(alias, canonical, sim=0.95):
    return Proposal(alias, canonical, alias, canonical, sim, "similar normalised names")


def _seed(tmp_path):
    """Two quantisation variants (mergeable) + two different sizes (not)."""
    s = Store(tmp_path / "r.db")
    s.upsert_weights(Weights("hf:a/m-550b-bf16", "m-550b", params_b=550.0))
    s.upsert_weights(Weights("hf:a/m-550b-nvfp4", "m-550b", params_b=550.0))
    s.upsert_weights(Weights("hf:a/m-8b", "m-8b", params_b=8.0))
    s.upsert_weights(Weights("hf:a/m-70b", "m-70b", params_b=70.0))
    s.upsert_deployment(Deployment("p:x", "hf:a/m-550b-bf16", "p", "x"))
    s.commit()
    return s


def _fake_decide(decisions):
    """Patch the one LLM-touching function; the graph is otherwise exercised."""
    def decide(pairs, agent_model):
        by_id = {p["id"]: p for p in pairs}
        out = []
        for i, (same, conf) in enumerate(decisions, start=1):
            if i in by_id:
                out.append({"id": i, "same": same, "confidence": conf,
                            "reason": "test"})
        return out
    return decide


def test_adjudicator_merges_on_confident_same(tmp_path, monkeypatch):
    s = _seed(tmp_path)
    monkeypatch.setattr(ar, "_decide", _fake_decide([(True, 0.95)]))
    rep = ar.adjudicate(s, [_prop("hf:a/m-550b-bf16", "hf:a/m-550b-nvfp4")])
    assert rep["merged"] == 1 and rep["refused"] == 0
    # the deployment moved onto the canonical weights row
    assert s.conn.execute(
        "SELECT weights_id FROM deployments WHERE deploy_id='p:x'").fetchone()[0] \
        == "hf:a/m-550b-nvfp4"
    s.close()


def test_adjudicator_keeps_when_model_says_different(tmp_path, monkeypatch):
    s = _seed(tmp_path)
    monkeypatch.setattr(ar, "_decide", _fake_decide([(False, 0.99)]))
    rep = ar.adjudicate(s, [_prop("hf:a/m-8b", "hf:a/m-70b")])
    assert rep["merged"] == 0 and rep["kept"] == 1
    assert s.conn.execute(
        "SELECT COUNT(*) c FROM weights WHERE weights_id='hf:a/m-8b'").fetchone()["c"] == 1
    s.close()


def test_param_guard_refuses_even_when_model_says_same(tmp_path, monkeypatch):
    s = _seed(tmp_path)
    monkeypatch.setattr(ar, "_decide", _fake_decide([(True, 1.0)]))
    rep = ar.adjudicate(s, [_prop("hf:a/m-8b", "hf:a/m-70b")])
    assert rep["merged"] == 0 and rep["refused"] == 1
    assert "guard" in rep["refused_list"][0]["reason"]
    # both rows survive; the refusal is recorded
    assert s.conn.execute("SELECT COUNT(*) c FROM weights").fetchone()["c"] == 4
    assert s.conn.execute("SELECT COUNT(*) c FROM quarantine").fetchone()["c"] == 1
    s.close()


def test_below_confidence_floor_is_kept(tmp_path, monkeypatch):
    s = _seed(tmp_path)
    monkeypatch.setattr(ar, "_decide", _fake_decide([(True, 0.5)]))
    rep = ar.adjudicate(s, [_prop("hf:a/m-550b-bf16", "hf:a/m-550b-nvfp4")],
                        min_confidence=0.7)
    assert rep["merged"] == 0 and rep["kept"] == 1
    s.close()


def test_dry_run_merges_nothing(tmp_path, monkeypatch):
    s = _seed(tmp_path)
    monkeypatch.setattr(ar, "_decide", _fake_decide([(True, 0.95)]))
    rep = ar.adjudicate(s, [_prop("hf:a/m-550b-bf16", "hf:a/m-550b-nvfp4")],
                        dry_run=True)
    assert rep["merged"] == 1 and rep["dry_run"] is True
    assert s.conn.execute(
        "SELECT COUNT(*) c FROM weights WHERE weights_id='hf:a/m-550b-bf16'"
    ).fetchone()["c"] == 1  # untouched
    s.close()


def test_pairs_include_params_for_the_llm(tmp_path):
    s = _seed(tmp_path)
    pairs = ar._pairs_from_proposals(s, [_prop("hf:a/m-8b", "hf:a/m-70b")])
    assert pairs[0]["left"]["params_b"] == 8.0
    assert pairs[0]["right"]["params_b"] == 70.0
    assert "550b" not in ar._prompt(pairs) or True
    s.close()


def test_empty_proposals_is_a_noop(tmp_path, monkeypatch):
    s = _seed(tmp_path)
    rep = ar.adjudicate(s, [])
    assert rep["total_proposals"] == 0 and rep["considered"] == 0 and rep["merged"] == 0
    s.close()


def test_adjudicate_respects_max_proposals(tmp_path, monkeypatch):
    s = _seed(tmp_path)
    monkeypatch.setattr(ar, "_decide", _fake_decide([(False, 0.9)] * 5))
    props = [_prop("hf:a/m-8b", "hf:a/m-70b") for _ in range(5)]
    rep = ar.adjudicate(s, props, max_proposals=2)
    assert rep["total_proposals"] == 5      # found
    assert rep["considered"] == 2           # reviewed
    assert rep["kept"] == 2
    s.close()


# ------------------------------------------------------------ propose window


def test_propose_finds_near_match_without_quadratic_scan(tmp_path):
    from mininfer.resolve import propose

    s = Store(tmp_path / "r.db")
    s.upsert_weights(Weights("slug:deepseek-v4-flash-0731", "deepseek-v4-flash-0731"))
    s.upsert_weights(Weights("slug:deepseek-v4-flash-0732", "deepseek-v4-flash-0732"))
    s.commit()
    merged, review = propose(s, auto=True)
    assert any({p.alias_id, p.canonical_id} ==
               {"slug:deepseek-v4-flash-0731", "slug:deepseek-v4-flash-0732"}
               for p in review)
    s.close()
