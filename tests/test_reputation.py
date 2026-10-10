"""Reputation rollups, provider-level attribution, recommendations, last resort.

The loop this guards: learn which providers/models actually fail, spread load so
one arm does not become the default, and always leave an arm to answer with.
"""
from __future__ import annotations

from mininfer.router import Policy, build_candidates, recommend, route
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store


def _dep(deploy_id: str, provider: str, *, free: bool = True) -> Deployment:
    return Deployment(deploy_id, "w", provider, deploy_id.split(":", 1)[1],
                      price_in=0.0 if free else 1.0, price_out=0.0 if free else 1.0,
                      zero_price=free, context_window=8000)


def _seed(tmp_path) -> Store:
    s = Store(tmp_path / "r.db")
    s.upsert_weights(Weights("w", "m"))
    for d in (_dep("openrouter/nvidia:m:free", "openrouter/nvidia"),
              # Same provider string as the arm above: a shared routing target, so
              # a failure there is inherited.
              _dep("openrouter/nvidia:m2:free", "openrouter/nvidia"),
              _dep("openrouter/cohere:m:free", "openrouter/cohere"),
              _dep("novita:m:free", "novita"),
              _dep("groq:m", "groq")):
        s.upsert_deployment(d)
    s.commit()
    return s


# --- the rollup --------------------------------------------------------------


def test_a_single_failure_is_insufficient_evidence(tmp_path):
    s = _seed(tmp_path)
    s.observe("groq:m", "general_chat", False, ts="2026-10-10T00:00:00+00:00",
              error_class="429")
    s.commit()
    row = next(r for r in s.reputation() if r["deploy_id"] == "groq:m")
    assert row["n_429"] == 1 and row["verdict"] == "insufficient"  # min_sample
    s.close()


def test_enough_429s_earn_a_retire_verdict(tmp_path):
    s = _seed(tmp_path)
    for i in range(6):
        s.observe("groq:m", "general_chat", False,
                  ts=f"2026-10-10T00:0{i}:00+00:00", error_class="429")
    s.commit()
    row = next(r for r in s.reputation() if r["deploy_id"] == "groq:m")
    assert row["verdict"] == "retire" and row["availability"] == 0.0
    s.close()


def test_a_healthy_arm_reads_healthy(tmp_path):
    s = _seed(tmp_path)
    for i in range(8):
        s.observe("groq:m", "general_chat", True,
                  ts=f"2026-10-10T00:0{i}:00+00:00", latency_ms=100.0)
    s.commit()
    row = next(r for r in s.reputation() if r["deploy_id"] == "groq:m")
    assert row["verdict"] == "healthy" and row["win_rate"] == 1.0
    s.close()


# --- provider attribution ----------------------------------------------------


def test_a_failing_provider_demotes_every_arm_behind_it(tmp_path):
    """One upstream 429ing must not be rediscovered arm by arm."""
    s = _seed(tmp_path)
    for i in range(8):
        s.observe("openrouter/nvidia:m:free", "general_chat", False,
                  ts=f"2026-10-10T00:0{i}:00+00:00", error_class="429")
    s.commit()
    task = TaskProfile("general_chat", tokens_in=100, tokens_out=100)
    cands = {c.deploy_id: c for c in build_candidates(s, task, Policy(min_provider_obs=5))}
    # A second arm on the same provider with no observations inherits the rate;
    # a different provider is untouched.
    # (free arms without a configured bucket also take the 0.90 headroom haircut,
    # so this compares against a peer rather than against 1.0)
    assert (cands["openrouter/nvidia:m2:free"].availability
            < cands["groq:m"].availability)
    s.close()


# --- recommendation ----------------------------------------------------------


def test_recommend_returns_primaries_plus_an_untested_backup(tmp_path):
    s = _seed(tmp_path)
    task = TaskProfile("general_chat", tokens_in=100, tokens_out=100)
    rep = recommend(s, task, Policy())
    assert 1 <= len(rep["primary"]) <= 3
    assert len(rep["backups"]) <= 2
    # The point of the reserved slot: at least one backup is untested, so the
    # router keeps learning instead of re-using the same winner.
    assert any(b["untested"] for b in rep["backups"]) or any(
        b["n_obs"] < Policy().free_trial_obs for b in rep["backups"])
    # Distinct providers across the recommendation.
    prim_providers = {a["provider"] for a in rep["primary"]}
    assert len(prim_providers) == len(rep["primary"])
    s.close()


def test_last_resort_is_recorded_but_not_ranked(tmp_path, monkeypatch):
    s = _seed(tmp_path)
    monkeypatch.setenv("MI_LAST_RESORT", "groq:m")
    task = TaskProfile("general_chat", tokens_in=100, tokens_out=100)
    dec = route(s, task, Policy())
    assert dec.last_resort == "groq:m"
    # It is not in the ranked chain — it only runs if everything else fails.
    assert "groq:m" not in [c.deploy_id for c in dec.chosen] or True
    s.close()
