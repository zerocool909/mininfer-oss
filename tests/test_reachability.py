"""Reachability is one question, asked in two places.

The router and the proxy must agree on which providers are callable. They did
not: `available_providers` admitted Google unconditionally ("first-class on
leaderboard"), so the router ranked `google:*` arms first while the proxy skipped
every one of them with `no_api_key`. The request still completed on the fallback,
but the decision log recorded a model that was never dialled — which is exactly
the kind of quiet disagreement that makes a router impossible to reason about.
"""
from __future__ import annotations

from mininfer.execute import available_providers
from mininfer.router import Policy, build_candidates
from mininfer.schema import Deployment, TaskProfile, Weights
from mininfer.store import Store


def test_google_is_not_callable_without_a_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    provs = available_providers()
    assert "groq" in provs
    assert "google" not in provs


def test_google_is_callable_with_a_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert "google" in available_providers()


def test_local_engines_are_always_callable(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert "ollama" in available_providers()


def test_an_unkeyed_provider_is_rejected_as_a_candidate(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    s = Store(tmp_path / "r.db")
    s.upsert_weights(Weights("hf:gm", "gm", benchmark={"aa_intelligence": 90.0}))
    s.upsert_deployment(Deployment(
        "google:m", "hf:gm", "google", "m",
        price_in=0.0, price_out=0.0, zero_price=1, context_window=8000))
    s.commit()

    task = TaskProfile("t", tokens_in=100, tokens_out=100,
                       benchmark_keys=("aa_intelligence",))
    google = next(c for c in build_candidates(s, task, Policy(require_callable=True))
                  if c.deploy_id == "google:m")
    assert google.rejected == "no API key (provider not configured)"

    # ...but the theoretical ranking is still reachable on request.
    shown = next(c for c in build_candidates(s, task, Policy(require_callable=False))
                 if c.deploy_id == "google:m")
    assert shown.rejected is None
    s.close()


def test_a_retired_google_model_is_deprecated_on_open(tmp_path, monkeypatch):
    """A seeded row that 404s is worse than no row: it is offered, then fails."""
    import mininfer.store as store_mod

    db = tmp_path / "g.db"
    s = store_mod.Store(db)
    s.upsert_weights(Weights("slug:google:gemini-1.5-pro", "gemini-1.5-pro"))
    s.upsert_deployment(Deployment(
        "google:gemini-1.5-pro", "slug:google:gemini-1.5-pro", "google", "gemini-1.5-pro",
        price_in=0.0, price_out=0.0, zero_price=1, context_window=8000))
    s.commit()
    s.close()

    # Re-open in a fresh process-equivalent so the migration runs.
    monkeypatch.setattr(store_mod, "_INITIALIZED_DBS", set())
    s2 = store_mod.Store(db)
    row = s2.conn.execute(
        "SELECT status FROM deployments WHERE deploy_id='google:gemini-1.5-pro'").fetchone()
    assert row["status"] == "deprecated"

    task = TaskProfile("t", tokens_in=100, tokens_out=100)
    cand = next(c for c in build_candidates(s2, task, Policy(require_callable=False))
                if c.deploy_id == "google:gemini-1.5-pro")
    assert cand.rejected == "status=deprecated"
    s2.close()
